import json
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QListWidget, QPushButton
from videotrans.component.onlyone_set_role import (
    CharacterPickerDialog,
    SpeakerAssignmentDialog,
)
from videotrans.process.series_speakers import (
    empty_manifest,
    manifest_path_for_series,
    register_episode,
    save_manifest,
)


class _ParentStub:
    width = 1280
    height = 800

    def activateWindow(self):
        pass


def _srt(rows):
    blocks = []
    for index, text in enumerate(rows, start=1):
        blocks.append(
            f"{index}\n00:00:0{index},000 --> 00:00:0{index},900\n{text}"
        )
    return "\n\n".join(blocks)


def test_series_dialog_uses_bilingual_layout_and_confirmed_character_name(tmp_path):
    app = QApplication.instance() or QApplication([])
    series_dir = tmp_path / "drama"
    output_dir = tmp_path / "output"
    cache_dir = tmp_path / "cache"
    series_dir.mkdir()
    output_dir.mkdir()
    cache_dir.mkdir()
    video_path = series_dir / "01.mp4"
    video_path.write_bytes(b"")
    source_sub = tmp_path / "source.srt"
    target_sub = tmp_path / "target.srt"
    source_sub.write_text(_srt(["家庭不顾", "爸爸"]), encoding="utf-8")
    target_sub.write_text(_srt(["この愛情", "パパ"]), encoding="utf-8")
    (cache_dir / "speaker.json").write_text(
        json.dumps(["spk0", "spk1"]), encoding="utf-8"
    )
    (cache_dir / "speaker_roles.json").write_text(
        json.dumps({
            "profile_version": 2,
            "target_language_code": "ja",
            "tts_type": 0,
            "speaker_profiles": {
                "spk0": {"gender": "female"},
                "spk1": {"gender": "male"},
            },
            "speaker_to_voice": {"spk0": "VoiceA", "spk1": "VoiceB"},
        }),
        encoding="utf-8",
    )
    manifest = empty_manifest(series_dir.as_posix())
    mapping = register_episode(
        manifest,
        episode_key="01.mp4",
        episode_name="01.mp4",
        speaker_profiles={
            "spk0": {"embedding": [1.0, 0.0], "sample_count": 2},
            "spk1": {"embedding": [0.0, 1.0], "sample_count": 2},
        },
        speaker_counts={"spk0": 1, "spk1": 1},
        speaker_to_voice={"spk0": "VoiceA", "spk1": "VoiceB"},
    )
    first_character = mapping["spk0"]["character_id"]
    for character in manifest["characters"]:
        if character["id"] == first_character:
            character["name"] = "沈轻"
            character["name_confirmed"] = True
    save_manifest(
        manifest_path_for_series(output_dir, series_dir, [video_path]), manifest
    )

    dialog = SpeakerAssignmentDialog(
        parent=_ParentStub(),
        target_sub=target_sub.as_posix(),
        source_sub=source_sub.as_posix(),
        all_voices=["No", "VoiceA", "VoiceB"],
        cache_folder=cache_dir.as_posix(),
        target_language="ja",
        source_language="zh-cn",
        video_path=video_path.as_posix(),
        series_folder=series_dir.as_posix(),
        series_output_dir=output_dir.as_posix(),
    )
    dialog.show()
    dialog.load_table()
    for _ in range(5):
        app.processEvents()

    assert dialog.table.columnCount() == 6
    assert dialog.table.horizontalHeaderItem(3).text() == "原文"
    assert dialog.table.horizontalHeaderItem(4).text() == "原音"
    assert dialog.table.horizontalHeaderItem(5).text() == "译文（可编辑）"
    assert dialog.table.item(0, 2).text() == ""
    assert dialog.table.cellWidget(0, 2).text() == "沈轻  ▾"
    assert dialog.table.item(0, 3).text() == "家庭不顾"
    assert dialog.table.cellWidget(0, 4).objectName() == "sourceAudioButton"
    assert dialog.table.item(0, 5).text() == "この愛情"
    assert not bool(dialog.table.item(0, 3).flags() & Qt.ItemIsEditable)
    assert bool(dialog.table.item(0, 5).flags() & Qt.ItemIsEditable)
    assert not dialog.findChildren(QPushButton, "targetAudioButton")
    assert not dialog.series_scope_button.isVisible()
    assert dialog.episode_scope_button.text() == "当前视频 2"
    assert dialog.series_status_label.text() == "已识别当前视频 · 2 个角色"
    assert not dialog.identity_actions_widget.isVisible()
    visible_labels = "\n".join(
        label.text() for label in dialog.findChildren(QLabel) if label.isVisible()
    )
    assert "全片" not in visible_labels
    assert "全剧" not in visible_labels
    assert "跨集" not in visible_labels
    assert "女 · 1句 · 50%" in visible_labels
    assert "男 · 1句 · 50%" in visible_labels

    voice_buttons = [
        button
        for button in dialog.findChildren(QPushButton, "characterVoiceButton")
        if button.isVisible()
    ]
    assert len(voice_buttons) == 2
    assert all(button.toolTip() for button in voice_buttons)
    assert len([
        button for button in dialog.findChildren(
            QPushButton, "characterVoicePreviewButton"
        ) if button.isVisible()
    ]) == 2

    second_character = mapping["spk1"]["character_id"]
    dialog._assign_row_character(0, second_character)
    assert dialog.speaker_list_sub[0] == "spk1"
    assert "角色 2" in dialog.table.cellWidget(0, 2).text()

    dialog._apply_voice_to_sidebar_key(
        ("character", second_character), "VoiceA"
    )
    assert dialog.speakers["spk1"] == "VoiceA"

    dialog.cancel_and_close()


