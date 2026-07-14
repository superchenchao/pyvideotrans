from videotrans import tts
from videotrans.util.voice_catalog import (
    build_voice_entries,
    get_voice_favorites,
    set_voice_favorite,
)


def test_azure_catalog_preserves_role_and_resolves_voice_id():
    entries = build_voice_entries(
        tts.AZURE_TTS,
        ["No", "Ximena Multilingual(Female)"],
        "es",
    )

    assert entries[0].previewable is False
    assert entries[1].role == "Ximena Multilingual(Female)"
    assert entries[1].voice_id == "es-ES-XimenaMultilingualNeural"
    assert entries[1].gender == "女声"
    assert "多语言" in entries[1].tags


def test_azure_catalog_reuses_metadata_by_voice_id():
    entries = build_voice_entries(
        tts.AZURE_TTS,
        ["Elena(Female)"],
        "es",
    )

    assert entries[0].voice_id == "es-AR-ElenaNeural"
    assert entries[0].age == "成年"
    assert entries[0].region == "阿根廷"
    assert "成年" in entries[0].tags


def test_edge_catalog_uses_rich_metadata():
    entries = build_voice_entries(
        tts.EDGE_TTS,
        ["Ximena(Female/ES)"],
        "es",
    )

    assert entries[0].voice_id == "es-ES-XimenaNeural"
    assert entries[0].gender == "女声"
    assert entries[0].age == "成年"
    assert entries[0].region == "西班牙"


def test_clone_voice_is_visible_but_not_previewable():
    entries = build_voice_entries(
        tts.CLONE_VOICE_TTS,
        ["clone"],
        "zh-cn",
    )

    assert entries[0].role == "clone"
    assert entries[0].previewable is False
    assert "克隆音色" in entries[0].tags


def test_favorites_are_isolated_by_channel(monkeypatch):
    from videotrans.util import voice_catalog

    monkeypatch.setattr(voice_catalog.params, "voice_favorites", {})
    monkeypatch.setattr(voice_catalog.params, "save", lambda: None)

    set_voice_favorite(tts.AZURE_TTS, "Azure Voice", True)
    set_voice_favorite(tts.EDGE_TTS, "Edge Voice", True)

    assert get_voice_favorites(tts.AZURE_TTS) == {"Azure Voice"}
    assert get_voice_favorites(tts.EDGE_TTS) == {"Edge Voice"}

    set_voice_favorite(tts.AZURE_TTS, "Azure Voice", False)
    assert get_voice_favorites(tts.AZURE_TTS) == set()
    assert get_voice_favorites(tts.EDGE_TTS) == {"Edge Voice"}
