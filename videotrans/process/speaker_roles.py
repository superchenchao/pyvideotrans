import re
from collections import Counter, defaultdict
from pathlib import Path


VOICE_GENDER_PATTERN = re.compile(r"\((Female|Male)(?:/|\))", re.I)


def normalize_speaker(value):
    text = str(value or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    return text


def voice_gender(voice):
    match = VOICE_GENDER_PATTERN.search(str(voice or ""))
    return match.group(1).lower() if match else "unknown"


def is_assignable_voice(voice):
    text = str(voice or "").strip()
    return bool(text) and text.lower() not in {"no", "-", "clone"}


def classify_pitch_gender(
    *,
    primary_median,
    primary_upper_quartile,
    primary_high_ratio,
    octave_safe_median,
    octave_safe_high_ratio,
    male_max_hz=190.0,
    female_min_hz=200.0,
):
    """Classify pitch while guarding against the common half-frequency error."""
    octave_recovered_female = (
        octave_safe_median >= female_min_hz + 5
        and octave_safe_high_ratio >= 0.50
        and primary_upper_quartile >= female_min_hz + 15
        and primary_high_ratio >= 0.30
    )
    if octave_recovered_female:
        return "female", "octave_recovered"
    if primary_median <= male_max_hz:
        return "male", "primary_pitch"
    if primary_median >= female_min_hz:
        return "female", "primary_pitch"
    return "unknown", "pitch_overlap"


def estimate_speaker_profiles(
    *,
    audio_file,
    subtitles,
    speakers,
    min_line_ms=700,
    max_audio_ms=15000,
    male_max_hz=190.0,
    female_min_hz=200.0,
):
    profiles = {
        speaker: {
            "gender": "unknown",
            "median_pitch_hz": None,
            "octave_safe_pitch_hz": None,
            "high_pitch_ratio": 0.0,
            "pitch_frames": 0,
            "gender_reason": "insufficient_audio",
        }
        for speaker in dict.fromkeys(normalize_speaker(value) for value in speakers)
        if speaker
    }
    if not audio_file or not Path(audio_file).is_file():
        return profiles

    try:
        import librosa
        import numpy as np
        import soundfile as sf

        audio, sample_rate = sf.read(audio_file, dtype="float32")
        if audio.ndim > 1:
            audio = audio[:, 0]
        chunks = defaultdict(list)
        durations = defaultdict(int)
        for subtitle, raw_speaker in zip(subtitles, speakers):
            speaker = normalize_speaker(raw_speaker)
            if not speaker or speaker not in profiles:
                continue
            start = int(subtitle["start_time"])
            end = int(subtitle["end_time"])
            duration = end - start
            if duration < min_line_ms or durations[speaker] >= max_audio_ms:
                continue
            remaining = max_audio_ms - durations[speaker]
            end = min(end, start + remaining)
            sample_start = max(0, round(start * sample_rate / 1000))
            sample_end = min(len(audio), round(end * sample_rate / 1000))
            if sample_end <= sample_start:
                continue
            chunks[speaker].append(audio[sample_start:sample_end])
            durations[speaker] += end - start

        for speaker, parts in chunks.items():
            samples = np.concatenate(parts)
            if len(samples) < sample_rate // 2:
                continue
            rms = librosa.feature.rms(
                y=samples,
                frame_length=1024,
                hop_length=256,
                center=True,
            )[0]
            energy_floor = max(float(np.percentile(rms, 30)), 0.005)

            def voiced_pitches(fmin):
                pitches = librosa.yin(
                    samples,
                    fmin=fmin,
                    fmax=350,
                    sr=sample_rate,
                    frame_length=1024,
                    hop_length=256,
                )
                size = min(len(pitches), len(rms))
                values = pitches[:size]
                energy = rms[:size]
                return values[
                    (energy >= energy_floor)
                    & np.isfinite(values)
                    & (values >= fmin)
                    & (values < 345)
                ]

            voiced = voiced_pitches(70)
            if len(voiced) < 12:
                continue
            octave_safe = voiced_pitches(130)
            if len(octave_safe) < 12:
                octave_safe = voiced
            median_pitch = float(np.median(voiced))
            octave_safe_median = float(np.median(octave_safe))
            primary_high_ratio = float(np.mean(voiced >= female_min_hz))
            octave_safe_high_ratio = float(
                np.mean(octave_safe >= female_min_hz)
            )
            gender, gender_reason = classify_pitch_gender(
                primary_median=median_pitch,
                primary_upper_quartile=float(np.percentile(voiced, 75)),
                primary_high_ratio=primary_high_ratio,
                octave_safe_median=octave_safe_median,
                octave_safe_high_ratio=octave_safe_high_ratio,
                male_max_hz=male_max_hz,
                female_min_hz=female_min_hz,
            )
            profiles[speaker] = {
                "gender": gender,
                "median_pitch_hz": round(median_pitch, 1),
                "octave_safe_pitch_hz": round(octave_safe_median, 1),
                "high_pitch_ratio": round(primary_high_ratio, 3),
                "pitch_frames": int(len(voiced)),
                "gender_reason": gender_reason,
            }
    except Exception:
        return profiles
    return profiles


def assign_speaker_voices(
    speakers,
    available_voices,
    default_voice="",
    speaker_profiles=None,
    preferred_voices=None,
    locked_speakers=None,
):
    normalized = [normalize_speaker(value) for value in speakers]
    normalized = [value for value in normalized if value]
    counts = Counter(normalized)
    first_seen = {}
    for index, speaker in enumerate(normalized):
        first_seen.setdefault(speaker, index)
    ordered_speakers = sorted(
        counts,
        key=lambda speaker: (-counts[speaker], first_seen[speaker], speaker),
    )

    voices = []
    for voice in available_voices or []:
        voice = str(voice or "").strip()
        if is_assignable_voice(voice) and voice not in voices:
            voices.append(voice)
    default_voice = str(default_voice or "").strip()
    if is_assignable_voice(default_voice):
        if default_voice in voices:
            voices.remove(default_voice)
        voices.insert(0, default_voice)
    if not voices:
        return {}, {"voice_reused": False, "available_voice_count": 0}

    gender_pools = {
        "female": [voice for voice in voices if voice_gender(voice) == "female"],
        "male": [voice for voice in voices if voice_gender(voice) == "male"],
        "unknown": voices,
    }
    profiles = speaker_profiles or {}
    preferred_voices = preferred_voices or {}
    locked_speakers = {str(value) for value in (locked_speakers or set())}

    # Reserve still-valid automatic choices before allocating replacements, so
    # correcting one bad gender guess does not reshuffle every other character.
    auto_preferred = {}
    reserved_voices = set()
    for speaker in ordered_speakers:
        preferred = str(preferred_voices.get(speaker, "") or "").strip()
        if not preferred or preferred not in voices or speaker in locked_speakers:
            continue
        expected_gender = profiles.get(speaker, {}).get("gender", "unknown")
        preferred_gender = voice_gender(preferred)
        if (
            expected_gender not in {"male", "female"}
            or preferred_gender not in {"male", "female"}
            or expected_gender == preferred_gender
        ) and preferred not in reserved_voices:
            auto_preferred[speaker] = preferred
            reserved_voices.add(preferred)

    used = set()
    pool_offsets = defaultdict(int)
    mapping = {}
    for speaker in ordered_speakers:
        gender = profiles.get(speaker, {}).get("gender", "unknown")
        preferred = gender_pools.get(gender) or voices
        locked_voice = str(preferred_voices.get(speaker, "") or "").strip()
        if speaker in locked_speakers and locked_voice in voices:
            voice = locked_voice
        else:
            voice = auto_preferred.get(speaker)
        if voice is None:
            voice = next(
                (
                    item for item in preferred
                    if item not in used and item not in reserved_voices
                ),
                None,
            )
        if voice is None:
            voice = next((item for item in preferred if item not in used), None)
        if voice is None:
            voice = preferred[pool_offsets[gender] % len(preferred)]
            pool_offsets[gender] += 1
        mapping[speaker] = voice
        used.add(voice)
    return mapping, {
        "voice_reused": len(set(mapping.values())) < len(mapping),
        "available_voice_count": len(voices),
    }


def build_auto_line_roles(
    *,
    speakers,
    subtitles,
    available_voices,
    default_voice="",
    audio_file=None,
    preferred_voices=None,
    locked_speakers=None,
):
    normalized = [normalize_speaker(value) for value in speakers]
    profiles = estimate_speaker_profiles(
        audio_file=audio_file,
        subtitles=subtitles,
        speakers=normalized,
    )
    speaker_to_voice, assignment = assign_speaker_voices(
        normalized,
        available_voices,
        default_voice=default_voice,
        speaker_profiles=profiles,
        preferred_voices=preferred_voices,
        locked_speakers=locked_speakers,
    )
    line_roles = {}
    for index, speaker in enumerate(normalized):
        if index >= len(subtitles):
            break
        voice = speaker_to_voice.get(speaker)
        if voice:
            line = subtitles[index].get("line", index + 1)
            line_roles[str(line)] = voice
    counts = Counter(speaker for speaker in normalized if speaker)
    report = {
        "profile_version": 2,
        "speaker_count": len(counts),
        "speaker_counts": dict(sorted(counts.items())),
        "speaker_profiles": profiles,
        "speaker_to_voice": speaker_to_voice,
        "line_role_count": len(line_roles),
        **assignment,
    }
    return line_roles, report