def test_dubbing_review_reuses_role_layout_and_adds_target_audio(
        tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    source_sub = tmp_path / "source.srt"
    target_sub = tmp_path / "target.srt"
    source_audio = tmp_path / "source.wav"
    source_audio.write_bytes(b"source audio")
    source_sub.write_text(_srt(["原文一", "原文二"]), encoding="utf-8")
    target_sub.write_text(_srt(["Target one", "Target two"]), encoding="utf-8")
    (cache_dir / "speaker.json").write_text(
        json.dumps(["spk0", "spk1"]), encoding="utf-8"
    )
    (cache_dir / "speaker_roles.json").write_text(
        json.dumps({
            "profile_version": 2,
            "target_language_code": "en",
            "tts_type": 0,
            "speaker_profiles": {
                "spk0": {"gender": "female"},
                "spk1": {"gender": "male"},
            },
            "speaker_to_voice": {
                "spk0": "VoiceA(Female)",
                "spk1": "VoiceB(Male)",
            },
        }),
        encoding="utf-8",
    )
    queue = [
        {
            "line": 1, "text": "Target one", "role": "VoiceA(Female)",
            "filename": (cache_dir / "line1.wav").as_posix(),
        },
        {
            "line": 2, "text": "Target two", "role": "VoiceB(Male)",
            "filename": (cache_dir / "line2.wav").as_posix(),
        },
    ]
    for item in queue:
        Path(item["filename"]).write_bytes(b"dubbing audio")
    task = SimpleNamespace(review_dubbing_dirty=False)
    dialog = SpeakerAssignmentDialog(
        parent=_ParentStub(),
        target_sub=target_sub.as_posix(),
        source_sub=source_sub.as_posix(),
        source_audio=source_audio.as_posix(),
        all_voices=["No", "VoiceA(Female)", "VoiceB(Male)"],
        cache_folder=cache_dir.as_posix(),
        target_language="en",
        source_language="zh-cn",
        dubbing_review=True,
        dubbing_queue=queue,
        dubbing_task=task,
    )
    dialog.show()
    dialog.load_table()
    for _ in range(5):
        app.processEvents()

    assert dialog.table.columnCount() == 7
    assert dialog.table.horizontalHeaderItem(6).text() == "译音"
    assert len(dialog.findChildren(QPushButton, "sourceAudioButton")) == 2
    assert len(dialog.findChildren(QPushButton, "targetAudioButton")) == 2

    listened_roles = []
    monkeypatch.setattr(
        dialog,
        "_listen_role_value",
        lambda role, text, button, reset_text:
            listened_roles.append((role, text, reset_text)),
    )
    dialog._listen_sidebar_voice(
        ("speaker", "spk0"),
        dialog.findChildren(QPushButton, "characterVoicePreviewButton")[-2],
    )
    assert listened_roles == [("VoiceA(Female)", "Target one", "试听")]

    played = []
    monkeypatch.setattr(
        dialog,
        "_play_audio_file",
        lambda filename, button, **options:
            played.append((filename, button.objectName(), options)),
    )
    dialog._play_original_row(0)
    dialog._play_translated_row(0)

    assert played[0] == (
        source_audio.as_posix(),
        "sourceAudioButton",
        {"start_ms": 1000, "end_ms": 1900},
    )
    assert played[1][0] == queue[0]["filename"]
    assert played[1][1] == "targetAudioButton"

    dialog.table.item(0, dialog.target_text_column).setText("Changed target")
    dialog.save_and_close()

    assert task.review_dubbing_dirty is True


def test_single_video_character_picker_has_no_redundant_series_tab():
    app = QApplication.instance() or QApplication([])
    options = [
        {"character_id": f"character_{index:03d}", "name": f"角色 {index}", "percent": 25}
        for index in range(1, 5)
    ]
    picker = CharacterPickerDialog(
        episode_options=options,
        series_options=[],
        current_id="character_001",
        show_series_scope=False,
    )
    picker.show()
    app.processEvents()

    assert picker.tabs is None
    assert picker.height() < 220
    assert picker.findChild(QListWidget).count() == 4

    picker.reject()
