import httpx

from cineflow.caption_worker import CaptionWorkerSettings, create_app
from cineflow.models import SubtitleLine, Transcript


class FakeExtractor:
    configured = True

    async def extract(self, request):
        return Transcript(
            language=request.source_language,
            provider="fake_cloud_ocr",
            task_id="ocr-job",
            lines=[
                SubtitleLine(
                    line_id=1,
                    start_ms=0,
                    end_ms=1000,
                    text="字幕",
                    source="ocr",
                )
            ],
        )


async def test_caption_worker_reports_no_local_ocr_and_returns_transcript():
    app = create_app(
        CaptionWorkerSettings(bearer_token="secret"),
        extractor=FakeExtractor(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["local_ocr"] is False

        denied = await client.post("/v1/extract", json={})
        assert denied.status_code == 401

        response = await client.post(
            "/v1/extract",
            headers={"Authorization": "Bearer secret"},
            json={
                "input_url": "https://example.com/original.mp4",
                "clean_video_url": "https://example.com/clean.mp4",
                "probe": {
                    "duration_seconds": 10,
                    "input_bytes": 1000,
                    "width": 1280,
                    "height": 720,
                    "fps": 25,
                },
                "target_language": "en-US",
            },
        )
        assert response.status_code == 200
        assert response.json()["provider"] == "fake_cloud_ocr"
        assert response.json()["lines"][0]["source"] == "ocr"
