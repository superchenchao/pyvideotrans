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
