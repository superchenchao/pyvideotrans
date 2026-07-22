import json
from types import SimpleNamespace

from videotrans.process import speaker_roles
from videotrans.task.trans_create import TransCreate
from videotrans.util import tools


def test_prepare_line_roles_keeps_auto_mapping_task_local_and_applies_manual_override(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    target_dir = tmp_path / "target"
    cache_dir.mkdir()
    target_dir.mkdir()
    (cache_dir / "speaker.json").write_text(
        json.dumps(["spk0", "spk1"]), encoding="utf-8"
    )
    (cache_dir / "line_roles.json").write_text(
        json.dumps({"2": "ManualVoice"}), encoding="utf-8"
    )

    captured_build_kwargs = {}

    def fake_build_auto_line_roles(**kwargs):
        captured_build_kwargs.update(kwargs)
        return {"1": "AutoVoiceA", "2": "AutoVoiceB"}, {
            "speaker_count": 2,
            "speaker_to_voice": {"spk0": "AutoVoiceA", "spk1": "AutoVoiceB"},
        }

    monkeypatch.setattr(speaker_roles, "build_auto_line_roles", fake_build_auto_line_roles)
    monkeypatch.setattr(tools, "role_menu", lambda *args: ["AutoVoiceA", "AutoVoiceB"])

    task = object.__new__(TransCreate)
    task.auto_line_roles = {}
    task.cfg = SimpleNamespace(
        cache_folder=cache_dir.as_posix(),
        target_dir=target_dir.as_posix(),
        target_language_code="en",
        tts_type=0,
        voice_role="AutoVoiceA",
        source_wav=(tmp_path / "source.wav").as_posix(),
    )
    subtitles = [
        {"line": 1, "start_time": 0, "end_time": 1000},
        {"line": 2, "start_time": 1000, "end_time": 2000},
    ]

    result = task._prepare_line_roles(subtitles)

    assert result == {"1": "AutoVoiceA", "2": "ManualVoice"}
    assert task.auto_line_roles == {"1": "AutoVoiceA", "2": "AutoVoiceB"}
    assert captured_build_kwargs["audio_file"] == task.cfg.source_wav
    report = json.loads((cache_dir / "speaker_roles.json").read_text(encoding="utf-8"))
    assert report["gender_audio_source"] == "source_wav"
    assert (cache_dir / "speaker_roles.json").is_file()


def test_prepare_line_roles_prefers_separated_vocal_for_gender_detection(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    target_dir = tmp_path / "target"
    cache_dir.mkdir()
    target_dir.mkdir()
    (cache_dir / "speaker.json").write_text(
        json.dumps(["spk0", "spk1"]), encoding="utf-8"
    )
    source_wav = tmp_path / "source.wav"
    vocal_wav = tmp_path / "vocal.wav"
    source_wav.write_bytes(b"source")
    vocal_wav.write_bytes(b"vocal")
    captured = {}

    def fake_build_auto_line_roles(**kwargs):
        captured.update(kwargs)
        return {"1": "MaleVoice", "2": "FemaleVoice"}, {
            "speaker_count": 2,
            "speaker_to_voice": {
                "spk0": "MaleVoice",
                "spk1": "FemaleVoice",
            },
        }

    monkeypatch.setattr(speaker_roles, "build_auto_line_roles", fake_build_auto_line_roles)
    monkeypatch.setattr(tools, "role_menu", lambda *args: ["MaleVoice", "FemaleVoice"])

    task = object.__new__(TransCreate)
    task.auto_line_roles = {}
    task.recogn_vocal = ""
    task.cfg = SimpleNamespace(
        cache_folder=cache_dir.as_posix(),
        target_dir=target_dir.as_posix(),
        target_language_code="en",
        tts_type=0,
        voice_role="MaleVoice",
        source_wav=source_wav.as_posix(),
        vocal=vocal_wav.as_posix(),
    )
    task._register_series_speakers = lambda **kwargs: None
    subtitles = [
        {"line": 1, "start_time": 0, "end_time": 1000},
        {"line": 2, "start_time": 1000, "end_time": 2000},
    ]

    task._prepare_line_roles(subtitles)

    assert captured["audio_file"] == vocal_wav.as_posix()
    report = json.loads((cache_dir / "speaker_roles.json").read_text(encoding="utf-8"))
    assert report["gender_audio_source"] == "separated_vocal"


def test_prepare_line_roles_registers_single_speaker_episode(tmp_path):
    cache_dir = tmp_path / "cache"
    target_dir = tmp_path / "target"
    cache_dir.mkdir()
    target_dir.mkdir()
    (cache_dir / "speaker.json").write_text(
        json.dumps(["spk0", "spk0"]), encoding="utf-8"
    )
    task = object.__new__(TransCreate)
    task.auto_line_roles = {}
    task.cfg = SimpleNamespace(
        cache_folder=cache_dir.as_posix(),
        target_dir=target_dir.as_posix(),
        target_language_code="ja",
        tts_type=0,
        voice_role="Nanami",
        source_wav=(tmp_path / "source.wav").as_posix(),
    )
    captured = {}
    task._register_series_speakers = lambda **kwargs: captured.update(kwargs)
    subtitles = [
        {"line": 1, "start_time": 0, "end_time": 1000},
        {"line": 2, "start_time": 1000, "end_time": 2000},
    ]

    assert task._prepare_line_roles(subtitles) == {}
    assert captured["speakers"] == ["spk0", "spk0"]
    assert captured["report"]["speaker_to_voice"] == {"spk0": "Nanami"}
