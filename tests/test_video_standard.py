import json
from pathlib import Path
from types import SimpleNamespace

from videotrans.task.trans_create import TransCreate
from videotrans.video_standard import (
    API_WORK_VIDEO_BITRATE,
    FINAL_AUDIO_BITRATE,
    FINAL_AUDIO_SAMPLE_RATE,
    FINAL_FPS,
    FINAL_VIDEO_BITRATE,
    FINAL_VIDEO_BUFSIZE,
    api_work_profile,
    build_api_work_video_args,
    build_work_video_args,
    display_dimensions,
    final_audio_codec_args,
    final_video_codec_args,
    fixed_dimensions,
    map_normalized_rect_to_canvas,
    work_profile,
)


def test_fixed_dimensions_follow_source_orientation():
    assert fixed_dimensions(3840, 2160) == (1920, 1080)
    assert fixed_dimensions(1080, 1920) == (1080, 1920)
    assert fixed_dimensions(1080, 1080) == (1920, 1080)
    assert display_dimensions(1920, 1080, 90) == (1080, 1920)
    assert fixed_dimensions(*display_dimensions(1920, 1080, -90)) == (1080, 1920)


def test_subtitle_rect_maps_through_scale_and_letterbox_padding():
    mapped = map_normalized_rect_to_canvas(
        [0.1, 0.7, 0.8, 0.1],
        source_width=1080,
        source_height=1440,
        target_width=1080,
        target_height=1920,
        reference_aspect_ratio=1080 / 1440,
    )

    assert mapped == [0.1, 0.65, 0.8, 0.075]


def test_work_video_is_high_quality_30fps_not_delivery_bitrate():
    args = build_work_video_args("input.mp4", "work.mp4", 1080, 1920)
    video_filter = args[args.index("-vf") + 1]

    assert f"fps={FINAL_FPS}" in video_filter
    assert "scale=1080:1920:force_original_aspect_ratio=decrease" in video_filter
    assert "pad=1080:1920" in video_filter
    assert args[args.index("-c:v") + 1] == "libx264"
    assert args[args.index("-crf") + 1] == "14"
    assert "-b:v" not in args
    assert "3000k" not in args


def test_cloud_api_video_is_30fps_1080p_6000k_and_has_no_audio():
    args = build_api_work_video_args("input.mp4", "api.mp4", 1080, 1920)
    video_filter = args[args.index("-vf") + 1]

    assert f"fps={FINAL_FPS}" in video_filter
    assert "scale=1080:1920:force_original_aspect_ratio=decrease" in video_filter
    assert args[args.index("-b:v") + 1] == API_WORK_VIDEO_BITRATE
    assert args[args.index("-maxrate") + 1] == API_WORK_VIDEO_BITRATE
    assert args[args.index("-r") + 1] == str(FINAL_FPS)
    assert args[args.index("-fps_mode") + 1] == "cfr"
    assert "-an" in args


def test_final_codec_args_enforce_real_x264_cbr_and_fixed_aac():
    video = final_video_codec_args()
    audio = final_audio_codec_args()

    assert video[video.index("-c:v") + 1] == "libx264"
    assert video[video.index("-b:v") + 1] == FINAL_VIDEO_BITRATE
    assert video[video.index("-minrate") + 1] == FINAL_VIDEO_BITRATE
    assert video[video.index("-maxrate") + 1] == FINAL_VIDEO_BITRATE
    assert video[video.index("-bufsize") + 1] == FINAL_VIDEO_BUFSIZE
    assert "nal-hrd=cbr" in video[video.index("-x264-params") + 1]
    assert video[video.index("-r") + 1] == str(FINAL_FPS)
    assert video[video.index("-fps_mode") + 1] == "cfr"
    assert audio[audio.index("-c:a") + 1] == "aac"
    assert audio[audio.index("-b:a") + 1] == FINAL_AUDIO_BITRATE
    assert audio[audio.index("-ar") + 1] == str(FINAL_AUDIO_SAMPLE_RATE)


def test_prepare_work_visual_source_records_profile_and_updates_pipeline_source(
        tmp_path, monkeypatch):
    source = tmp_path / "input.mp4"
    source.write_bytes(b"source")
    task = object.__new__(TransCreate)
    task.cfg = SimpleNamespace(
        name=source.as_posix(),
        cache_folder=tmp_path.as_posix(),
    )
    task.video_info = {
        "width": 1080,
        "height": 1920,
        "time": 2000,
        "video_codec_name": "h264",
        "color": "yuv420p",
    }
    task.signal = lambda **kwargs: None
    task._exit = lambda: False
    captured = []

    def fake_run(args, **kwargs):
        captured.append((args, kwargs))
        Path(args[-1]).write_bytes(b"work-video")

    monkeypatch.setattr("videotrans.task.trans_create.run_work_video_ffmpeg", fake_run)
    monkeypatch.setattr(
        "videotrans.task.trans_create.tools.get_video_info",
        lambda path: {
            "video_codec_name": "h264",
            "width": 1080,
            "height": 1920,
            "video_fps": 30,
        },
    )

    task._prepare_work_visual_source()

    assert len(captured) == 1
    assert captured[0][1]["cancel_callback"] == task._exit
    assert task.visual_source.endswith("source-working-30fps.mp4")
    assert task.ocr_source == task.visual_source
    assert task.video_info["video_fps"] == 30
    metadata = json.loads(
        Path(f"{task.visual_source}.json").read_text(encoding="utf-8")
    )
    assert metadata["profile"] == work_profile(1080, 1920)


