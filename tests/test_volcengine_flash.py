import json
from pathlib import Path

import requests

from videotrans.configure.config import params
from videotrans.recognition import volcengine_flash
from videotrans.recognition import _zijiemodel


def test_build_headers_prefers_new_console_api_key(monkeypatch):
    values = {
        "zijierecognmodel_apikey": "new-key",
        "zijierecognmodel_appid": "old-app",
        "zijierecognmodel_token": "old-token",
    }
    monkeypatch.setattr(params, "get", lambda key, default=None: values.get(key, default))

    headers = volcengine_flash.build_headers("request-id")

    assert headers["X-Api-Key"] == "new-key"
    assert "X-Api-App-Key" not in headers
    assert headers["X-Api-Resource-Id"] == "volc.bigasr.auc_turbo"


def test_build_headers_supports_old_console_credentials(monkeypatch):
    values = {
        "zijierecognmodel_apikey": "",
        "zijierecognmodel_appid": "old-app",
        "zijierecognmodel_token": "old-token",
    }
    monkeypatch.setattr(params, "get", lambda key, default=None: values.get(key, default))

    headers = volcengine_flash.build_headers("request-id")

    assert headers["X-Api-App-Key"] == "old-app"
    assert headers["X-Api-Access-Key"] == "old-token"
    assert "X-Api-Key" not in headers


def test_extract_and_map_speakers_by_time_overlap():
    response = {
        "result": {
            "utterances": [
                {
                    "start_time": 0,
                    "end_time": 900,
                    "text": "第一句",
                    "additions": {"speaker": "speaker_7"},
                },
                {
                    "start_time": 900,
                    "end_time": 2200,
                    "text": "第二句",
                    "additions": {"speaker": 3},
                },
            ]
        }
    }
    utterances = volcengine_flash.extract_utterances(response)

    labels = volcengine_flash.map_speakers_to_subtitles(
        [[0, 1000], [1000, 2000]], utterances
    )

    assert labels == ["spk0", "spk1"]
    assert utterances[0]["speaker"] == "spk7"


def test_prepare_large_wav_for_upload_compresses_and_reuses_mp3(
        tmp_path, monkeypatch
):
    audio_file = tmp_path / "long.wav"
    audio_file.write_bytes(b"w" * (1024 * 1024 + 1))
    ffmpeg_calls = []

    def fake_runffmpeg(args, **kwargs):
        ffmpeg_calls.append((args, kwargs))
        Path(args[-1]).write_bytes(b"compressed-mp3")
        return True

    monkeypatch.setattr(volcengine_flash.tools, "runffmpeg", fake_runffmpeg)

    prepared = volcengine_flash.prepare_audio_for_upload(audio_file)
    reused = volcengine_flash.prepare_audio_for_upload(audio_file)

    assert prepared.suffix == ".mp3"
    assert prepared.read_bytes() == b"compressed-mp3"
    assert reused == prepared
    assert len(ffmpeg_calls) == 1
    assert ffmpeg_calls[0][0][ffmpeg_calls[0][0].index("-ar") + 1] == "16000"
    assert ffmpeg_calls[0][0][ffmpeg_calls[0][0].index("-b:a") + 1] == "64k"


def test_request_transcription_uses_direct_session_and_long_upload_timeout(
        tmp_path, monkeypatch
):
    audio_file = tmp_path / "voice.wav"
    audio_file.write_bytes(b"audio")
    calls = []

    class FakeResponse:
        headers = {
            "X-Api-Status-Code": "20000000",
            "X-Tt-Logid": "trace-id",
        }

        def raise_for_status(self):
            return None

        def json(self):
            return {"result": {"utterances": []}}

    class FakeSession:
        def __init__(self):
            self.trust_env = True

        def post(self, url, **kwargs):
            calls.append((self.trust_env, url, kwargs))
            return FakeResponse()

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(volcengine_flash.requests, "Session", FakeSession)
    monkeypatch.setattr(
        volcengine_flash,
        "build_headers",
        lambda _request_id: {"X-Api-Key": "test-key"},
    )

    response, trace_id = volcengine_flash.request_transcription(audio_file.as_posix())

    assert response == {"result": {"utterances": []}}
    assert trace_id == "trace-id"
    assert calls[0][0] is False
    assert calls[0][2]["timeout"] == (60, 120)
    assert calls[0][2]["json"]["audio"]["format"] == "wav"
    assert calls[-1] == "closed"


