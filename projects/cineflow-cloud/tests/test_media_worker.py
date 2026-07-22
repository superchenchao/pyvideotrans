from __future__ import annotations

import base64
from types import SimpleNamespace

import httpx

from cineflow.media_worker import MediaWorkerSettings, create_app
from cineflow.models import MediaArtifacts, OutputArtifact


class FakeMediaService:
    ice = SimpleNamespace(configured=True)
    store = SimpleNamespace(configured=True)

    async def prepare(self, request):
        return MediaArtifacts(
            video_url=str(request.clean_video_url or request.input_url),
            source_audio_url=str(request.source_audio_url) if request.source_audio_url else None,
            provider="fake_media",
        )

    async def save_artifact(self, name, content):
        assert content == b"audio"
        return {
            "url": f"https://oss.example/{name}",
            "download_url": f"https://oss.example/{name}?signed=1",
            "oss_uri": f"oss://bucket/{name}",
            "object_key": name,
        }

    async def assemble(self, job, media, translated, dubbing):
        assert translated.lines[0].line_id == dubbing.clips[0].line_id
        return OutputArtifact(
            video_url="https://oss.example/result.mp4?signed=1",
            subtitle_url="https://oss.example/en-US.srt?signed=1",
            provider="fake_media",
            task_id="fake-job",
        )


JOB = {
    "input_url": "https://oss.example/source.mp4",
    "clean_video_url": "https://oss.example/clean.mp4",
    "source_audio_url": "https://oss.example/source.wav",
    "probe": {
        "duration_seconds": 60,
        "input_bytes": 10_000_000,
        "width": 1920,
        "height": 1080,
    },
    "source_language": "zh-CN",
    "target_language": "en-US",
    "target_voice": "en-US-AvaMultilingualNeural",
}


async def test_media_worker_prepare_artifact_and_assemble_contract():
    app = create_app(
        MediaWorkerSettings(
            bearer_token="secret",
            aliyun_access_key_id="key",
            aliyun_access_key_secret="secret-key",
            oss_bucket="bucket",
        ),
        service=FakeMediaService(),
    )
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer secret"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["warm"] is True

        unauthorized = await client.post("/v1/prepare", json=JOB)
        assert unauthorized.status_code == 401

        prepared = await client.post("/v1/prepare", json=JOB, headers=headers)
        assert prepared.status_code == 200
        assert prepared.json()["video_url"] == "https://oss.example/clean.mp4"

        artifact = await client.post(
            "/v1/artifacts/base64",
            json={
                "name": "line-1.mp3",
                "content_base64": base64.b64encode(b"audio").decode("ascii"),
            },
            headers=headers,
        )
        assert artifact.status_code == 200
        assert artifact.json()["oss_uri"] == "oss://bucket/line-1.mp3"

        assembled = await client.post(
            "/v1/assemble",
            json={
                "job": JOB,
                "media": prepared.json(),
                "translated": {
                    "language": "en-US",
                    "lines": [
                        {
                            "line_id": 1,
                            "start_ms": 0,
                            "end_ms": 1000,
                            "text": "Hello.",
                        }
                    ],
                },
                "dubbing": {
                    "clips": [
                        {
                            "line_id": 1,
                            "character_id": "character_001",
                            "audio_url": artifact.json()["url"],
                        }
                    ]
                },
            },
            headers=headers,
        )
        assert assembled.status_code == 200
        assert assembled.json()["provider"] == "fake_media"
        assert assembled.json()["task_id"] == "fake-job"
