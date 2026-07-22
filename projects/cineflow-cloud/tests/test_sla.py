import pytest

from cineflow.config import Settings
from cineflow.models import JobRequest, VideoProbe
from cineflow.sla import AdmissionController, AdmissionRejected


def request_for(**probe_updates):
    probe = {
        "duration_seconds": 300,
        "input_bytes": 300_000_000,
        "codec": "h264",
        "width": 1920,
        "height": 1080,
        "fps": 25,
        **probe_updates,
    }
    return JobRequest(
        input_url="https://example.com/input.mp4",
        probe=VideoProbe(**probe),
        source_language="zh-CN",
        target_language="en-US",
        target_voice="en-US-AvaMultilingualNeural",
    )


def test_five_minute_job_is_admitted_with_reserve():
    controller = AdmissionController(Settings())
    predicted = controller.estimate(request_for())
    assert predicted <= 280


def test_oversized_input_is_rejected_before_billing():
    settings = Settings(max_input_bytes=100_000_000)
    controller = AdmissionController(settings)
    with pytest.raises(AdmissionRejected, match="too large"):
        controller.estimate(request_for(input_bytes=100_000_001))


def test_4k_input_is_rejected_in_strict_sla_mode():
    controller = AdmissionController(Settings())
    with pytest.raises(AdmissionRejected, match="1080p"):
        controller.estimate(request_for(width=3840, height=2160))
