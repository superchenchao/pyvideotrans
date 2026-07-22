import asyncio

from cineflow.config import Settings
from cineflow.models import JobRequest, JobState, VideoProbe
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
