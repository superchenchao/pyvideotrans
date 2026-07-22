import asyncio

import pytest

from cineflow.config import Settings
from cineflow.models import JobRequest, ProviderHealth, VideoProbe
from cineflow.sla import (
    AdmissionController,
    AdmissionRejected,
    CapacityUnavailable,
    ProviderUnavailable,
)


def request_for(**updates):
    values = {
        "input_url": "https://example.com/input.mp4",
        "probe": VideoProbe(duration_seconds=300, input_bytes=250_000_000),
        "source_language": "zh-CN",
        "target_language": "en-US",
        "target_voice": "en-US-AvaMultilingualNeural",
        "max_cost_cny": 5.0,
    }
    values.update(updates)
    return JobRequest(**values)


def test_cost_guard_accepts_five_minute_default_profile():
    controller = AdmissionController(Settings())
    _, quote = controller.quote(request_for())
    assert quote.total_cny < 5.0
    assert quote.breakdown["azure_tts"] > 0


def test_cost_guard_rejects_job_before_billing():
    controller = AdmissionController(Settings())
    with pytest.raises(AdmissionRejected, match="exceeds budget"):
        controller.quote(request_for(max_cost_cny=0.1))


def test_strict_sla_rejects_cold_speaker_worker():
    controller = AdmissionController(Settings())
    health = [
        ProviderHealth(name="media", healthy=True, warm=True),
        ProviderHealth(name="asr", healthy=True, warm=True),
        ProviderHealth(name="speaker", healthy=True, warm=False),
        ProviderHealth(name="deepseek", healthy=True, warm=True),
        ProviderHealth(name="azure_tts", healthy=True, warm=True),
    ]
    with pytest.raises(ProviderUnavailable, match="pre-warmed"):
        controller.validate_provider_health(request_for(), health)


async def test_capacity_gate_never_queues_an_accepted_job():
    controller = AdmissionController(Settings(max_inflight_jobs=1))
    await controller.acquire_nowait()
    with pytest.raises(CapacityUnavailable):
        await controller.acquire_nowait()
    await controller.release()
    await asyncio.wait_for(controller.acquire_nowait(), timeout=0.1)
    await controller.release()
