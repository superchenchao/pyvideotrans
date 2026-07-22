import httpx

from cineflow.asr_worker import ASRWorkerSettings, create_app
from cineflow.models import SubtitleLine, Transcript


class FakeASRProvider:
    configured = True
    ready = True

    def health_detail(self):
        return "fake provider ready"

    async def transcribe(self, request):
        return Transcript(
            language=request.source_language,
            provider="fake_asr",
            lines=[
                SubtitleLine(
                    line_id=1,
                    start_ms=0,
                    end_ms=1000,
                    text="你好。",
                    speaker_id="spk0",
                )
            ],
        )


async def test_asr_worker_health_and_transcription_contract():
    app = create_app(
        ASRWorkerSettings(backend="aliyun", max_concurrency=2),
        provider=FakeASRProvider(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        health = await client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["backend"] == "aliyun"
        assert health.json()["warm"] is True

        response = await client.post(
            "/v1/transcribe",
            json={
                "input_url": "https://oss.example/input.mp4",
                "probe": {
                    "duration_seconds": 60,
                    "input_bytes": 1000000,
                },
                "source_language": "zh-CN",
                "target_language": "en-US",
                "target_voice": "en-US-AvaMultilingualNeural",
            },
        )
        assert response.status_code == 200
        transcript = response.json()
        assert transcript["provider"] == "fake_asr"
        assert transcript["lines"][0]["speaker_id"] == "spk0"
