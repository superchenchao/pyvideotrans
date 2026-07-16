import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from videotrans.configure.excepts import VideoTransError
from videotrans.configure.config import settings
from videotrans.recognition import FASTER_WHISPER
from videotrans.subtitle_removal.automation import normalize_rect, scale_normalized_rect
from videotrans.task.trans_create import TransCreate
from videotrans.util import tools


def test_normalized_rect_scales_across_matching_resolutions():
    normalized = normalize_rect((108, 1200, 864, 180), 1080, 1920)

    assert scale_normalized_rect(
        normalized,
        720,
        1280,
        reference_aspect_ratio=1080 / 1920,
    ) == (72, 800, 576, 120)


def test_normalized_rect_rejects_different_aspect_ratio():
    with pytest.raises(ValueError, match="does not match"):
        scale_normalized_rect(
            [0.1, 0.6, 0.8, 0.1],
            1920,
            1080,
            reference_aspect_ratio=1080 / 1920,
        )


def test_prepare_clean_visual_source_uses_scaled_batch_rect(tmp_path, monkeypatch):
    input_file = tmp_path / "input.mp4"
    input_file.write_bytes(b"video")
    task = object.__new__(TransCreate)
    task.cfg = SimpleNamespace(
        remove_burned_subtitles=True,
        subtitle_removal_rect=[0.1, 0.625, 0.8, 0.09375],
        subtitle_removal_aspect_ratio=1080 / 1920,
        cache_folder=tmp_path.as_posix(),
        name=input_file.as_posix(),
    )
    task.video_info = {"width": 720, "height": 1280, "time": 120000}
    task.visual_source = task.cfg.name
    task.signal = lambda **kwargs: None
    task._exit = lambda: False
    calls = []

    def fake_remove(**kwargs):
        calls.append(kwargs)
        return kwargs["output_file"]

    monkeypatch.setattr("videotrans.subtitle_removal.remove_burned_subtitles", fake_remove)
    monkeypatch.setattr(tools, "vail_file", lambda path: False)

    task._prepare_clean_visual_source()

    assert calls[0]["input_file"] == task.cfg.name
    assert calls[0]["rect"] == (72, 800, 576, 120)
    assert calls[0]["duration_ms"] == 120000
    assert task.visual_source.endswith("source-without-burned-subtitles.mp4")


def test_changed_batch_rect_does_not_reuse_stale_clean_video(tmp_path, monkeypatch):
    input_file = tmp_path / "input.mp4"
    input_file.write_bytes(b"video")
    clean_file = tmp_path / "source-without-burned-subtitles.mp4"
    clean_file.write_bytes(b"old-clean-video")
    Path(f"{clean_file}.json").write_text(
        '{"rect": [1, 2, 3, 4]}',
        encoding="utf-8",
    )
    task = object.__new__(TransCreate)
    task.cfg = SimpleNamespace(
        remove_burned_subtitles=True,
        subtitle_removal_rect=[0.1, 0.625, 0.8, 0.09375],
        subtitle_removal_aspect_ratio=1080 / 1920,
        cache_folder=tmp_path.as_posix(),
        name=input_file.as_posix(),
    )
    task.video_info = {"width": 720, "height": 1280, "time": 120000}
    task.visual_source = input_file.as_posix()
    task.signal = lambda **kwargs: None
    task._exit = lambda: False
    calls = []

    def fake_remove(**kwargs):
        calls.append(kwargs)
        return kwargs["output_file"]

    monkeypatch.setattr("videotrans.subtitle_removal.remove_burned_subtitles", fake_remove)

    task._prepare_clean_visual_source()

    assert len(calls) == 1
    metadata = Path(f"{clean_file}.json").read_text(encoding="utf-8")
    assert '"rect": [' in metadata
    assert "72" in metadata and "800" in metadata


def test_prepare_clean_visual_source_fails_this_video_on_ratio_mismatch(tmp_path):
    task = object.__new__(TransCreate)
    task.cfg = SimpleNamespace(
        remove_burned_subtitles=True,
        subtitle_removal_rect=[0.1, 0.6, 0.8, 0.1],
        subtitle_removal_aspect_ratio=1080 / 1920,
        cache_folder=tmp_path.as_posix(),
        name=(tmp_path / "input.mp4").as_posix(),
    )
    task.video_info = {"width": 1920, "height": 1080, "time": 120000}
    task.visual_source = task.cfg.name

    with pytest.raises(VideoTransError, match="画面比例"):
        task._prepare_clean_visual_source()


