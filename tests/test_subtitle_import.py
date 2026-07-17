from pathlib import Path
from types import SimpleNamespace

import pytest

from videotrans.task.trans_create import TransCreate
from videotrans.mainwin._actions import WinAction
from videotrans.configure.config import params
from videotrans.util import tools
from videotrans.util.subtitle_import import (
    match_subtitles_to_videos,
    normalized_path_key,
    read_timed_subtitle,
)


def test_single_video_and_subtitle_match_without_same_filename(tmp_path):
    video = tmp_path / "video.mp4"
    subtitle = tmp_path / "manual-name.srt"

    result = match_subtitles_to_videos([video], [subtitle])

    assert result.complete
    assert result.mapping[normalized_path_key(video)] == str(subtitle)


def test_batch_matches_normalized_language_suffixes(tmp_path):
    videos = [tmp_path / "1.mp4", tmp_path / "2.mp4"]
    subtitles = [tmp_path / "2.es.srt", tmp_path / "1.zh-CN.srt"]

    result = match_subtitles_to_videos(videos, subtitles)

    assert result.complete
    assert Path(result.mapping[normalized_path_key(videos[0])]).name == "1.zh-CN.srt"
    assert Path(result.mapping[normalized_path_key(videos[1])]).name == "2.es.srt"


def test_batch_matches_episode_numbers_before_refusing_positional_fallback(tmp_path):
    videos = [tmp_path / "短剧 第01集.mp4", tmp_path / "短剧 第02集.mp4"]
    subtitles = [tmp_path / "EP02.srt", tmp_path / "EP01.srt"]

    result = match_subtitles_to_videos(videos, subtitles)

    assert result.complete
    assert Path(result.mapping[normalized_path_key(videos[0])]).name == "EP01.srt"
    assert Path(result.mapping[normalized_path_key(videos[1])]).name == "EP02.srt"

    mismatched = match_subtitles_to_videos(
        [tmp_path / "alpha.mp4", tmp_path / "beta.mp4"],
        [tmp_path / "one.srt", tmp_path / "two.srt"],
    )
    assert not mismatched.complete
    assert mismatched.safe_to_run
    assert len(mismatched.unmatched_videos) == 2


def test_batch_matches_language_episode_after_trimmed_export_suffix(tmp_path):
    videos = [
        tmp_path
        / f"第100次的回响_配音成品视频(关闭背景音乐)_zh_{index}_trimmed.mp4"
        for index in range(1, 4)
    ]
    subtitles = [
        tmp_path / f"第100次的回响_zh_{index}.srt"
        for index in range(1, 4)
    ]

    result = match_subtitles_to_videos(videos, reversed(subtitles))

    assert result.complete
    for index, video in enumerate(videos, start=1):
        assert Path(result.mapping[normalized_path_key(video)]).name == (
            f"第100次的回响_zh_{index}.srt"
        )


def test_batch_does_not_cross_language_when_only_wrong_language_exists(tmp_path):
    video = tmp_path / "剧名_zh_1_trimmed.mp4"
    wrong_language = tmp_path / "剧名_en_1.srt"

    result = match_subtitles_to_videos(
        [video, tmp_path / "剧名_zh_2_trimmed.mp4"],
        [wrong_language],
    )

    assert result.safe_to_run
    assert normalized_path_key(video) not in result.mapping
    assert video.as_posix() in [Path(path).as_posix() for path in result.unmatched_videos]


def test_batch_reports_duplicate_subtitle_candidates_as_ambiguous(tmp_path):
    video = tmp_path / "01.mp4"
    result = match_subtitles_to_videos(
        [video, tmp_path / "02.mp4"],
        [tmp_path / "01.zh.srt", tmp_path / "01.en.srt", tmp_path / "02.srt"],
    )

    assert not result.complete
    assert result.ambiguous_videos == [str(video)]


def test_read_timed_subtitle_accepts_srt_and_rejects_plain_txt(tmp_path):
    srt = tmp_path / "1.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\n你好\n", encoding="utf-8")
    plain = tmp_path / "1.txt"
    plain.write_text("只有文字，没有时间轴", encoding="utf-8")

    assert "你好" in read_timed_subtitle(srt)
    with pytest.raises(ValueError, match="no valid SRT timestamps"):
        read_timed_subtitle(plain)


