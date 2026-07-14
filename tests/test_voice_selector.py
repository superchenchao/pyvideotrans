import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox

from videotrans import tts
from videotrans.component.voice_selector import (
    VoiceSelectorDialog,
    VoiceSelectorEventFilter,
)


def _app():
    return QApplication.instance() or QApplication([])


def test_dialog_search_filters_current_channel_roles():
    _app()
    dialog = VoiceSelectorDialog(
        tts_type=tts.AZURE_TTS,
        language="es",
        roles=["No", "Elena(Female)", "Ximena Multilingual(Female)"],
        current_role="Elena(Female)",
    )

    dialog.search_input.setText("multilingual")

    assert [entry.role for entry in dialog.filtered_entries] == [
        "Ximena Multilingual(Female)"
    ]


def test_combo_adapter_preserves_item_data_as_role_value():
    _app()
    combo = QComboBox()
    combo.addItem("Ximena（女声 / 西班牙）", "Ximena(Female/ES)")
    combo.addItem("Victor（男声 / 波多黎各）", "Victor(Male/PR)")
    event_filter = VoiceSelectorEventFilter(
        combo,
        tts_type_getter=lambda: tts.EDGE_TTS,
        language_getter=lambda: "es",
    )

    assert event_filter._combo_roles() == [
        "Ximena(Female/ES)",
        "Victor(Male/PR)",
    ]

    event_filter._select_role("Victor(Male/PR)")
    assert combo.currentData() == "Victor(Male/PR)"


def test_dialog_pages_large_channel_but_searches_all_roles():
    _app()
    roles = [f"Voice {index}(Female)" for index in range(200)]
    dialog = VoiceSelectorDialog(
        tts_type=tts.OPENAI_TTS,
        language="en",
        roles=roles,
    )

    assert dialog.voice_list.count() == dialog.PAGE_SIZE
    assert not dialog.load_more_button.isHidden()

    dialog.search_input.setText("voice 199")

    assert [entry.role for entry in dialog.filtered_entries] == [
        "Voice 199(Female)"
    ]
    assert dialog.voice_list.count() == 1


def test_dialog_handles_empty_current_channel():
    _app()
    dialog = VoiceSelectorDialog(
        tts_type=tts.AZURE_TTS,
        language="es",
        roles=[],
    )

    assert dialog.filtered_entries == []
    assert dialog.voice_list.count() == 0
    assert not dialog.empty_label.isHidden()
    assert dialog.load_more_button.isHidden()


def test_unconfigured_channel_clears_previous_channel_roles(monkeypatch):
    _app()
    from videotrans.mainwin._actions import WinAction

    class FakeMain:
        def __init__(self):
            self.voice_role = QComboBox()
            self.voice_role.addItems(["Previous Voice A", "Previous Voice B"])
            self.current_rolelist = ["Previous Voice A", "Previous Voice B"]

    monkeypatch.setattr(tts, "is_input_api", lambda **kwargs: False)
    main = FakeMain()

    WinAction(main=main).tts_type_change(tts.OPENAI_TTS)

    assert main.current_rolelist == ["No"]
    assert [main.voice_role.itemText(index) for index in range(main.voice_role.count())] == [
        "No"
    ]
