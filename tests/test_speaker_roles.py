from videotrans.process.speaker_roles import (
    assign_speaker_voices,
    build_auto_line_roles,
    classify_pitch_gender,
    normalize_speaker,
    voice_gender,
)


def test_pitch_classifier_recovers_half_frequency_female_voice():
    gender, reason = classify_pitch_gender(
        primary_median=145.4,
        primary_upper_quartile=233.0,
        primary_high_ratio=0.39,
        octave_safe_median=215.4,
        octave_safe_high_ratio=0.57,
    )

    assert gender == "female"
    assert reason == "octave_recovered"


def test_pitch_classifier_keeps_real_male_voice_male():
    gender, reason = classify_pitch_gender(
        primary_median=132.3,
        primary_upper_quartile=166.1,
        primary_high_ratio=0.16,
        octave_safe_median=161.7,
        octave_safe_high_ratio=0.27,
    )

    assert gender == "male"
    assert reason == "primary_pitch"


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


def test_voice_reassignment_preserves_valid_choices_and_replaces_mismatch():
    speakers = ["spk1"] * 31 + ["spk3"] * 30 + ["spk0"] * 23 + ["spk2"] * 9
    voices = [
        "Arabella(Female/JP)",
        "Seraphina(Female/JP)",
        "Florian(Male/JP)",
        "William(Male/JP)",
        "Ollie(Male/JP)",
    ]
    profiles = {
        "spk0": {"gender": "female"},
        "spk1": {"gender": "male"},
        "spk2": {"gender": "male"},
        "spk3": {"gender": "female"},
    }
    previous = {
        "spk0": "Arabella(Female/JP)",
        "spk1": "Florian(Male/JP)",
        "spk2": "Ollie(Male/JP)",
        "spk3": "William(Male/JP)",
    }

    mapping, _ = assign_speaker_voices(
        speakers,
        voices,
        speaker_profiles=profiles,
        preferred_voices=previous,
    )

    assert mapping["spk0"] == "Arabella(Female/JP)"
    assert mapping["spk1"] == "Florian(Male/JP)"
    assert mapping["spk2"] == "Ollie(Male/JP)"
    assert mapping["spk3"] == "Seraphina(Female/JP)"


def test_manual_voice_choice_is_locked_even_when_gender_differs():
    mapping, _ = assign_speaker_voices(
        ["spk0"],
        ["CustomMale(Male/JP)", "AutoFemale(Female/JP)"],
        speaker_profiles={"spk0": {"gender": "female"}},
        preferred_voices={"spk0": "CustomMale(Male/JP)"},
        locked_speakers={"spk0"},
    )

    assert mapping["spk0"] == "CustomMale(Male/JP)"


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
