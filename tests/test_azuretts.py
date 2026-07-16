import pytest
from tenacity import RetryError

from videotrans.tts import _azuretts


def test_create_speech_config_uses_region(monkeypatch):
    captured = {}

    def fake_speech_config(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(_azuretts.speechsdk, "SpeechConfig", fake_speech_config)

    _azuretts.create_speech_config(" key ", " WestUs3 ")

    assert captured == {"subscription": "key", "region": "WestUs3"}


def test_create_speech_config_uses_endpoint_for_url(monkeypatch):
    captured = {}

    def fake_speech_config(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(_azuretts.speechsdk, "SpeechConfig", fake_speech_config)

    _azuretts.create_speech_config(
        " key ",
        " https://westus3.api.cognitive.microsoft.com/ ",
    )

    assert captured == {
        "subscription": "key",
        "endpoint": "https://westus3.api.cognitive.microsoft.com/",
    }


def test_resolve_voice_name_maps_display_name(monkeypatch):
    monkeypatch.setattr(
        _azuretts.tools,
        "get_azure_rolelist",
        lambda language, role_name: "zh-CN-YunjianNeural",
    )

    assert _azuretts.resolve_voice_name("zh", "Yunjian(Male)") == (
        "zh-CN-YunjianNeural"
    )


def test_resolve_voice_name_preserves_voice_id_when_lookup_misses(monkeypatch):
    monkeypatch.setattr(
        _azuretts.tools,
        "get_azure_rolelist",
        lambda language, role_name: None,
    )

    assert _azuretts.resolve_voice_name("zh", "zh-CN-YunjianNeural") == (
        "zh-CN-YunjianNeural"
    )


def test_resolve_voice_name_accepts_cached_edge_display_name(monkeypatch):
    monkeypatch.setattr(
        _azuretts.tools,
        "get_azure_rolelist",
        lambda language, role_name: None,
    )
    monkeypatch.setattr(
        _azuretts.tools,
        "get_edge_rolelist",
        lambda role_name, locale: "es-ES-XimenaNeural",
    )

    assert _azuretts.resolve_voice_name("es", "Ximena(Female/ES)") == (
        "es-ES-XimenaNeural"
    )


def test_azure_always_has_at_least_one_retry(monkeypatch):
    monkeypatch.setitem(_azuretts.settings, "retry_nums", 1)

    assert _azuretts.azure_retry_attempts() == 2


def test_describe_cancellation_marks_transient_errors_retryable():
    details = type("Details", (), {
        "error_code": type("Code", (), {"name": "ServiceTimeout"})(),
        "error_details": "request timed out",
        "reason": "Error",
    })()

    message, permanent = _azuretts.describe_cancellation(details)

    assert "ServiceTimeout" in message
    assert "request timed out" in message
    assert permanent is False


def test_describe_cancellation_does_not_retry_authentication_errors():
    details = type("Details", (), {
        "error_code": type("Code", (), {"name": "AuthenticationFailure"})(),
        "error_details": "invalid subscription key",
        "reason": "Error",
    })()

    message, permanent = _azuretts.describe_cancellation(details)

    assert "AuthenticationFailure" in message
    assert permanent is True


def test_transient_cancellation_is_retried_once(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(_azuretts.logger, "disabled", True)

    class FakeSpeechConfig:
        def set_speech_synthesis_output_format(self, output_format):
            self.output_format = output_format

    class FakeAsyncResult:
        def get(self):
            calls.append("synthesize")
            return type("Result", (), {
                "reason": _azuretts.speechsdk.ResultReason.Canceled,
                "cancellation_details": type("Details", (), {
                    "error_code": type("Code", (), {"name": "ServiceTimeout"})(),
                    "error_details": "request timed out",
                    "reason": "Error",
                })(),
            })()

    class FakeSynthesizer:
        def speak_ssml_async(self, ssml):
            return FakeAsyncResult()

    monkeypatch.setattr(_azuretts, "create_speech_config", lambda *args: FakeSpeechConfig())
    monkeypatch.setattr(_azuretts, "resolve_voice_name", lambda *args: "ja-JP-TestVoice")
    monkeypatch.setattr(_azuretts.speechsdk.audio, "AudioOutputConfig", lambda **kwargs: object())
    monkeypatch.setattr(_azuretts.speechsdk, "SpeechSynthesizer", lambda **kwargs: FakeSynthesizer())
    monkeypatch.setattr(_azuretts.AzureTTS._run.retry, "sleep", lambda seconds: None)

    item = {
        "text": "テスト",
        "role": "TestVoice",
        "line": 18,
        "filename": str(tmp_path / "line-18.wav"),
    }
    tts = _azuretts.AzureTTS(queue_tts=[item], language="ja")

    with pytest.raises(RetryError):
        tts._run(item, 0)

    assert calls == ["synthesize", "synthesize"]
