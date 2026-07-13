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
        speaker: {"gender": "unknown", "median_pitch_hz": None, "pitch_frames": 0}
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
            pitches = librosa.yin(
                samples,
                fmin=70,
                fmax=350,
                sr=sample_rate,
                frame_length=1024,
                hop_length=256,
            )
            rms = librosa.feature.rms(
                y=samples,
                frame_length=1024,
                hop_length=256,
                center=True,
            )[0]
            size = min(len(pitches), len(rms))
            pitches = pitches[:size]
            rms = rms[:size]
            energy_floor = max(float(np.percentile(rms, 30)), 0.005)
            voiced = pitches[
                (rms >= energy_floor)
                & np.isfinite(pitches)
                & (pitches >= 70)
                & (pitches < 345)
            ]
            if len(voiced) < 12:
                continue
            median_pitch = float(np.median(voiced))
            gender = "unknown"
            if median_pitch <= male_max_hz:
                gender = "male"
            elif median_pitch >= female_min_hz:
                gender = "female"
            profiles[speaker] = {
                "gender": gender,
                "median_pitch_hz": round(median_pitch, 1),
                "pitch_frames": int(len(voiced)),
            }
    except Exception:
        return profiles
    return profiles


def assign_speaker_voices(
    speakers,
    available_voices,
    default_voice="",
    speaker_profiles=None,
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
    used = set()
    pool_offsets = defaultdict(int)
    mapping = {}
    for speaker in ordered_speakers:
        gender = profiles.get(speaker, {}).get("gender", "unknown")
        preferred = gender_pools.get(gender) or voices
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
        "speaker_count": len(counts),
        "speaker_counts": dict(sorted(counts.items())),
        "speaker_profiles": profiles,
        "speaker_to_voice": speaker_to_voice,
        "line_role_count": len(line_roles),
        **assignment,
    }
    return line_roles, report
