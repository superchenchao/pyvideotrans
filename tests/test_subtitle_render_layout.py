from pathlib import Path
from types import SimpleNamespace

import pytest

from videotrans.task import trans_create as trans_create_module
from videotrans.task.trans_create import TransCreate
from videotrans.util import help_srt


ASS_TEMPLATE = """[Script Info]
ScriptType: v4.00+
PlayResX: 384
PlayResY: 288
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,16,&Hffffff,&Hffffff,&H0,&H0,0,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,恥というものを知っているのか
"""


def test_wrap_subtitle_for_portrait_video_balances_japanese_line(monkeypatch) -> None:
    monkeypatch.setattr(help_srt, "_load_ass_style", lambda: {})

    wrapped = help_srt.wrap_subtitle_for_video(
        "恥というものを知っているのか",
        language="ja",
        video_width=1080,
        video_height=1920,
        max_chars=20,
    )

    assert wrapped == "恥というものを\n知っているのか"


def test_wrap_subtitle_for_portrait_video_keeps_short_line(monkeypatch) -> None:
    monkeypatch.setattr(help_srt, "_load_ass_style", lambda: {})

    wrapped = help_srt.wrap_subtitle_for_video(
        "あなたは誰",
        language="ja",
        video_width=1080,
        video_height=1920,
        max_chars=20,
    )

    assert wrapped == "あなたは誰"


def test_wrap_subtitle_for_video_prefers_english_word_boundaries(monkeypatch) -> None:
    monkeypatch.setattr(help_srt, "_load_ass_style", lambda: {})

    wrapped, font_scale = help_srt.layout_subtitle_for_video(
        "This is an unusually long translated subtitle that should stay inside a portrait video frame.",
        language="en",
        video_width=1080,
        video_height=1920,
        max_chars=48,
    )

    assert "unusual\nly" not in wrapped
    assert "shou\nld" not in wrapped
    assert "po\nrtrait" not in wrapped
    assert font_scale < 1.0
    assert wrapped.count("\n") + 1 <= 4


def test_set_ass_font_normalizes_playres_and_scales_style(tmp_path, monkeypatch) -> None:
    source_srt = tmp_path / "end.srt"
    original_srt = "1\n00:00:00,000 --> 00:00:02,000\n恥というものを知っているのか\n"
    source_srt.write_text(original_srt, encoding="utf-8")

    def fake_runffmpeg(command):
        Path(command[-1]).write_text(ASS_TEMPLATE, encoding="utf-8")
        return True

    from videotrans.util import help_ffmpeg

    monkeypatch.setattr(help_ffmpeg, "runffmpeg", fake_runffmpeg)
    monkeypatch.setattr(help_srt, "_load_ass_style", lambda: {})

    ass_path = help_srt.set_ass_font(
        str(source_srt),
        video_width=1080,
        video_height=1920,
        dialogue_font_scales=[0.75],
    )

    normalized = Path(ass_path).read_text(encoding="utf-8")
    assert "PlayResX: 1080" in normalized
    assert "PlayResY: 1920" in normalized
    assert "WrapStyle: 0" in normalized

    style_line = next(line for line in normalized.splitlines() if line.startswith("Style: Default,"))
    fields = style_line.split(": ", 1)[1].split(",")
    assert float(fields[2]) == pytest.approx(106.67, abs=0.01)
    assert float(fields[16]) == pytest.approx(6.67, abs=0.01)
    assert float(fields[19]) == pytest.approx(28.13, abs=0.01)
    assert float(fields[20]) == pytest.approx(28.13, abs=0.01)
    assert float(fields[21]) == pytest.approx(66.67, abs=0.01)
    assert r"{\fscx75\fscy75}" in normalized
    assert source_srt.read_text(encoding="utf-8") == original_srt


def test_process_subtitles_uses_video_aware_layout_for_hard_subtitles(tmp_path, monkeypatch) -> None:
    target_srt = tmp_path / "ja.srt"
    target_srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n恥というものを知っているのか\n",
        encoding="utf-8",
    )

    task = object.__new__(TransCreate)
    task.cfg = SimpleNamespace(
        target_sub=str(target_srt),
        source_sub=str(tmp_path / "missing-source.srt"),
        target_dir=str(tmp_path),
        cache_folder=str(tmp_path),
        subtitle_type=1,
        source_language_code="zh-cn",
        target_language_code="ja",
        target_language="日语",
        output_srt=0,
    )
    task.video_info = {"width": 1080, "height": 1920}

    captured = {}

    def fake_set_ass_font(srtfile, **kwargs):
        captured["srtfile"] = srtfile
        captured.update(kwargs)
        return str(Path(srtfile).with_suffix(".ass"))

    monkeypatch.setattr(help_srt, "_load_ass_style", lambda: {})
    monkeypatch.setattr(trans_create_module.tools, "set_ass_font", fake_set_ass_font)
    monkeypatch.setattr(trans_create_module.translator, "get_subtitle_code", lambda **_kwargs: "jpn")

    result = TransCreate._process_subtitles(task)

    rendered_srt = (tmp_path / "end.srt").read_text(encoding="utf-8")
    assert "恥というものを\n知っているのか" in rendered_srt
    assert captured["video_width"] == 1080
    assert captured["video_height"] == 1920
    assert captured["dialogue_font_scales"] == [1.0]
    assert result == ("end.ass", "jpn")
