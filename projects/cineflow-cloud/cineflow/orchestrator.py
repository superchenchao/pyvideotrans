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
from .sla import AdmissionController, TargetTimer

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
    # These are performance checkpoints, not cancellation deadlines.
    PREPARE_TARGET = 130.0
    ASR_TARGET = 60.0
    SPEAKER_AND_TRANSLATION_TARGET = 170.0
    TTS_TARGET = 245.0
    ASSEMBLY_TARGET = 290.0

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
        warnings = self.admission.validate_provider_health(request, health)

        target = self.admission.settings.target_processing_seconds
        likely_within_target = predicted <= target
        if not likely_within_target:
            warnings.append(
                f"predicted p95 is {predicted:.1f}s; processing will still continue past "
                f"the {target}s optimization target if necessary"
            )

        job_id = uuid.uuid4().hex
        record = JobRecord(
            job_id=job_id,
            state=JobState.ACCEPTED,
            accepted_at_monotonic=time.monotonic(),
            target_seconds=target,
            request=request,
            predicted_seconds=round(predicted, 2),
            likely_within_target=likely_within_target,
            estimated_cost_cny=cost.total_cny,
            cost_breakdown=cost.breakdown,
            warnings=warnings,
        )
        await self.store.put(record)
        await self.events.publish(
            job_id,
            JobEvent(
                event_type="accepted",
                stage="accepted",
                progress=0,
                message=("job accepted; 300 seconds is an optimization target, not a hard timeout"),
                data={
                    "predicted_seconds": record.predicted_seconds,
                    "target_seconds": record.target_seconds,
                    "likely_within_target": record.likely_within_target,
                    "estimated_cost_cny": record.estimated_cost_cny,
                },
            ),
        )
        asyncio.create_task(self._run(record), name=f"cineflow:{job_id}")
        return record

    async def _stage(
        self,
        record: JobRecord,
        timer: TargetTimer,
        name: str,
        target_checkpoint: float,
        awaitable: Awaitable[T],
    ) -> T:
        record.current_stage = name
        await self.events.publish(
            record.job_id,
            JobEvent(
                event_type="stage_started",
                stage=name,
                progress=record.progress,
            ),
        )
        started = time.monotonic()
        try:
            result = await awaitable
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
        checkpoint_exceeded = timer.elapsed > target_checkpoint
        record.metrics.append(StageMetric(stage=name, elapsed_seconds=elapsed))
        await self.events.publish(
            record.job_id,
            JobEvent(
                event_type="stage_completed",
                stage=name,
                progress=record.progress,
                data={
                    "elapsed_seconds": elapsed,
                    "target_checkpoint_seconds": target_checkpoint,
                    "target_checkpoint_exceeded": checkpoint_exceeded,
                },
            ),
        )
        if checkpoint_exceeded:
            await self.events.publish(
                record.job_id,
                JobEvent(
                    event_type="performance_warning",
                    stage=name,
                    progress=record.progress,
                    message=(
                        f"{name} completed after its target checkpoint; the job continues normally"
                    ),
                    data={"total_elapsed_seconds": round(timer.elapsed, 3)},
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

    async def _report_timing_overflow(
        self,
        record: JobRecord,
        dubbing: DubbingArtifact,
    ) -> None:
        overflow = [clip for clip in dubbing.clips if not clip.within_target]
        if not overflow:
            return
        line_ids = [clip.line_id for clip in overflow]
        max_overflow = max(clip.timing_overflow_ms for clip in overflow)
        record.warnings.append(
            f"azure tts timing overflow remains on {len(overflow)} line(s); "
            f"maximum overflow is {max_overflow}ms"
        )
        await self.events.publish(
            record.job_id,
            JobEvent(
                event_type="timing_warning",
                stage="azure_tts",
                progress=record.progress,
                message=(
                    "Azure prosody fitting reached its configured limit; "
                    "audio is preserved and final assembly continues"
                ),
                data={
                    "line_ids": line_ids,
                    "max_overflow_ms": max_overflow,
                },
            ),
        )

    async def _run(self, record: JobRecord) -> None:
        timer = TargetTimer(
            record.target_seconds,
            started=record.accepted_at_monotonic,
        )
        tasks: set[asyncio.Task[object]] = set()
        slot_acquired = False

        capacity = await self.admission.capacity()
        if capacity["available"] <= 0:
            record.state = JobState.QUEUED
            record.current_stage = "queued"
            await self.store.put(record)
            await self.events.publish(
                record.job_id,
                JobEvent(
                    event_type="queued",
                    stage="queued",
                    progress=0,
                    message="waiting for a processing slot; the job is not rejected",
                    data=capacity,
                ),
            )

        try:
            record.queue_wait_seconds = round(await self.admission.acquire(), 3)
            slot_acquired = True
            record.processing_started_at_monotonic = time.monotonic()
            record.state = JobState.RUNNING
            record.current_stage = "starting"
            await self.store.put(record)
            await self.events.publish(
                record.job_id,
                JobEvent(
                    event_type="running",
                    stage="starting",
                    progress=0,
                    data={"queue_wait_seconds": record.queue_wait_seconds},
                ),
            )

            media_task = asyncio.create_task(
                self._stage(
                    record,
                    timer,
                    "prepare_media",
                    self.PREPARE_TARGET,
                    self.providers.prepare_media(record.request),
                )
            )
            tasks.add(media_task)

            transcript = await self._stage(
                record,
                timer,
                "asr",
                self.ASR_TARGET,
                self.providers.transcribe(record.request),
            )
            record.progress = 25

            speaker_task = asyncio.create_task(
                self._stage(
                    record,
                    timer,
                    "multimodal_speaker",
                    self.SPEAKER_AND_TRANSLATION_TARGET,
                    self.providers.analyze_speakers(record.request, transcript),
                )
            )
            translation_task = asyncio.create_task(
                self._stage(
                    record,
                    timer,
                    "deepseek_translation",
                    self.SPEAKER_AND_TRANSLATION_TARGET,
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
                    record.warnings.append(f"speaker fusion degraded to one Azure voice: {exc}")
                    decisions = self._single_voice_decisions(transcript.lines)
            else:
                speaker_task.cancel()
                decisions = self._single_voice_decisions(transcript.lines)

            record.decisions = decisions
            record.progress = 55

            dubbing: DubbingArtifact = await self._stage(
                record,
                timer,
                "azure_tts",
                self.TTS_TARGET,
                self.providers.synthesize(
                    record.request,
                    translated,
                    decisions,
                ),
            )
            record.progress = 78
            await self._report_timing_overflow(record, dubbing)

            try:
                media = await media_task
            except Exception as exc:
                record.warnings.append(f"media preparation failed; upstream video retained: {exc}")
                fallback_video = record.request.clean_video_url or record.request.input_url
                media = MediaArtifacts(
                    video_url=str(fallback_video),
                    source_audio_url=(
                        str(record.request.source_audio_url)
                        if record.request.source_audio_url is not None
                        else None
                    ),
                    degraded_features=["media_prepare"],
                )

            result: OutputArtifact = await self._stage(
                record,
                timer,
                "assemble",
                self.ASSEMBLY_TARGET,
                self.providers.assemble(
                    record.request,
                    media,
                    translated,
                    dubbing,
                ),
            )
            record.result = result
            record.progress = 100
            record.current_stage = "done"
            quality_degraded = any(
                warning.startswith(
                    (
                        "speaker fusion",
                        "media preparation",
                        "azure tts timing",
                    )
                )
                for warning in record.warnings
            )
            record.state = JobState.DEGRADED if quality_degraded else JobState.SUCCEEDED
        except Exception as exc:
            record.state = JobState.FAILED
            record.error = str(exc)
        finally:
            await self._cancel_pending(tasks)
            if slot_acquired:
                await self.admission.release()
            record.elapsed_seconds = round(timer.elapsed, 3)
            record.target_exceeded = timer.exceeded
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
                        "elapsed_seconds": record.elapsed_seconds,
                        "target_seconds": record.target_seconds,
                        "target_exceeded": record.target_exceeded,
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
