import asyncio

import httpx

from cineflow.api import app

PAYLOAD = {
    "input_url": "https://example.com/input.mp4",
    "clean_video_url": "https://example.com/already-cleaned.mp4",
    "probe": {
        "duration_seconds": 60,
        "input_bytes": 10_000_000,
        "codec": "h264",
        "width": 1280,
        "height": 720,
        "fps": 25,
    },
    "source_language": "zh-CN",
    "target_language": "en-US",
    "target_voice": "en-US-AvaMultilingualNeural",
    "max_cost_cny": 5,
}


async def test_admission_job_and_sse_flow():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        quote_response = await client.post("/v1/admission", json=PAYLOAD)
        assert quote_response.status_code == 200
        quote = quote_response.json()
        assert quote["accepted"] is True
        assert quote["target_seconds"] == 300
        assert quote["estimated_cost_cny"] > 0

        submit = await client.post("/v1/jobs", json=PAYLOAD)
        assert submit.status_code == 202
        submitted = submit.json()
        assert submitted["request"]["translation_engine"] == "deepseek"
        job_id = submitted["job_id"]

        record = None
        for _ in range(100):
            response = await client.get(f"/v1/jobs/{job_id}")
            record = response.json()
            if record["state"] in {"succeeded", "degraded", "failed"}:
                break
            await asyncio.sleep(0.01)
        assert record is not None
        assert record["state"] == "succeeded"
        assert record["estimated_cost_cny"] > 0
        assert record["target_exceeded"] is False

        events = await client.get(f"/v1/jobs/{job_id}/events")
        assert events.status_code == 200
        assert "event: accepted" in events.text
        assert "event: succeeded" in events.text
