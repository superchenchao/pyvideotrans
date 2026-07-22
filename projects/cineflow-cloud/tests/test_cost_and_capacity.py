import asyncio

import pytest

from cineflow.config import Settings
from cineflow.models import JobRequest, ProviderHealth, VideoProbe
from cineflow.sla import AdmissionController, AdmissionRejected, ProviderUnavailable


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
    assert "subtitle_removal" not in quote.breakdown


def test_cost_guard_rejects_job_before_billing():
    controller = AdmissionController(Settings())
    with pytest.raises(AdmissionRejected, match="exceeds budget"):
        controller.quote(request_for(max_cost_cny=0.1))


def test_cold_speaker_worker_warns_but_does_not_reject():
    controller = AdmissionController(Settings())
    health = [
        ProviderHealth(name="media", healthy=True, warm=True),
        ProviderHealth(name="asr", healthy=True, warm=True),
        ProviderHealth(name="speaker", healthy=True, warm=False),
        ProviderHealth(name="deepseek", healthy=True, warm=True),
        ProviderHealth(name="azure_tts", healthy=True, warm=True),
    ]
    warnings = controller.validate_provider_health(request_for(), health)
    assert warnings
    assert "may miss" in warnings[0]


def test_unhealthy_mandatory_provider_is_rejected():
    controller = AdmissionController(Settings())
    health = [
        ProviderHealth(name="media", healthy=True),
        ProviderHealth(name="asr", healthy=False),
        ProviderHealth(name="speaker", healthy=True),
        ProviderHealth(name="deepseek", healthy=True),
        ProviderHealth(name="azure_tts", healthy=True),
    ]
    with pytest.raises(ProviderUnavailable, match="asr"):
        controller.validate_provider_health(request_for(), health)


async def test_capacity_waits_instead_of_rejecting_a_valid_job():
    controller = AdmissionController(Settings(max_inflight_jobs=1))
    await controller.acquire()

    waiting = asyncio.create_task(controller.acquire())
    await asyncio.sleep(0)
    capacity = await controller.capacity()
    assert capacity["inflight"] == 1
    assert capacity["queued"] == 1
    assert waiting.done() is False

    await controller.release()
    wait_seconds = await asyncio.wait_for(waiting, timeout=0.1)
    assert wait_seconds >= 0
    await controller.release()