def test_final_visual_split_uses_cleaned_video(tmp_path, monkeypatch):
    task = object.__new__(TransCreate)
    task.cfg = SimpleNamespace(
        name=(tmp_path / "original.mp4").as_posix(),
        novoice_mp4=(tmp_path / "novoice.mp4").as_posix(),
        cache_folder=tmp_path.as_posix(),
    )
    task.visual_source = (tmp_path / "cleaned.mp4").as_posix()
    task.is_copy_video = True
    task.uuid = "test-visual-source"
    calls = []
    monkeypatch.setattr(tools, "runffmpeg", lambda cmd, **kwargs: calls.append(cmd) or True)
    monkeypatch.setattr(settings, "get", lambda key, default=None: default)

    task._split_novoice_byraw()

    assert calls[0][calls[0].index("-i") + 1] == task.visual_source


def test_burned_subtitle_ocr_keeps_reading_original_video(tmp_path, monkeypatch):
    original = tmp_path / "original.mp4"
    original.write_bytes(b"video")
    task = object.__new__(TransCreate)
    task.cfg = SimpleNamespace(
        name=original.as_posix(),
        cache_folder=tmp_path.as_posix(),
        recogn_type=FASTER_WHISPER,
        detect_language="zh-cn",
        subtitle_removal_rect=[0.1, 0.625, 0.8, 0.09375],
        subtitle_removal_aspect_ratio=1080 / 1920,
    )
    task.video_info = {"width": 1080, "height": 1920}
    task.visual_source = (tmp_path / "cleaned.mp4").as_posix()
    task.is_audio_trans = False
    task.signal = lambda **kwargs: None
    task._exit = lambda: False
    captured = []
    raw = [{"line": 1, "start_time": 0, "end_time": 1000, "text": "对白"}]

    def fake_extract(input_file, *args, **kwargs):
        captured.append((input_file, kwargs.get("normalized_rect")))
        return raw

    monkeypatch.setattr(settings, "get", lambda key, default=None: True)
    monkeypatch.setattr(tools, "vail_file", lambda path: Path(path).is_file())
    monkeypatch.setattr("videotrans.subtitle_ocr.extract_burned_subtitles", fake_extract)
    monkeypatch.setattr(
        "videotrans.subtitle_ocr.fuse_ocr_with_asr",
        lambda asr, ocr: (asr, {"matched": 1}),
    )

    assert task._fuse_burned_subtitles(raw) == raw
    assert captured == [(
        original.as_posix(),
        [0.1, 0.625, 0.8, 0.09375],
    )]


def test_burned_subtitle_ocr_reuses_saved_rect_even_when_clear_cache_is_enabled(
        tmp_path, monkeypatch):
    original = tmp_path / "original.mp4"
    original.write_bytes(b"video")
    task = object.__new__(TransCreate)
    task.cfg = SimpleNamespace(
        name=original.as_posix(),
        cache_folder=tmp_path.as_posix(),
        recogn_type=FASTER_WHISPER,
        detect_language="zh-cn",
        subtitle_removal_rect=None,
        subtitle_removal_aspect_ratio=0.0,
        clear_cache=True,
    )
    task.video_info = {"width": 1080, "height": 1920}
    task.is_audio_trans = False
    task.signal = lambda **kwargs: None
    task._exit = lambda: False
    captured = []
    raw = [{"line": 1, "start_time": 0, "end_time": 1000, "text": "对白"}]
    saved_rect = [0.05, 0.68, 0.9, 0.08]
    setting_values = {
        "burned_subtitle_ocr": True,
        "subtitle_removal_last_rect": json.dumps(saved_rect),
        "subtitle_removal_last_aspect_ratio": 1080 / 1920,
    }

    def fake_extract(input_file, *args, **kwargs):
        captured.append(kwargs.get("normalized_rect"))
        return raw

    monkeypatch.setattr(
        settings,
        "get",
        lambda key, default=None: setting_values.get(key, default),
    )
    monkeypatch.setattr(tools, "vail_file", lambda path: Path(path).is_file())
    monkeypatch.setattr("videotrans.subtitle_ocr.extract_burned_subtitles", fake_extract)
    monkeypatch.setattr(
        "videotrans.subtitle_ocr.fuse_ocr_with_asr",
        lambda asr, ocr: (asr, {"matched": 1}),
    )

    assert task._fuse_burned_subtitles(raw) == raw
    assert captured == [saved_rect]


