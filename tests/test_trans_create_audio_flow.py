import json
from pathlib import Path
from types import SimpleNamespace

from videotrans.configure.config import settings
from videotrans.process.stt_fun import (
    _build_refinement_groups,
    _merge_refined_faster_result,
)
from videotrans.task.trans_create import TransCreate
from videotrans.util import tools


def test_unlimited_built_oversegmentation_retries_with_cam(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    target_dir = tmp_path / "target"
    cache_dir.mkdir()
    target_dir.mkdir()
    speaker_path = cache_dir / "speaker.json"
    labels = (
        ["spk0"] * 10 + ["spk1"] * 9 + ["spk2"] * 7 + ["spk3"] * 6
        + [f"spk{index}" for index in range(4, 30) for _ in range(2)]
    )
    speaker_path.write_text(json.dumps(labels), encoding="utf-8")

    task = object.__new__(TransCreate)
    task.cfg = SimpleNamespace(
        enable_diariz=True,
        cache_folder=cache_dir.as_posix(),
        target_dir=target_dir.as_posix(),
        dirname=tmp_path.as_posix(),
        name=(tmp_path / "01.mp4").as_posix(),
        series_video_paths=[(tmp_path / "01.mp4").as_posix()],
        detect_language="zh-cn",
        source_wav=(cache_dir / "zh-cn.wav").as_posix(),
        is_cuda=True,
    )
    task.max_speakers = 0
    task.precent = 0
    task.source_srt_list = [{"start_time": 0, "end_time": 1000}]
    task._process_callback = lambda *args, **kwargs: None
    task._exit = lambda: False
    task.signal = lambda **kwargs: None
    calls = []

    def fake_process(*, callback, kwargs, **unused):
        calls.append((callback.__name__, dict(kwargs)))
        Path(kwargs["speak_file"]).write_text(
            json.dumps(["spk0"]), encoding="utf-8"
        )
        return True

    monkeypatch.setattr(tools, "check_and_down_ms", lambda *args, **kwargs: None)
    monkeypatch.setattr(settings, "get", lambda key, default=None: (
        "built" if key == "speaker_type" else default
    ))
    task._new_process = fake_process

    task.diariz()

    assert calls[0][0] == "cam_speakers"
    assert calls[0][1]["num_speakers"] == 4
    assert calls[0][1]["is_cuda"] is True


def test_vocal_separation_does_not_overwrite_recognition_audio(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    target_dir = tmp_path / "target"
    cache_dir.mkdir()
    target_dir.mkdir()

    source_wav = cache_dir / "zh-cn.wav"
    vocal_wav = cache_dir / "vocal.wav"
    instrument_wav = cache_dir / "instrument.wav"

    task = object.__new__(TransCreate)
    task.cfg = SimpleNamespace(
        name=(tmp_path / "input.mp4").as_posix(),
        source_wav=source_wav.as_posix(),
        cache_folder=cache_dir.as_posix(),
        vocal=vocal_wav.as_posix(),
        instrument=instrument_wav.as_posix(),
        target_dir=target_dir.as_posix(),
    )
    task._process_callback = lambda *args, **kwargs: None

    ffmpeg_calls = []

    def fake_runffmpeg(cmd, *args, **kwargs):
        ffmpeg_calls.append(cmd)
        return True

    def fake_new_process(*, kwargs, **unused):
        Path(kwargs["vocal_file"]).write_bytes(b"vocal")
        Path(kwargs["instr_file"]).write_bytes(b"instrument")
        return True

    monkeypatch.setattr(tools, "runffmpeg", fake_runffmpeg)
    monkeypatch.setattr(tools, "down_file_from_ms", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        settings,
        "get",
        lambda key, default=None: "spleeter" if key == "uvr_models" else default,
    )
    task._new_process = fake_new_process

    task._split_audio_byraw(is_separate=True)

    recognition_writes = [cmd for cmd in ffmpeg_calls if cmd[-1] == source_wav.as_posix()]
    assert len(recognition_writes) == 1
    assert recognition_writes[0][recognition_writes[0].index("-i") + 1] == task.cfg.name
    assert all(vocal_wav.as_posix() not in cmd for cmd in recognition_writes)
    assert (target_dir / "vocal.wav").read_bytes() == b"vocal"
    assert (target_dir / "instrument.wav").read_bytes() == b"instrument"


def test_prepare_recognition_vocal_keeps_original_source_separate(tmp_path, monkeypatch):
    vocal_wav = tmp_path / "vocal.wav"
    vocal_wav.write_bytes(b"vocal")

    task = object.__new__(TransCreate)
    task.cfg = SimpleNamespace(vocal=vocal_wav.as_posix())
    task.recogn_vocal = (tmp_path / "recognition-vocal.wav").as_posix()
    ffmpeg_calls = []
    monkeypatch.setattr(tools, "runffmpeg", lambda cmd: ffmpeg_calls.append(cmd) or True)

    task._prepare_recognition_vocal()

    assert len(ffmpeg_calls) == 1
    assert ffmpeg_calls[0][ffmpeg_calls[0].index("-i") + 1] == vocal_wav.as_posix()
    assert ffmpeg_calls[0][-1] == task.recogn_vocal
    assert ffmpeg_calls[0][ffmpeg_calls[0].index("-af") + 1] == "volume=1.5"


def test_volcengine_is_default_diarization_provider_and_uses_clean_vocal(
        tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    target_dir = tmp_path / "target"
    cache_dir.mkdir()
    target_dir.mkdir()
    source_wav = cache_dir / "source.wav"
    vocal_wav = cache_dir / "vocal.wav"
    recognition_vocal = cache_dir / "recognition-vocal.wav"
    source_wav.write_bytes(b"source")
    vocal_wav.write_bytes(b"vocal")
    recognition_vocal.write_bytes(b"clean-vocal")
    # Simulate an old local-model cache. The cloud provider must replace it.
    (cache_dir / "speaker.json").write_text(json.dumps(["spk9"]), encoding="utf-8")

    task = object.__new__(TransCreate)
    task.cfg = SimpleNamespace(
        enable_diariz=True,
        cache_folder=cache_dir.as_posix(),
        target_dir=target_dir.as_posix(),
        detect_language="zh-cn",
        source_wav=source_wav.as_posix(),
        vocal=vocal_wav.as_posix(),
        is_cuda=True,
    )
    task.recogn_vocal = recognition_vocal.as_posix()
    task.max_speakers = 0
    task.precent = 0
    task.source_srt_list = [{"start_time": 0, "end_time": 1000}]
    task._exit = lambda: False
    task.signal = lambda **kwargs: None
    calls = []

    def fake_process(*, callback, kwargs, is_cuda, **unused):
        calls.append((callback.__name__, dict(kwargs), is_cuda))
        Path(kwargs["speak_file"]).write_text(json.dumps(["spk0"]), encoding="utf-8")
        Path(kwargs["speak_file"]).with_name("speaker.volcengine.json").write_text(
            json.dumps({"provider": "volcengine_flash"}), encoding="utf-8"
        )
        return True

    monkeypatch.setattr(
        settings,
        "get",
        lambda key, default=None: "volcengine" if key == "speaker_type" else default,
    )
    from videotrans.recognition import volcengine_flash
    monkeypatch.setattr(volcengine_flash, "credentials_configured", lambda: True)
    task._new_process = fake_process

    task.diariz()

    assert len(calls) == 1
    assert calls[0][0] == "volcengine_flash_speakers"
    assert calls[0][1]["input_file"] == recognition_vocal.as_posix()
    assert calls[0][2] is False
    assert json.loads((target_dir / "speaker.json").read_text(encoding="utf-8")) == ["spk0"]


def test_refinement_replaces_only_similar_original_audio_candidates():
    baseline = [
        {"line": 1, "start_time": 31500, "end_time": 33020, "text": "我醉了真好"},
        {"line": 2, "start_time": 33020, "end_time": 35300, "text": "这么快就入门了"},
        {"line": 3, "start_time": 35300, "end_time": 35840, "text": "他"},
        {"line": 4, "start_time": 37500, "end_time": 40460, "text": "身上怎么也有酒的味道"},
        {"line": 5, "start_time": 63760, "end_time": 64319, "text": "你看"},
        {"line": 6, "start_time": 77180, "end_time": 78560, "text": "清楚我是谁"},
    ]
    refined = [
        {"line": 1, "start_time": 31520, "end_time": 33040, "text": "喝醉了真好"},
        {"line": 2, "start_time": 33040, "end_time": 35320, "text": "这么快就入梦了"},
        {"line": 3, "start_time": 35320, "end_time": 36080, "text": "一"},
        {"line": 4, "start_time": 36960, "end_time": 38060, "text": "他身上"},
        {"line": 5, "start_time": 38060, "end_time": 40060, "text": "怎么也有酒的味道"},
        {"line": 6, "start_time": 64320, "end_time": 65100, "text": "未经许可不得翻唱或使用"},
        {"line": 7, "start_time": 76520, "end_time": 78460, "text": "你看清楚我是谁"},
    ]

    groups = _build_refinement_groups(baseline, audio_duration=120000)
    result = _merge_refined_faster_result(groups, refined)

    assert [item["text"] for item in result] == [
        "喝醉了真好",
        "这么快就入梦了",
        "他身上",
        "怎么也有酒的味道",
        "你看",
        "你看清楚我是谁",
    ]
    assert [item["line"] for item in result] == list(range(1, 7))