def test_stale_50fps_work_profile_is_not_reused(tmp_path, monkeypatch):
    source = tmp_path / "input.mp4"
    source.write_bytes(b"source")
    work = tmp_path / "source-working-30fps.mp4"
    work.write_bytes(b"stale-work-video")
    Path(f"{work}.json").write_text(
        json.dumps({"profile": {"fps": 50}}),
        encoding="utf-8",
    )
    task = object.__new__(TransCreate)
    task.cfg = SimpleNamespace(name=source.as_posix(), cache_folder=tmp_path.as_posix())
    task.video_info = {"width": 1920, "height": 1080, "time": 1000}
    task.signal = lambda **kwargs: None
    task._exit = lambda: False
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        Path(args[-1]).write_bytes(b"new-work-video")

    monkeypatch.setattr("videotrans.task.trans_create.run_work_video_ffmpeg", fake_run)
    monkeypatch.setattr(
        "videotrans.task.trans_create.tools.get_video_info",
        lambda path: {
            "video_codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "video_fps": 30,
        },
    )

    task._prepare_work_visual_source()

    assert len(calls) == 1
    metadata = json.loads(Path(f"{work}.json").read_text(encoding="utf-8"))
    assert metadata["profile"]["fps"] == 30


def test_cloud_provider_uses_direct_api_transport_profile(tmp_path, monkeypatch):
    source = tmp_path / "input.mp4"
    source.write_bytes(b"source")
    task = object.__new__(TransCreate)
    task.cfg = SimpleNamespace(
        name=source.as_posix(),
        cache_folder=tmp_path.as_posix(),
        remove_burned_subtitles=True,
        subtitle_removal_provider="caca_link",
    )
    task.video_info = {"width": 1080, "height": 1920, "time": 2000}
    task.signal = lambda **kwargs: None
    task._exit = lambda: False
    captured = []

    def fake_run(args, **kwargs):
        captured.append(args)
        Path(args[-1]).write_bytes(b"api-video")

    monkeypatch.setattr("videotrans.task.trans_create.run_work_video_ffmpeg", fake_run)
    monkeypatch.setattr(
        "videotrans.task.trans_create.tools.get_video_info",
        lambda path: {
            "video_codec_name": "h264",
            "width": 1080,
            "height": 1920,
            "video_fps": 30,
        },
    )

    task._prepare_work_visual_source()

    assert len(captured) == 1
    assert captured[0][captured[0].index("-i") + 1] == source.as_posix()
    assert captured[0][captured[0].index("-b:v") + 1] == "6000k"
    assert "-an" in captured[0]
    assert task.visual_source.endswith(
        "source-working-api-30fps-6000k-noaudio.mp4"
    )
    metadata = json.loads(
        Path(f"{task.visual_source}.json").read_text(encoding="utf-8")
    )
    assert metadata["profile"] == api_work_profile(1080, 1920)


def test_final_join_command_never_uses_copy_and_enforces_delivery_standard(
        tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    novoice = tmp_path / "novoice.mp4"
    source.write_bytes(b"source")
    novoice.write_bytes(b"video")
    task = object.__new__(TransCreate)
    task.cfg = SimpleNamespace(
        name=source.as_posix(),
        novoice_mp4=novoice.as_posix(),
        cache_folder=tmp_path.as_posix(),
        target_wav_output=(tmp_path / "audio.m4a").as_posix(),
        target_sub=(tmp_path / "unused-target.srt").as_posix(),
        source_sub=(tmp_path / "unused-source.srt").as_posix(),
        targetdir_mp4=(tmp_path / "final.mp4").as_posix(),
        subtitle_type=0,
        video_autorate=False,
    )
    task.video_info = {
        "streams_audio": 1,
        "audio_codec_name": "aac",
        "width": 1080,
        "height": 1920,
    }
    task.should_hebing = True
    task.should_dubbing = False
    task.precent = 90
    task.hasend = False
    task.uuid = "standard-command-test"
    task.signal = lambda **kwargs: None
    task._exit = lambda: False
    task._get_origin_audio = lambda output: Path(output).write_bytes(b"audio")
    commands = []

    class DummyThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    def fake_runffmpeg(command, *, cmd_dir=None, **kwargs):
        commands.append(command)
        output = Path(command[-1])
        if not output.is_absolute() and cmd_dir:
            output = Path(cmd_dir, output)
        output.write_bytes(b"final")
        return True

    monkeypatch.setattr("videotrans.task.trans_create.threading.Thread", DummyThread)
    monkeypatch.setattr("videotrans.task.trans_create.tools.is_novoice_mp4", lambda *args: None)
    monkeypatch.setattr("videotrans.task.trans_create.tools.get_video_duration", lambda *args: 8000)
    monkeypatch.setattr("videotrans.task.trans_create.tools.get_audio_time", lambda *args: 8000)
    monkeypatch.setattr("videotrans.task.trans_create.tools.runffmpeg", fake_runffmpeg)

    task._join_video_audio_srt()

    final_command = commands[-1]
    assert "copy" not in final_command
    assert final_command[final_command.index("-c:v") + 1] == "libx264"
    assert final_command[final_command.index("-b:v") + 1] == "3000k"
    assert final_command[final_command.index("-minrate") + 1] == "3000k"
    assert final_command[final_command.index("-maxrate") + 1] == "3000k"
    assert final_command[final_command.index("-bufsize") + 1] == "6000k"
    assert final_command[final_command.index("-c:a") + 1] == "aac"
    assert final_command[final_command.index("-b:a") + 1] == "192k"
    assert final_command[final_command.index("-ar") + 1] == "44100"
    assert "fps=30" in final_command[final_command.index("-vf") + 1]