def test_recogn_loads_imported_timeline_before_speaker_diarization(tmp_path):
    source_sub = tmp_path / "source.srt"
    source_sub.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n第一句\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\n第二句\n",
        encoding="utf-8",
    )
    cache = tmp_path / "cache"
    cache.mkdir()
    stale_speaker = cache / "speaker.json"
    stale_speaker.write_text('["spk0"]', encoding="utf-8")
    recognized = []
    fake_task = SimpleNamespace(
        should_recogn=False,
        used_imported_subtitles=True,
        source_srt_list=[],
        cfg=SimpleNamespace(
            source_sub=str(source_sub),
            cache_folder=str(cache),
            target_dir=str(tmp_path / "output"),
        ),
        _exit=lambda: False,
        _recogn_succeed=lambda: recognized.append(True),
    )

    TransCreate.recogn(fake_task)

    assert len(fake_task.source_srt_list) == 2
    assert not stale_speaker.exists()
    assert recognized == [True]


class _FakeTextArea:
    def __init__(self):
        self.text = ""

    def clear(self):
        self.text = ""

    def insertPlainText(self, text):
        self.text += text


class _FakeButton:
    def setText(self, text):
        self.text = text

    def setToolTip(self, text):
        self.tooltip = text


class _FakeStatus:
    def setText(self, text):
        self.text = text


def test_start_time_rematch_preserves_single_subtitle_edits(tmp_path):
    video = tmp_path / "1.mp4"
    subtitle = tmp_path / "1.srt"
    subtitle.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n原文\n",
        encoding="utf-8",
    )
    main = SimpleNamespace(
        subtitle_area=_FakeTextArea(),
        import_sub=_FakeButton(),
    )
    action = WinAction(main=main)
    action.queue_mp4 = [str(video)]
    action.imported_subtitle_files = [str(subtitle)]

    assert action._refresh_imported_subtitle_matches()
    main.subtitle_area.text = main.subtitle_area.text.replace("原文", "用户修改")
    assert action._refresh_imported_subtitle_matches(reload_single=False)

    assert "用户修改" in main.subtitle_area.text


def test_partial_batch_uses_asr_for_unmatched_video_without_blocking(
        tmp_path, monkeypatch):
    videos = [tmp_path / "1.mp4", tmp_path / "2.mp4"]
    subtitle = tmp_path / "1.srt"
    subtitle.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n第一集\n",
        encoding="utf-8",
    )
    main = SimpleNamespace(
        subtitle_area=_FakeTextArea(),
        import_sub=_FakeButton(),
        show_tips=_FakeStatus(),
    )
    action = WinAction(main=main)
    action.queue_mp4 = [str(path) for path in videos]
    action.imported_subtitle_files = [str(subtitle)]
    monkeypatch.setattr(
        tools, "show_error",
        lambda *_args, **_kwargs: pytest.fail("partial match must not block"),
    )

    assert action._refresh_imported_subtitle_matches(show_error=True)
    assert len(action.imported_subtitle_map) == 1
    assert "1" in main.show_tips.text


def test_video_root_auto_imports_named_subtitle_folder(tmp_path, monkeypatch):
    root = tmp_path / "剧集"
    video_dir = root / "视频"
    subtitle_dir = root / "字幕"
    video_dir.mkdir(parents=True)
    subtitle_dir.mkdir()
    video = video_dir / "第1集.mp4"
    subtitle = subtitle_dir / "第1集.srt"
    subtitle.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n第一集\n",
        encoding="utf-8",
    )
    main = SimpleNamespace(
        subtitle_area=_FakeTextArea(),
        import_sub=_FakeButton(),
        show_tips=_FakeStatus(),
    )
    action = WinAction(main=main)
    action.queue_mp4 = [str(video)]
    old_last_opendir = params.get('last_opendir', '')
    monkeypatch.setattr(params, 'save', lambda: None)

    try:
        assert action._auto_import_subtitles_for_video_folder(root)
    finally:
        params['last_opendir'] = old_last_opendir

    assert action.imported_subtitle_files == [subtitle.resolve().as_posix()]
    assert action.imported_subtitle_map[normalized_path_key(video)] == (
        subtitle.resolve().as_posix()
    )