def test_saved_ocr_rect_is_not_reused_for_different_aspect_ratio(monkeypatch):
    task = object.__new__(TransCreate)
    task.cfg = SimpleNamespace(
        subtitle_removal_rect=None,
        subtitle_removal_aspect_ratio=0.0,
    )
    task.video_info = {"width": 1920, "height": 1080}
    setting_values = {
        "subtitle_removal_last_rect": json.dumps([0.05, 0.68, 0.9, 0.08]),
        "subtitle_removal_last_aspect_ratio": 1080 / 1920,
    }
    monkeypatch.setattr(
        settings,
        "get",
        lambda key, default=None: setting_values.get(key, default),
    )

    assert task._resolve_burned_subtitle_ocr_rect() is None


def test_invalid_saved_ocr_rect_skips_unsafe_default_region(monkeypatch):
    task = object.__new__(TransCreate)
    task.cfg = SimpleNamespace(
        subtitle_removal_rect=None,
        subtitle_removal_aspect_ratio=0.0,
    )
    task.video_info = {"width": 1080, "height": 1920}
    setting_values = {
        "subtitle_removal_last_rect": json.dumps([0.9, 0.68, 0.2, 0.08]),
        "subtitle_removal_last_aspect_ratio": 1080 / 1920,
    }
    monkeypatch.setattr(
        settings,
        "get",
        lambda key, default=None: setting_values.get(key, default),
    )

    assert task._resolve_burned_subtitle_ocr_rect() is None


def test_burned_subtitle_ocr_without_any_rect_keeps_asr(tmp_path, monkeypatch):
    original = tmp_path / "original.mp4"
    original.write_bytes(b"video")
    task = object.__new__(TransCreate)
    task.cfg = SimpleNamespace(
        name=original.as_posix(),
        cache_folder=tmp_path.as_posix(),
        recogn_type=FASTER_WHISPER,
        detect_language="zh-cn",
        subtitle_removal_rect=None,
        subtitle_removal_aspect_ratio=0.0,
    )
    task.video_info = {"width": 1080, "height": 1920}
    task.is_audio_trans = False
    task.signal = lambda **kwargs: None
    task._exit = lambda: False
    raw = [{"line": 1, "start_time": 0, "end_time": 1000, "text": "ASR 对白"}]
    setting_values = {
        "burned_subtitle_ocr": True,
        "subtitle_removal_last_rect": "",
        "subtitle_removal_last_aspect_ratio": 0.0,
    }

    monkeypatch.setattr(
        settings,
        "get",
        lambda key, default=None: setting_values.get(key, default),
    )
    monkeypatch.setattr(tools, "vail_file", lambda path: Path(path).is_file())

    def should_not_extract(*args, **kwargs):
        raise AssertionError("OCR must not run without a selected subtitle area")

    monkeypatch.setattr(
        "videotrans.subtitle_ocr.extract_burned_subtitles",
        should_not_extract,
    )

    assert task._fuse_burned_subtitles(raw) == raw


def test_dialog_initialization_does_not_block_automatic_seek(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from videotrans.component.subtitle_removal import BatchSubtitleRemovalDialog

    input_file = tmp_path / "input.mp4"
    input_file.write_bytes(b"placeholder")
    monkeypatch.setattr(
        tools,
        "get_video_info",
        lambda path: {"width": 1080, "height": 1920, "time": 120000},
    )
    monkeypatch.setattr(BatchSubtitleRemovalDialog, "_start_locator", lambda self: None)
    app = QApplication.instance() or QApplication([])
    dialog = BatchSubtitleRemovalDialog(input_file=input_file.as_posix())

    assert dialog.user_interacted is False

    dialog.reject()
    app.processEvents()
