import base64
import json

import httpx

from cineflow.models import JobRequest, VideoProbe
from cineflow.providers.aliyun_asr import AliyunFunASRClient, AliyunFunASRConfig
from cineflow.providers.volcengine_asr import (
    VolcengineASRConfig,
    VolcengineFlashASRClient,
)


def job_request(**updates):
    values = {
        "input_url": "https://oss.example/source.mp4",
        "source_audio_url": "https://oss.example/source.wav",
        "probe": VideoProbe(duration_seconds=60, input_bytes=10_000_000),
        "source_language": "zh-CN",
        "target_language": "en-US",
        "target_voice": "en-US-AvaMultilingualNeural",
        "expected_speakers": 2,
    }
    values.update(updates)
    return JobRequest(**values)


async def test_aliyun_fun_asr_submits_polls_and_preserves_speakers_and_words():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "POST":
            payload = json.loads(request.content)
            assert payload["input"]["file_urls"] == ["https://oss.example/source.wav"]
            assert payload["parameters"]["diarization_enabled"] is True
            assert payload["parameters"]["speaker_count"] == 2
            assert request.headers["X-DashScope-Async"] == "enable"
            return httpx.Response(
                200,
                json={"output": {"task_id": "task-aliyun"}},
            )
        if request.url.host == "result.example":
            return httpx.Response(
                200,
                json={
                    "transcripts": [
                        {
                            "sentences": [
                                {
                                    "begin_time": 0,
                                    "end_time": 1200,
                                    "text": "你好。",
                                    "speaker_id": 1,
                                    "words": [
                                        {
                                            "begin_time": 0,
                                            "end_time": 500,
                                            "text": "你好",
                                            "punctuation": "。",
                                        }
                                    ],
                                }
                            ]
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "request_id": "request-aliyun",
                "output": {
                    "task_status": "SUCCEEDED",
                    "results": [
                        {
                            "subtask_status": "SUCCEEDED",
                            "transcription_url": "https://result.example/transcript.json",
                        }
                    ],
                },
                "usage": {"duration": 1.2},
            },
        )

    client = AliyunFunASRClient(
        AliyunFunASRConfig(
            api_key="aliyun-key",
            base_url="https://dashscope.test",
            poll_interval_seconds=0.01,
        ),
        transport=httpx.MockTransport(handler),
    )
    transcript = await client.transcribe(job_request())

    assert transcript.provider == "aliyun_fun_asr"
    assert transcript.task_id == "task-aliyun"
    assert transcript.usage_seconds == 1.2
    assert transcript.lines[0].speaker_id == "spk1"
    assert transcript.lines[0].words[0].text == "你好"
    assert len(calls) == 3


async def test_volcengine_flash_normalizes_utterance_speakers():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        payload = json.loads(request.content)
        captured["payload"] = payload
        return httpx.Response(
            200,
            headers={
                "X-Api-Status-Code": "20000000",
                "X-Tt-Logid": "trace-volc",
            },
            json={
                "result": {
                    "utterances": [
                        {
                            "start_time": 0,
                            "end_time": 900,
                            "text": "你好。",
                            "additions": {"speaker": "speaker_3"},
                        }
                    ]
                }
            },
        )

    client = VolcengineFlashASRClient(
        VolcengineASRConfig(api_key="volc-key"),
        transport=httpx.MockTransport(handler),
    )
    transcript = await client.transcribe_audio_bytes(
        job_request(),
        b"audio-bytes",
        audio_format="mp3",
    )

    assert captured["headers"]["x-api-key"] == "volc-key"
    assert captured["payload"]["request"]["enable_speaker_info"] is True
    encoded = captured["payload"]["audio"]["data"]
    assert base64.b64decode(encoded) == b"audio-bytes"
    assert transcript.provider == "volcengine_bigmodel_flash"
    assert transcript.metadata["trace_id"] == "trace-volc"
    assert transcript.lines[0].speaker_id == "spk3"
