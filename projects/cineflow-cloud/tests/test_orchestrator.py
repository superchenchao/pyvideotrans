import asyncio

from cineflow.config import Settings
from cineflow.models import (
    DubbingArtifact,
    DubbingClip,
    JobRequest,
    JobState,
    VideoProbe,
)
from cineflow.orchestrator import JobStore, PipelineOrchestrator
from cineflow.providers.demo import DemoProviders
from cineflow.sla import AdmissionController


async def wait_for_terminal(store, job_id):
    for _ in range(100):
        job = await store.get(job_id)
        if job and job.state in {
            JobState.SUCCEEDED,
            JobState.DEGRADED,
            JobState.FAILED,
        }:
            return job
        await asyncio.sleep(0.01)
    raise AssertionError("job did not finish")


async def test_demo_pipeline_completes_and_preserves_multispeaker_mapping():
    settings = Settings(max_inflight_jobs=1)
    store = JobStore()
    orchestrator = PipelineOrchestrator(
        DemoProviders(delay_scale=0), AdmissionController(settings), store
    )
    request = JobRequest(
        input_url="https://example.com/input.mp4",
        clean_video_url="https://example.com/already-cleaned.mp4",
        probe=VideoProbe(duration_seconds=120, input_bytes=20_000_000),
        target_language="en-US",
        target_voice="en-US-AvaMultilingualNeural",
    )
    accepted = await orchestrator.submit(request)
    completed = await wait_for_terminal(store, accepted.job_id)
    assert completed.state == JobState.SUCCEEDED
    assert completed.result is not None
    assert completed.target_exceeded is False
    assert completed.request.translation_engine == "deepseek"
    assert [item.character_id for item in completed.decisions] == [
        "character_001",
        "character_002",
        "character_001",
    ]


class OverflowProviders(DemoProviders):
    async def synthesize(self, request, translated, decisions):
        by_line = {item.line_id: item for item in decisions}
        return DubbingArtifact(
            clips=[
                DubbingClip(
                    line_id=line.line_id,
                    character_id=by_line[line.line_id].character_id,
                    audio_url=f"memory://line-{line.line_id}.wav",
                    duration_ms=1800 if line.line_id == 2 else 900,
                    target_duration_ms=1000,
                    rate_percent=55 if line.line_id == 2 else 0,
                    timing_overflow_ms=800 if line.line_id == 2 else 0,
                    within_target=line.line_id != 2,
                    output_format="riff-24khz-16bit-mono-pcm",
                )
                for line in translated.lines
            ]
        )


async def test_unresolved_azure_timing_overflow_is_preserved_and_reported():
    settings = Settings(max_inflight_jobs=1)
    store = JobStore()
    orchestrator = PipelineOrchestrator(
        OverflowProviders(delay_scale=0),
        AdmissionController(settings),
        store,
    )
    request = JobRequest(
        input_url="https://example.com/input.mp4",
        probe=VideoProbe(duration_seconds=3, input_bytes=1_000_000),
        target_language="en-US",
        target_voice="en-US-AvaMultilingualNeural",
    )

    accepted = await orchestrator.submit(request)
    completed = await wait_for_terminal(store, accepted.job_id)
    events = await orchestrator.events.history(accepted.job_id)

    assert completed.state == JobState.DEGRADED
    assert any(warning.startswith("azure tts timing overflow") for warning in completed.warnings)
    timing_event = next(event for event in events if event.event_type == "timing_warning")
    assert timing_event.data["line_ids"] == [2]
    assert timing_event.data["max_overflow_ms"] == 800
