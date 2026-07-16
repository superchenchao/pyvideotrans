import hashlib
import json
import math
import os
import re
import threading
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np


MANIFEST_VERSION = 1
DEFAULT_MATCH_THRESHOLD = 0.58
DEFAULT_MATCH_MARGIN = 0.06
_MANIFEST_LOCK = threading.RLock()
_CJK_NAME = r"[\u3400-\u4dbf\u4e00-\u9fff]{2,4}"
_SELF_INTRO_PATTERNS = (
    re.compile(
        rf"(?:我叫|我的名字叫)(?P<name>{_CJK_NAME})"
        rf"(?=[，,。.!！?？\s]|$)"
    ),
    re.compile(
        rf"(?:本王|本宫|本座|老夫)是(?P<name>{_CJK_NAME})"
        rf"(?=[，,。.!！?？\s]|$)"
    ),
)
_VOCATIVE_PATTERN = re.compile(rf"^(?P<name>{_CJK_NAME})[，,！!]")
_NAME_STOPWORDS = {
    "不是", "不要", "不会", "不能", "可以", "知道", "什么", "怎么",
    "为什么", "没事", "谢谢", "真的", "当然", "好的", "等等", "快点",
    "现在", "今天", "明天", "昨天", "这里", "那里", "这个", "那个",
    "爸爸", "妈妈", "父亲", "母亲", "哥哥", "姐姐", "弟弟", "妹妹",
    "师父", "师傅", "师兄", "师姐", "师弟", "师妹", "陛下", "殿下",
    "夫人", "小姐", "少爷", "主人", "大人", "医生", "老师", "同学",
}


def _normalized_series_videos(series_videos):
    return sorted({
        Path(value).resolve().as_posix().casefold()
        for value in (series_videos or [])
        if str(value or "").strip()
    })


def empty_manifest(series_folder="", series_videos=None):
    return {
        "version": MANIFEST_VERSION,
        "series_folder": str(series_folder or ""),
        "series_videos": _normalized_series_videos(series_videos),
        "characters": [],
        "episodes": {},
    }


def manifest_path_for_series(output_dir, series_folder="", series_videos=None):
    output_path = Path(output_dir)
    normalized_videos = _normalized_series_videos(series_videos)
    if not series_folder and not normalized_videos:
        return output_path / "series_characters.json"
    resolved = (
        Path(series_folder).resolve().as_posix().casefold()
        if series_folder else ""
    )
    identity = resolved
    if normalized_videos:
        identity += "\n" + "\n".join(normalized_videos)
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:10]
    slug = re.sub(r"[^0-9A-Za-z\u3400-\u4dbf\u4e00-\u9fff_-]+", "-", Path(series_folder).name)
    slug = slug.strip("-")[:32] or "series"
    return output_path / f"series_characters.{slug}.{digest}.json"


def voice_scope_key(tts_type, target_language):
    return f"{int(tts_type)}:{str(target_language or '').strip().casefold()}"


def character_voice(character, scope=""):
    if scope:
        voices = character.get("voices")
        if isinstance(voices, dict):
            return str(voices.get(scope, "") or "").strip()
    return str(character.get("voice", "") or "").strip()


def load_manifest(path, *, series_folder="", series_videos=None):
    manifest_path = Path(path)
    if not manifest_path.is_file():
        return empty_manifest(series_folder, series_videos)
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return empty_manifest(series_folder, series_videos)
    if not isinstance(data, dict):
        return empty_manifest(series_folder, series_videos)
    data.setdefault("version", MANIFEST_VERSION)
    data.setdefault("series_folder", str(series_folder or ""))
    data.setdefault("series_videos", _normalized_series_videos(series_videos))
    data.setdefault("characters", [])
    data.setdefault("episodes", {})
    return data


