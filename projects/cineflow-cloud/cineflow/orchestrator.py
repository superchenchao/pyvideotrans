from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable
from typing import TypeVar

from .events import EventBroker
from .fusion import fuse_speakers
from .models import (
    DubbingArtifact,
    JobEvent,
    JobRecord,
    JobRequest,
    JobState,
    LineEvidence,
    MediaArtifacts,
    OutputArtifact,
    SpeakerDecision,
    StageMetric,
)
from .providers.contracts import PipelineProviders
from .sla import AdmissionController, Deadline

T = TypeVar("T")


class JobStore:
    """Small demo store; production deployments should replace it with Redis/Postgres."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._lock = asyncio.Lock()

    async def put(self, record: JobRecord) -> None:
        async with self._lock:
            self._jobs[record.job_id] = record

    async def get(self, job_id: str) -> JobRecord | None:
        async with self._lock:
            return self._jobs.get(job_id)


class PipelineOrchestrator:
    # Deadlines reserve the last ten seconds for state persistence and response delivery.
    PREPARE_DEADLINE = 130.0
    ASR_DEADLINE = 60.0
    SPEAKER_AND_TRANSLATION_DEADLINE = 170.0
    TTS_DEADLINE = 245.0
    ASSEMBLY_DEADLINE = 290.0

    def __init__(
        self,
        providers: PipelineProviders,
        admission: AdmissionController,
        store: JobStore,
        events: EventBroker | None = None,
    ) -> None:
        self.providers = providers
        self.admission = admission
        self.store = store
        self.events = events or EventBroker()

    async def submit(self, request: JobRequest) -> JobRecord:
        predicted, cost = self.admission.quote(request)
        health = await self.providers.health()
        self.admission.validate_provider_health(request, health)
        await self.admission.acquire_nowait()

        job_id = uuid.uuid4().hex
        record = JobRecord(
            job_id=job_id,
            state=JobState.ACCEPTED,
            accepted_at_monotonic=time.monotonic(),
            deadline_seconds=self.admission.settings.hard_sla_seconds,
            request=request,
            predicted_seconds=round(predicted, 2),
            estimated_cost_cny=cost.total_cny,
            cost_breakdown=cost.breakdown,
        )
        await self.store.put(record)
        await self.events.publish(
            job_id,
            JobEvent(
                event_type="accepted",
                stage="accepted",
                progress=0,
                message="job admitted with an immediate pre-warmed slot",
                data={
                    "predicted_seconds": record.predicted_seconds,
                    "estimated_cost_cny": record.estimated_cost_cny,
                },
            ),
        )
        asyncio.create_task(self._run(record), name=f"cineflow:{job_id}")
        return record

    async def _stage(
        self,
        record: JobRecord,
        deadline: Deadline,
        name: str,
        cutoff: float,
        awaitable: Awaitable[T],
    ) -> T:
        record.current_stage = name
        await self.events.publish(
            record.job_id,
            JobEvent(event_type="stage_started", stage=name, progress=record.progress),
        )
        started = time.monotonic()
        try:
            result = await deadline.run_before(cutoff, awaitable)
        except Exception as exc:
            elapsed = round(time.monotonic() - started, 3)
            record.metrics.append(
                StageMetric(
                    stage=name,
                    elapsed_seconds=elapsed,
                    degraded=True,
                    detail=str(exc),
                )
            )
            await self.events.publish(
                record.job_id,
                JobEvent(
                    event_type="stage_failed",
                    stage=name,
                    progress=record.progress,
                    message=str(exc),
                    data={"elapsed_seconds": elapsed},
                ),
            )
            raise
        elapsed = round(time.monotonic() - started, 3)
        record.metrics.append(StageMetric(stage=name, elapsed_seconds=elapsed))
        await self.events.publish(
            record.job_id,
            JobEvent(
                event_type="stage_completed",
                stage=name,
                progress=record.progress,
                data={"elapsed_seconds": elapsed},
            ),
        )
        return result

    @staticmethod
    async def _cancel_pending(tasks: set[asyncio.Task[object]]) -> None:
        pending = [task for task in tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        finished = [task for task in tasks if task.done()]
        if finished:
            # Retrieve exceptions so the event loop never reports an orphaned task.
            await asyncio.gather(*finished, return_exceptions=True)

    async def _run(self, record: JobRecord) -> None:
        deadline = Deadline(record.deadline_seconds)
        record.state = JobState.RUNNING
        tasks: set[asyncio.Task[object]] = set()

        media_task = asyncio.create_task(
            self._stage(
                record,
                deadline,
                "prepare_media",
                self.PREPARE_DEADLINE,
                self.providers.prepare_media(record.request),
            )
        )
        tasks.add(media_task)

        try:
            transcript = await self._stage(
                record,
                deadline,
                "asr",
                self.ASR_DEADLINE,
                self.providers.transcribe(record.request),
            )
            record.progress = 25

            speaker_task = asyncio.create_task(
                self._stage(
                    record,
                    deadline,
                    "multimodal_speaker",
                    self.SPEAKER_AND_TRANSLATION_DEADLINE,
                    self.providers.analyze_speakers(record.request, transcript),
                )
            )
            translation_task = asyncio.create_task(
                self._stage(
                    record,
                    deadline,
                    "translation",
                    self.SPEAKER_AND_TRANSLATION_DEADLINE,
                    self.providers.translate(record.request, transcript),
                )
            )
            tasks.update({speaker_task, translation_task})

            translated = await translation_task
            evidence: list[LineEvidence]
            if record.request.multi_speaker:
                try:
                    evidence = await speaker_task
                    decisions = fuse_speakers(transcript.lines, evidence)
                except Exception as exc:
                    # Runtime degradation is preferable to crossing the hard deadline.
                    record.warnings.append(
                        f"speaker fusion degraded to one Azure voice: {exc}"
                    )
                    decisions = self._single_voice_decisions(transcript.lines)
            else:
                speaker_task.cancel()
                decisions = self._single_voice_decisions(transcript.lines)

            record.decisions = decisions
            record.progress = 55

            dubbing: DubbingArtifact = await self._stage(
                record,
                deadline,
                "azure_tts",
                self.TTS_DEADLINE,
                self.providers.synthesize(record.request, translated, decisions),
            )
            record.progress = 78

            try:
                media = await media_task
            except Exception as exc:
                record.warnings.append(
                    f"media enhancement missed its deadline; original video retained: {exc}"
                )
                media = MediaArtifacts(
                    video_url=str(record.request.input_url),
                    degraded_features=["subtitle_removal", "background_separation"],
                )

            result: OutputArtifact = await self._stage(
                record,
                deadline,
                "assemble",
                self.ASSEMBLY_DEADLINE,
                self.providers.assemble(record.request, media, translated, dubbing),
            )
            record.result = result
            record.progress = 100
            record.current_stage = "done"
            record.state = JobState.DEGRADED if record.warnings else JobState.SUCCEEDED
        except TimeoutError as exc:
            record.state = JobState.TIMED_OUT
            record.error = str(exc)
        except Exception as exc:
            record.state = JobState.FAILED
            record.error = str(exc)
        finally:
            await self._cancel_pending(tasks)
            await self.admission.release()
            await self.store.put(record)
            await self.events.publish(
                record.job_id,
                JobEvent(
                    event_type=record.state.value,
                    stage=record.current_stage,
                    progress=record.progress,
                    message=record.error or ("; ".join(record.warnings)),
                    data={
                        "result": (
                            record.result.model_dump(mode="json")
                            if record.result is not None
                            else None
                        ),
                        "elapsed_seconds": round(deadline.elapsed, 3),
                    },
                ),
            )

    @staticmethod
    def _single_voice_decisions(lines) -> list[SpeakerDecision]:
        return [
            SpeakerDecision(
                line_id=line.line_id,
                character_id="default",
                confidence=0.0,
                needs_review=True,
            )
            for line in lines
        ]
