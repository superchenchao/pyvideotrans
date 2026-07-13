from videotrans.process.speaker_roles import (
    assign_speaker_voices,
    build_auto_line_roles,
    normalize_speaker,
    voice_gender,
)


def test_normalize_speaker_and_voice_gender():
    assert normalize_speaker("[spk2]") == "spk2"
    assert voice_gender("Jenny(Female/US)") == "female"
    assert voice_gender("Guy(Male/US)") == "male"
    assert voice_gender("CustomVoice") == "unknown"


def test_assign_speaker_voices_prefers_gender_and_default_voice():
    speakers = ["spk0", "spk1", "spk1", "spk2", "spk1", "spk0"]
    voices = [
        "No",
        "Alice(Female/US)",
        "Bob(Male/US)",
        "Carl(Male/US)",
    ]
    profiles = {
        "spk0": {"gender": "female"},
        "spk1": {"gender": "male"},
        "spk2": {"gender": "male"},
    }

    mapping, report = assign_speaker_voices(
        speakers,
        voices,
        default_voice="Bob(Male/US)",
        speaker_profiles=profiles,
    )

    assert mapping == {
        "spk1": "Bob(Male/US)",
        "spk0": "Alice(Female/US)",
        "spk2": "Carl(Male/US)",
    }
    assert report["voice_reused"] is False


def test_build_auto_line_roles_reuses_limited_gender_voice():
    subtitles = [
        {"line": 1, "start_time": 0, "end_time": 1000},
        {"line": 2, "start_time": 1000, "end_time": 2000},
        {"line": 3, "start_time": 2000, "end_time": 3000},
    ]
    line_roles, report = build_auto_line_roles(
        speakers=["spk0", "spk1", "spk2"],
        subtitles=subtitles,
        available_voices=["No", "A(Female/JP)", "B(Male/JP)"],
        default_voice="B(Male/JP)",
        audio_file=None,
    )

    assert len(line_roles) == 3
    assert report["speaker_count"] == 3
    assert report["line_role_count"] == 3