def save_manifest(path, manifest):
    manifest_path = Path(path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = manifest_path.with_name(
        f".{manifest_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    payload = json.dumps(manifest, ensure_ascii=False, indent=2)
    with _MANIFEST_LOCK:
        temp_path.write_text(payload, encoding="utf-8")
        os.replace(temp_path, manifest_path)


def register_episode_file(path, *, series_folder="", series_videos=None, **episode_data):
    """Load, update, and atomically save one shared series manifest."""
    with _MANIFEST_LOCK:
        manifest = load_manifest(
            path, series_folder=series_folder, series_videos=series_videos
        )
        if series_folder:
            manifest["series_folder"] = str(series_folder)
        if series_videos:
            manifest["series_videos"] = _normalized_series_videos(series_videos)
        assignments = register_episode(manifest, **episode_data)
        save_manifest(path, manifest)
    return manifest, assignments


def _normalized_vector(values):
    vector = np.asarray(values, dtype=np.float32)
    norm = float(np.linalg.norm(vector))
    if not np.isfinite(norm) or norm <= 1e-9:
        return None
    return vector / norm


def cosine_similarity(left, right):
    left_vector = _normalized_vector(left)
    right_vector = _normalized_vector(right)
    if left_vector is None or right_vector is None:
        return -1.0
    if left_vector.shape != right_vector.shape:
        return -1.0
    return float(left_vector @ right_vector)


def _next_character_id(characters):
    used = {
        str(item.get("id", ""))
        for item in characters
        if isinstance(item, dict)
    }
    index = 1
    while f"character_{index:03d}" in used:
        index += 1
    return f"character_{index:03d}"


def _character_index(manifest):
    return {
        str(item.get("id")): item
        for item in manifest.get("characters", [])
        if isinstance(item, dict) and item.get("id")
    }


def _candidate_rankings(profile, characters, unavailable_ids):
    embedding = profile.get("embedding") or []
    rankings = []
    for character in characters:
        character_id = str(character.get("id", ""))
        if not character_id or character_id in unavailable_ids:
            continue
        similarity = cosine_similarity(embedding, character.get("centroid") or [])
        if similarity >= -0.5:
            rankings.append((similarity, character_id))
    return sorted(rankings, key=lambda item: (-item[0], item[1]))


def _merge_centroid(character, embedding, sample_count):
    incoming = _normalized_vector(embedding)
    if incoming is None:
        return
    existing = _normalized_vector(character.get("centroid") or [])
    existing_weight = max(0, int(character.get("sample_count", 0)))
    incoming_weight = max(1, int(sample_count or 1))
    if existing is None or existing.shape != incoming.shape:
        merged = incoming
        total_weight = incoming_weight
    else:
        merged = existing * existing_weight + incoming * incoming_weight
        merged = merged / (np.linalg.norm(merged) + 1e-9)
        total_weight = existing_weight + incoming_weight
    character["centroid"] = [round(float(value), 7) for value in merged]
    character["sample_count"] = total_weight


def _merge_name_candidates(character, candidates):
    merged = {
        str(item.get("name")): dict(item)
        for item in character.get("name_candidates", [])
        if isinstance(item, dict) and item.get("name")
    }
    for item in candidates or []:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        current = merged.get(name)
        if current is None or float(item.get("score", 0)) > float(current.get("score", 0)):
            merged[name] = dict(item)
    character["name_candidates"] = sorted(
        merged.values(),
        key=lambda item: (-float(item.get("score", 0)), str(item.get("name", ""))),
    )[:8]


def register_episode(
    manifest,
    *,
    episode_key,
    episode_name,
    speaker_profiles,
    speaker_counts=None,
    speaker_to_voice=None,
    name_candidates=None,
    voice_scope="",
    match_threshold=DEFAULT_MATCH_THRESHOLD,
    match_margin=DEFAULT_MATCH_MARGIN,
):
    """Register one processed episode and return its speaker-to-character mapping.

    Speaker IDs are episode-local. Matching is one-to-one inside an episode, and
    low-confidence matches create a new character rather than silently merging.
    """
    manifest.setdefault("characters", [])
    manifest.setdefault("episodes", {})
    episode_key = str(episode_key)
    old_episode = manifest["episodes"].get(episode_key, {})
    old_speakers = old_episode.get("speakers", {}) if isinstance(old_episode, dict) else {}
    characters = manifest["characters"]
    by_id = _character_index(manifest)
    counts = speaker_counts or {}
    voices = speaker_to_voice or {}
    suggestions = name_candidates or {}
    assignments = {}
    used_character_ids = set()

    # Keep stable mappings when the same episode is reopened.
    for speaker in sorted(speaker_profiles):
        previous = old_speakers.get(speaker, {}) if isinstance(old_speakers, dict) else {}
        character_id = str(previous.get("character_id", ""))
        if character_id and character_id in by_id and character_id not in used_character_ids:
            assignments[speaker] = {
                "character_id": character_id,
                "similarity": (
                    float(previous.get("similarity"))
                    if previous.get("similarity") is not None else None
                ),
                "match_state": "existing",
            }
            used_character_ids.add(character_id)

    pending = [speaker for speaker in sorted(speaker_profiles) if speaker not in assignments]
    candidate_rows = []
    for speaker in pending:
        rankings = _candidate_rankings(
            speaker_profiles[speaker], characters, used_character_ids
        )
        top_score = rankings[0][0] if rankings else -1.0
        second_score = rankings[1][0] if len(rankings) > 1 else -1.0
        top_character = rankings[0][1] if rankings else ""
        if (
            top_character
            and top_score >= match_threshold
            and top_score - second_score >= match_margin
        ):
            candidate_rows.append((top_score, speaker, top_character))

    # Greedy highest-confidence one-to-one assignment prevents two speakers in
    # one episode from collapsing into the same series character.
    for similarity, speaker, character_id in sorted(
        candidate_rows, key=lambda item: (-item[0], item[1], item[2])
    ):
        if speaker in assignments or character_id in used_character_ids:
            continue
        assignments[speaker] = {
            "character_id": character_id,
            "similarity": round(float(similarity), 4),
            "match_state": "matched",
        }
        used_character_ids.add(character_id)

    for speaker in pending:
        if speaker in assignments:
            continue
        character_id = _next_character_id(characters)
        character = {
            "id": character_id,
            "name": "",
            "name_confirmed": False,
            "voice": str(voices.get(speaker, "") or ""),
            "voices": (
                {voice_scope: str(voices.get(speaker, "") or "")}
                if voice_scope and voices.get(speaker) else {}
            ),
            "voice_sources": (
                {voice_scope: "auto"}
                if voice_scope and voices.get(speaker) else {}
            ),
            "voice_source": "auto" if voices.get(speaker) else "",
            "centroid": [],
            "sample_count": 0,
            "episodes": {},
            "name_candidates": [],
        }
        characters.append(character)
        by_id[character_id] = character
        assignments[speaker] = {
            "character_id": character_id,
            "similarity": None,
            "match_state": "new",
        }
        used_character_ids.add(character_id)

    episode_speakers = {}
    for speaker, assignment in assignments.items():
        character = by_id[assignment["character_id"]]
        profile = speaker_profiles.get(speaker, {})
        previous = old_speakers.get(speaker, {}) if isinstance(old_speakers, dict) else {}
        already_registered = (
            str(previous.get("character_id", "")) == assignment["character_id"]
        )
        if not already_registered:
            _merge_centroid(
                character,
                profile.get("embedding") or [],
                profile.get("sample_count", 1),
            )
        if voices.get(speaker):
            if voice_scope:
                scoped_voices = character.setdefault("voices", {})
                if not scoped_voices and character.get("voice"):
                    scoped_voices[voice_scope] = str(character["voice"])
                voice_sources = character.setdefault("voice_sources", {})
                if voice_sources.get(voice_scope) != "manual":
                    scoped_voices[voice_scope] = str(voices[speaker])
                    voice_sources[voice_scope] = "auto"
            elif character.get("voice_source") != "manual":
                character["voice"] = str(voices[speaker])
                character["voice_source"] = "auto"
        character.setdefault("episodes", {})[episode_key] = {
            "episode_name": str(episode_name),
            "speaker": speaker,
            "line_count": int(counts.get(speaker, 0)),
        }
        _merge_name_candidates(character, suggestions.get(speaker, []))
        episode_speakers[speaker] = {
            **assignment,
            "line_count": int(counts.get(speaker, 0)),
            "embedding": list(profile.get("embedding") or []),
            "sample_count": int(profile.get("sample_count", 0)),
        }

    manifest["episodes"][episode_key] = {
        "name": str(episode_name),
        "speakers": episode_speakers,
    }
    return assignments


def set_character_name(manifest, character_id, name, *, confirmed=True):
    character = _character_index(manifest).get(str(character_id))
    if character is None:
        raise KeyError(character_id)
    character["name"] = str(name or "").strip()
    character["name_confirmed"] = bool(confirmed and character["name"])
    return character


def set_character_voice(
    manifest, character_id, voice, *, scope="", source="manual"
):
    character = _character_index(manifest).get(str(character_id))
    if character is None:
        raise KeyError(character_id)
    value = str(voice or "").strip()
    if scope:
        character.setdefault("voices", {})[scope] = value
        character.setdefault("voice_sources", {})[scope] = str(source or "manual")
    else:
        character["voice"] = value
        character["voice_source"] = str(source or "manual")
    return character


def merge_characters(manifest, target_id, source_ids):
    """Merge series characters when they never represent two speakers in one episode."""
    by_id = _character_index(manifest)
    target_id = str(target_id)
    target = by_id.get(target_id)
    if target is None:
        raise KeyError(target_id)
    source_ids = [
        str(value) for value in source_ids
        if str(value) and str(value) != target_id
    ]
    for source_id in source_ids:
        source = by_id.get(source_id)
        if source is None:
            raise KeyError(source_id)
        overlap = set(target.get("episodes", {})) & set(source.get("episodes", {}))
        if overlap:
            raise ValueError("同一集中的两个说话人不能合并为同一角色")

        _merge_centroid(
            target,
            source.get("centroid") or [],
            source.get("sample_count", 1),
        )
        target.setdefault("episodes", {}).update(source.get("episodes", {}))
        _merge_name_candidates(target, source.get("name_candidates", []))
        if not target.get("name_confirmed") and source.get("name_confirmed"):
            target["name"] = source.get("name", "")
            target["name_confirmed"] = True
        target.setdefault("voices", {}).update({
            scope: voice
            for scope, voice in source.get("voices", {}).items()
            if scope not in target.get("voices", {})
        })
        target_voice_sources = target.setdefault("voice_sources", {})
        for scope, voice_source in source.get("voice_sources", {}).items():
            target_voice_sources.setdefault(scope, voice_source)
        if not target.get("voice") and source.get("voice"):
            target["voice"] = source.get("voice", "")
            target["voice_source"] = source.get("voice_source", "")

        for episode in manifest.get("episodes", {}).values():
            for speaker_data in episode.get("speakers", {}).values():
                if str(speaker_data.get("character_id", "")) == source_id:
                    speaker_data["character_id"] = target_id

    removed = set(source_ids)
    manifest["characters"] = [
        character for character in manifest.get("characters", [])
        if str(character.get("id", "")) not in removed
    ]
    return target


def detach_episode_speaker(manifest, episode_key, speaker):
    """Recover from a false cross-episode match by creating a new character."""
    episode_key = str(episode_key)
    speaker = str(speaker)
    episode = manifest.get("episodes", {}).get(episode_key)
    if not episode or speaker not in episode.get("speakers", {}):
        raise KeyError(f"{episode_key}:{speaker}")
    speaker_data = episode["speakers"][speaker]
    old_character_id = str(speaker_data.get("character_id", ""))
    by_id = _character_index(manifest)
    old_character = by_id.get(old_character_id, {})
    character_id = _next_character_id(manifest.get("characters", []))
    episode_info = dict(old_character.get("episodes", {}).get(episode_key, {}))
    new_character = {
        "id": character_id,
        "name": "",
        "name_confirmed": False,
        "voice": str(old_character.get("voice", "") or ""),
        "voices": dict(old_character.get("voices", {})),
        "voice_sources": dict(old_character.get("voice_sources", {})),
        "voice_source": str(old_character.get("voice_source", "") or ""),
        "centroid": list(speaker_data.get("embedding") or []),
        "sample_count": int(speaker_data.get("sample_count", 0)),
        "episodes": {episode_key: episode_info},
        "name_candidates": [],
    }
    manifest.setdefault("characters", []).append(new_character)
    if old_character:
        old_character.setdefault("episodes", {}).pop(episode_key, None)
    speaker_data["character_id"] = character_id
    speaker_data["similarity"] = None
    speaker_data["match_state"] = "manual_split"
    return new_character


def character_display_name(character):
    name = str(character.get("name", "")).strip()
    if name:
        return name
    candidates = character.get("name_candidates", [])
    if candidates:
        candidate = str(candidates[0].get("name", "")).strip()
        if candidate:
            return f"{candidate}？"
    character_id = str(character.get("id", ""))
    match = re.search(r"(\d+)$", character_id)
    return f"角色 {int(match.group(1))}" if match else "未命名角色"


def infer_oversegmentation_speaker_count(speakers):
    """Return a conservative retry count when unlimited diarization exploded.

    This guard only activates for obviously fragmented results: more than 12
    labels and fewer than five subtitle lines per label on average.  The retry
    count is based on speakers that own at least 5% of the dialogue, so a normal
    ensemble result is left untouched.
    """
    labels = [str(value or "").strip() for value in (speakers or [])]
    labels = [value for value in labels if value]
    counts = Counter(labels)
    unique_count = len(counts)
    if unique_count <= 12 or not labels or len(labels) / unique_count >= 5:
        return None
    meaningful_lines = max(3, int(math.ceil(len(labels) * 0.05)))
    estimate = sum(1 for count in counts.values() if count >= meaningful_lines)
    if estimate < 2:
        estimate = min(4, unique_count)
    return estimate if 2 <= estimate <= 10 and estimate < unique_count else None


def previous_episode_speaker_count(
    output_dir, series_folder, episode_key, *, series_videos=None
):
    """Read a prior count from the scoped manifest, then the legacy folder one."""
    paths = [
        manifest_path_for_series(output_dir, series_folder, series_videos),
        manifest_path_for_series(output_dir, series_folder),
    ]
    seen = set()
    for path in paths:
        key = path.resolve().as_posix().casefold()
        if key in seen:
            continue
        seen.add(key)
        manifest = load_manifest(path, series_folder=series_folder)
        episode = manifest.get("episodes", {}).get(str(episode_key), {})
        count = len(episode.get("speakers", {})) if isinstance(episode, dict) else 0
        if count > 1:
            return count
    return None


def suggest_character_names(subtitles, speakers):
    """Return conservative name candidates; suggestions are never confirmed."""
    rows = []
    for index, subtitle in enumerate(subtitles or []):
        if index >= len(speakers):
            break
        text = str(subtitle.get("text", "")).strip()
        speaker = str(speakers[index] or "").strip()
        if speaker:
            rows.append((speaker, text, int(subtitle.get("line", index + 1))))

    found = defaultdict(dict)

    def add(speaker, name, score, evidence, line):
        name = str(name or "").strip()
        if name in _NAME_STOPWORDS or not re.fullmatch(_CJK_NAME, name):
            return
        item = {
            "name": name,
            "score": round(float(score), 2),
            "evidence": evidence,
            "line": int(line),
        }
        previous = found[speaker].get(name)
        if previous is None or item["score"] > previous["score"]:
            found[speaker][name] = item

    for index, (speaker, text, line) in enumerate(rows):
        for pattern in _SELF_INTRO_PATTERNS:
            match = pattern.search(text)
            if match:
                add(speaker, match.group("name"), 0.98, "self_intro", line)

        if index + 1 >= len(rows):
            continue
        next_speaker, _, _ = rows[index + 1]
        if next_speaker == speaker:
            continue
        match = _VOCATIVE_PATTERN.match(text)
        if match:
            add(next_speaker, match.group("name"), 0.55, "reply_to_vocative", line)

    return {
        speaker: sorted(
            candidates.values(),
            key=lambda item: (-item["score"], item["name"]),
        )
        for speaker, candidates in found.items()
    }


def extract_episode_speaker_embeddings(
    *,
    audio_file,
    subtitles,
    speakers,
    model_path,
    max_lines_per_speaker=8,
    min_line_ms=700,
):
    """Extract one normalized CAM++ centroid for every episode-local speaker."""
    diagnostics = []
    indices_by_speaker = defaultdict(list)
    for index, (subtitle, speaker) in enumerate(zip(subtitles, speakers)):
        speaker = str(speaker or "").strip()
        start = int(subtitle.get("start_time", 0))
        end = int(subtitle.get("end_time", 0))
        duration = end - start
        if not speaker or duration < min_line_ms:
            continue
        diagnostics.append({
            "start": start,
            "end": end,
            "duration_ms": duration,
            "speaker": speaker,
            "source_index": index,
        })

    ranked_by_speaker = defaultdict(list)
    for diagnostic_index, item in enumerate(diagnostics):
        ranked_by_speaker[item["speaker"]].append(
            (item["duration_ms"], diagnostic_index)
        )
    selected_indices = []
    for speaker, ranked in ranked_by_speaker.items():
        chosen = [
            index
            for _, index in sorted(ranked, key=lambda item: (-item[0], item[1]))[
                :max_lines_per_speaker
            ]
        ]
        indices_by_speaker[speaker].extend(chosen)
        selected_indices.extend(chosen)
    if not selected_indices:
        return {}

    from videotrans.process.speaker_refine import _load_embeddings

    embeddings = _load_embeddings(
        audio_file,
        diagnostics,
        selected_indices,
        model_path,
        min_audio_ms=min_line_ms,
    )
    profiles = {}
    for speaker, indices in indices_by_speaker.items():
        values = [embeddings[index] for index in indices if index in embeddings]
        if not values:
            continue
        centroid = np.mean(values, axis=0)
        centroid = centroid / (np.linalg.norm(centroid) + 1e-9)
        profiles[speaker] = {
            "embedding": [round(float(value), 7) for value in centroid],
            "sample_count": len(values),
        }
    return profiles


def speaker_counts(speakers):
    return dict(Counter(str(value or "").strip() for value in speakers if value))
