from __future__ import annotations

import wave
from io import BytesIO

import httpx

from cineflow.models import (
    JobRequest,
    SpeakerDecision,
    SubtitleLine,
    Transcript,
    VideoProbe,
)
from cineflow.providers.azure_tts import (
    AZURE_PCM_OUTPUT_FORMAT,
    AzureSpeechEndpoint,
    AzureSynthesisResult,
    AzureTTSClient,
    wav_duration_ms,
)
from cineflow.providers.http_bundle import ProductionProviders


def make_wav(duration_ms: int, sample_rate: int = 24000) -> bytes:
    frames = max(1, round(sample_rate * duration_ms / 1000))
    output = BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(b"\x00\x00" * frames)
    return output.getvalue()


def test_wav_duration_and_ssml_are_well_formed():
    assert wav_duration_ms(make_wav(1234)) == 1234

    ssml = AzureTTSClient.build_ssml(
        "en-US",
        'Voice "A"',
        "A & B < C",
        rate="+50%",
    )
    assert ssml.endswith("</speak>")
    assert not ssml.endswith('"')
    assert 'rate="+50%"' in ssml
    assert 'name="Voice &quot;A&quot;"' in ssml
    assert "A &amp; B &lt; C" in ssml


async def test_azure_retries_with_bounded_rate_until_clip_fits_slot():
    bodies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8")
        bodies.append(body)
        assert request.headers["X-Microsoft-OutputFormat"] == AZURE_PCM_OUTPUT_FORMAT
        if 'rate="+0%"' in body:
            return httpx.Response(200, content=make_wav(2000))
        if 'rate="+50%"' in body:
            return httpx.Response(200, content=make_wav(1050))
        raise AssertionError(body)

    client = AzureTTSClient(
        AzureSpeechEndpoint("key", "eastasia"),
        concurrency=2,
        max_fit_rate_percent=50,
        duration_tolerance_ratio=1.08,
        max_fit_attempts=2,
        transport=httpx.MockTransport(handler),
    )

    result = await client.synthesize_for_slot(
        "en-US",
        "en-US-AvaMultilingualNeural",
        "This is a deliberately long subtitle line.",
        1000,
    )

    assert len(bodies) == 2
    assert result.duration_ms == 1050
    assert result.target_duration_ms == 1000
    assert result.rate_percent == 50
    assert result.attempts == 2
    assert result.overflow_ms == 50
    assert result.within_target is True
    assert result.extension == ".wav"


async def test_azure_reports_residual_overflow_without_clipping_audio():
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8")
        duration = 1400 if 'rate="+30%"' in body else 2000
        return httpx.Response(200, content=make_wav(duration))

    client = AzureTTSClient(
        AzureSpeechEndpoint("key", "eastasia"),
        max_fit_rate_percent=30,
        duration_tolerance_ratio=1.0,
        max_fit_attempts=2,
        transport=httpx.MockTransport(handler),
    )

    result = await client.synthesize_for_slot(
        "en-US",
        "en-US-AvaMultilingualNeural",
        "Long line",
        1000,
    )

    assert result.duration_ms == 1400
    assert result.rate_percent == 30
    assert result.overflow_ms == 400
    assert result.within_target is False
    assert wav_duration_ms(result.audio) == 1400


async def test_azure_uses_secondary_region_when_primary_rejects_request():
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host.startswith("primary."):
            return httpx.Response(400, text="primary unavailable")
        return httpx.Response(200, content=make_wav(800))

    client = AzureTTSClient(
        AzureSpeechEndpoint("primary-key", "primary"),
        AzureSpeechEndpoint("secondary-key", "secondary"),
        transport=httpx.MockTransport(handler),
    )

    audio = await client.synthesize(
        "en-US",
        "en-US-AvaMultilingualNeural",
        "Hello",
    )

    assert wav_duration_ms(audio) == 800
    assert hosts == [
        "primary.tts.speech.microsoft.com",
        "secondary.tts.speech.microsoft.com",
    ]


class FakeAzure:
    async def synthesize_for_slot(self, language, voice, text, target_duration_ms):
        assert language == "en-US"
        assert voice == "en-US-AvaMultilingualNeural"
        assert text == "Hello."
        assert target_duration_ms == 1000
        return AzureSynthesisResult(
            audio=make_wav(960),
            duration_ms=960,
            target_duration_ms=1000,
            rate_percent=20,
            attempts=2,
            overflow_ms=0,
            within_target=True,
        )


class FakeMedia:
    def __init__(self):
        self.calls = []

    async def post(self, path, payload, timeout):
        self.calls.append((path, payload, timeout))
        return {
            "url": "https://oss.example/line-1.wav",
            "download_url": "https://oss.example/line-1.wav?signed=1",
            "object_key": "artifacts/line-1.wav",
        }


async def test_provider_uploads_measured_wav_and_preserves_timing_metadata():
    providers = ProductionProviders.__new__(ProductionProviders)
    providers.azure = FakeAzure()
    providers.media = FakeMedia()

    request = JobRequest(
        input_url="https://oss.example/source.mp4",
        probe=VideoProbe(duration_seconds=5, input_bytes=1_000_000),
        target_language="en-US",
        target_voice="en-US-AvaMultilingualNeural",
    )
    translated = Transcript(
        language="en-US",
        lines=[
            SubtitleLine(
                line_id=1,
                start_ms=500,
                end_ms=1500,
                text="Hello.",
            )
        ],
    )
    decisions = [
        SpeakerDecision(
            line_id=1,
            character_id="character_001",
            confidence=0.95,
        )
    ]

    dubbing = await providers.synthesize(request, translated, decisions)

    path, payload, timeout = providers.media.calls[0]
    assert path == "/v1/artifacts/base64"
    assert payload["name"] == "line-1.wav"
    assert timeout == 20.0
    clip = dubbing.clips[0]
    assert clip.duration_ms == 960
    assert clip.target_duration_ms == 1000
    assert clip.rate_percent == 20
    assert clip.timing_overflow_ms == 0
    assert clip.within_target is True
    assert clip.output_format == AZURE_PCM_OUTPUT_FORMAT
    assert clip.metadata["attempts"] == 2
    assert clip.metadata["artifact_object_key"] == "artifacts/line-1.wav"
