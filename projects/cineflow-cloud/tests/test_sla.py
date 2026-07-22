import pytest

from cineflow.config import Settings
from cineflow.models import JobRequest, VideoProbe
from cineflow.sla import AdmissionController, AdmissionRejected, P95Profile


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


def test_five_minute_job_has_a_realistic_soft_target_prediction():
    controller = AdmissionController(Settings())
    predicted = controller.estimate(request_for())
    assert predicted <= 300


def test_prediction_over_300_seconds_does_not_reject_the_job():
    controller = AdmissionController(
        Settings(),
        p95=P95Profile(fixed_overhead=400),
    )
    predicted, quote = controller.quote(request_for())
    assert predicted > 300
    assert quote.total_cny > 0


def test_oversized_input_is_rejected_before_billing():
    settings = Settings(max_input_bytes=100_000_000)
    controller = AdmissionController(settings)
    with pytest.raises(AdmissionRejected, match="too large"):
        controller.estimate(request_for(input_bytes=100_000_001))


def test_4k_input_is_estimated_instead_of_rejected_for_missing_the_target():
    controller = AdmissionController(Settings())
    predicted = controller.estimate(request_for(width=3840, height=2160))
    assert predicted > 0