def test_request_transcription_retries_write_timeout(tmp_path, monkeypatch):
    audio_file = tmp_path / "voice.wav"
    audio_file.write_bytes(b"audio")
    attempts = []
    sleeps = []

    class FakeResponse:
        headers = {
            "X-Api-Status-Code": "20000000",
            "X-Tt-Logid": "trace-after-retry",
        }

        def raise_for_status(self):
            return None

        def json(self):
            return {"result": {"utterances": []}}

    class FakeSession:
        trust_env = True

        def post(self, _url, **_kwargs):
            attempts.append(1)
            if len(attempts) < 3:
                raise requests.ConnectionError(
                    "('Connection aborted.', TimeoutError('The write operation timed out'))"
                )
            return FakeResponse()

        def close(self):
            return None

    monkeypatch.setattr(volcengine_flash.requests, "Session", FakeSession)
    monkeypatch.setattr(
        volcengine_flash,
        "build_headers",
        lambda _request_id: {"X-Api-Key": "test-key"},
    )
    monkeypatch.setattr(volcengine_flash.time, "sleep", sleeps.append)

    _response, trace_id = volcengine_flash.request_transcription(audio_file.as_posix())

    assert trace_id == "trace-after-retry"
    assert len(attempts) == 3
    assert sleeps == [1.5, 3.0]


def test_volcengine_speaker_provider_writes_cache_and_diagnostics(
        tmp_path, monkeypatch
):
    subtitles_file = tmp_path / "subtitles.json"
    speaker_file = tmp_path / "speaker.json"
    audio_file = tmp_path / "vocal.wav"
    subtitles_file.write_text(json.dumps([[0, 1000], [1000, 2000]]), encoding="utf-8")
    audio_file.write_bytes(b"audio")
    response = {
        "result": {
            "utterances": [
                {"start_time": 0, "end_time": 1000, "text": "甲", "additions": {"speaker": 4}},
                {"start_time": 1000, "end_time": 2000, "text": "乙", "additions": {"speaker": 8}},
            ]
        }
    }
    monkeypatch.setattr(
        volcengine_flash,
        "request_transcription",
        lambda _audio: (response, "trace-id"),
    )

    ok, error = volcengine_flash.volcengine_flash_speakers(
        input_file=audio_file.as_posix(),
        subtitles_file=subtitles_file.as_posix(),
        speak_file=speaker_file.as_posix(),
    )

    assert ok is True
    assert error is None
    assert json.loads(speaker_file.read_text(encoding="utf-8")) == ["spk0", "spk1"]
    diagnostic = json.loads(
        (tmp_path / "speaker.volcengine.json").read_text(encoding="utf-8")
    )
    assert diagnostic["provider"] == "volcengine_flash"
    assert diagnostic["trace_id"] == "trace-id"


def test_zijie_recogn_exec_writes_returned_speaker_cache(tmp_path, monkeypatch):
    response = {
        "result": {
            "utterances": [
                {
                    "start_time": 0,
                    "end_time": 1000,
                    "text": "第一句",
                    "additions": {"speaker": 2},
                },
                {
                    "start_time": 1000,
                    "end_time": 2000,
                    "text": "第二句",
                    "additions": {"speaker": 7},
                },
            ]
        }
    }
    monkeypatch.setattr(
        _zijiemodel,
        "request_transcription",
        lambda _audio_file: (response, "trace-from-exec"),
    )

    recognizer = _zijiemodel.ZijieRecogn.__new__(_zijiemodel.ZijieRecogn)
    recognizer.audio_file = (tmp_path / "voice.wav").as_posix()
    recognizer.cache_folder = tmp_path.as_posix()
    recognizer._exit = lambda: False
    recognizer.signal = lambda **_kwargs: None

    subtitles = recognizer._exec()

    assert [item.text for item in subtitles] == ["第一句", "第二句"]
    assert json.loads((tmp_path / "speaker.json").read_text(encoding="utf-8")) == [
        "spk2",
        "spk7",
    ]
    diagnostic = json.loads(
        (tmp_path / "speaker.volcengine.json").read_text(encoding="utf-8")
    )
    assert diagnostic == {
        "provider": "volcengine_flash",
        "trace_id": "trace-from-exec",
        "speaker_count": 2,
    }
