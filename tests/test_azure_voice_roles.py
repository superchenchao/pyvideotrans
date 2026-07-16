import requests

from videotrans import tts
from videotrans.util import help_role


def _voice(display, short_name, locale, gender, secondary=None):
    data = {
        "DisplayName": display,
        "ShortName": short_name,
        "Locale": locale,
        "Gender": gender,
    }
    if secondary is not None:
        data["SecondaryLocaleList"] = secondary
    return data


def test_azure_voice_list_url_supports_region_and_endpoint():
    expected = (
        "https://westus3.tts.speech.microsoft.com/"
        "cognitiveservices/voices/list"
    )

    assert help_role._azure_voice_list_url("WestUs3") == expected
    assert help_role._azure_voice_list_url(
        "https://westus3.api.cognitive.microsoft.com/"
    ) == expected


def test_supported_roles_include_primary_and_secondary_locale(monkeypatch):
    voices = [
        _voice("Nanami", "ja-JP-NanamiNeural", "ja-JP", "Female"),
        _voice(
            "Andrew Multilingual",
            "en-US-AndrewMultilingualNeural",
            "en-US",
            "Male",
            ["de-DE", "ja-JP"],
        ),
        _voice(
            "Single Secondary",
            "en-US-SingleSecondaryNeural",
            "en-US",
            "Female",
            "ja-JP",
        ),
        _voice("Jenny", "en-US-JennyNeural", "en-US", "Female"),
    ]
    monkeypatch.setattr(help_role.params, "azure_speech_key", "test-key")
    monkeypatch.setattr(help_role.params, "azure_speech_region", "westus3")
    monkeypatch.setattr(help_role, "_get_azure_region_voices", lambda *args: voices)

    roles = help_role.get_azure_supported_rolelist("ja")

    assert roles == {
        "No": "No",
        "Nanami(Female)": "ja-JP-NanamiNeural",
        "Andrew Multilingual(Male)": "en-US-AndrewMultilingualNeural",
        "Single Secondary(Female)": "en-US-SingleSecondaryNeural",
    }


def test_supported_roles_match_full_locale_exactly(monkeypatch):
    voices = [
        _voice("Mexico", "es-MX-VoiceNeural", "es-MX", "Female"),
        _voice("Spain", "es-ES-VoiceNeural", "es-ES", "Female"),
    ]
    monkeypatch.setattr(help_role.params, "azure_speech_key", "test-key")
    monkeypatch.setattr(help_role.params, "azure_speech_region", "westus3")
    monkeypatch.setattr(help_role, "_get_azure_region_voices", lambda *args: voices)

    roles = help_role.get_azure_supported_rolelist("es-MX")

    assert list(roles) == ["No", "Mexico(Female)"]


def test_supported_roles_fall_back_to_local_catalog(monkeypatch):
    monkeypatch.setattr(help_role.params, "azure_speech_key", "test-key")
    monkeypatch.setattr(help_role.params, "azure_speech_region", "westus3")
    monkeypatch.setattr(
        help_role,
        "_get_azure_region_voices",
        lambda *args: (_ for _ in ()).throw(requests.Timeout("slow")),
    )

    roles = help_role.get_azure_supported_rolelist("ja")

    assert roles["No"] == "No"
    assert roles["Nanami(Female)"] == "ja-JP-NanamiNeural"


def test_role_menu_uses_supported_azure_catalog(monkeypatch):
    monkeypatch.setattr(
        help_role,
        "get_azure_supported_rolelist",
        lambda language: {"No": "No", "Multilingual(Male)": "voice-id"},
    )

    assert help_role.role_menu(tts.AZURE_TTS, "ja") == [
        "No",
        "Multilingual(Male)",
    ]
