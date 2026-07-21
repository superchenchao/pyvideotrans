import re
import xml.etree.ElementTree as ET

import pytest
from tenacity import RetryError

from videotrans.tts import _azuretts


SSML_NAMESPACE = "http://www.w3.org/2001/10/synthesis"


def test_build_azure_ssml_is_compact_and_preserves_configuration():
    text = 'Hello  世界, "Azure"!'
    ssml = _azuretts.build_azure_ssml(
        language="zh-CN",
        voice_name="zh-CN-YunjianNeural",
        rate="+18%",
        pitch="-6Hz",
        volume="+9%",
        text=text,
    )

    assert "\n" not in ssml
    assert re.search(r">\s+<", ssml) is None

    root = ET.fromstring(ssml)
    voice = root.find(f"{{{SSML_NAMESPACE}}}voice")
    prosodies = root.findall(f".//{{{SSML_NAMESPACE}}}prosody")

    assert root.attrib["{http://www.w3.org/XML/1998/namespace}lang"] == "zh-CN"
    assert voice is not None
    assert voice.attrib["name"] == "zh-CN-YunjianNeural"
    assert len(prosodies) == 2
    assert [prosody.attrib for prosody in prosodies] == [
        {"rate": "+18%", "pitch": "-6Hz", "volume": "+9%"},
        {"rate": "+18%", "pitch": "-6Hz", "volume": "+9%"},
    ]
    assert prosodies[1].text == text


@pytest.mark.parametrize(
    "text",
    [
        "",
        "'single' and \"double\" quotes",
        "A & B < C > D",
        "中文  and English",
        "第一行  保留空格\nSecond line  keeps spaces",
    ],
)
def test_build_azure_ssml_preserves_subtitle_text_semantics(text):
    ssml = _azuretts.build_azure_ssml(
        language="en-US",
        voice_name="en-US-TestNeural",
        rate="+0%",
        pitch="+0Hz",
        volume="+0%",
        text=text,
    )

    root = ET.fromstring(ssml)
    prosodies = root.findall(f".//{{{SSML_NAMESPACE}}}prosody")

    assert (prosodies[1].text or "") == text
    assert root.text is None
    assert root[0].text is None
    assert prosodies[0].text is None


def test_build_azure_ssml_does_not_double_escape_existing_xml_entities():
    ssml = _azuretts.build_azure_ssml(
        language="en-US",
        voice_name="en-US-TestNeural",
        rate="+0%",
        pitch="+0Hz",
        volume="+0%",
        text=(
            "A &amp; B &lt; C &gt; D &quot;Q&quot; "
            "&apos;S&apos; &#33; &#x3F;"
        ),
    )

    assert "&amp;amp;" not in ssml
    assert "&amp;lt;" not in ssml
    assert "&amp;gt;" not in ssml
    root = ET.fromstring(ssml)
    prosodies = root.findall(f".//{{{SSML_NAMESPACE}}}prosody")
    assert prosodies[1].text == 'A & B < C > D "Q" \'S\' ! ?'


def test_build_azure_ssml_keeps_invalid_numeric_entity_as_literal_text():
    ssml = _azuretts.build_azure_ssml(
        language="en-US",
        voice_name="en-US-TestNeural",
        rate="+0%",
        pitch="+0Hz",
        volume="+0%",
        text="Invalid XML entities: &#0; and &#xD800;",
    )

    root = ET.fromstring(ssml)
    prosodies = root.findall(f".//{{{SSML_NAMESPACE}}}prosody")
    assert prosodies[1].text == "Invalid XML entities: &#0; and &#xD800;"


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
