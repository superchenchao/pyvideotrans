from __future__ import annotations

import re
import math
from difflib import SequenceMatcher
from typing import Iterable, List, Mapping, Sequence, Tuple

from videotrans.task.taskcfg import SrtItem
from videotrans.util.help_srt import ms_to_time_string

try:
    import zhconv
except ImportError:  # OCR can still work in a minimal installation.
    zhconv = None


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_NORMALIZE_RE = re.compile(r"[^0-9A-Za-z\u3400-\u4dbf\u4e00-\u9fff]+")
_CONTINUATION_SUFFIXES = frozenset("的给把被和跟与及或在向对从为让")


def normalize_subtitle_text(text: str) -> str:
    return _NORMALIZE_RE.sub("", text or "").lower()


def simplify_ocr_text(text: str) -> str:
    text = (text or "").strip()
    return zhconv.convert(text, "zh-hans") if zhconv else text


def is_spoken_subtitle_text(text: str) -> bool:
    """Keep Chinese dialogue/narration and reject English song lyrics/noise."""
    chinese_count = len(_CJK_RE.findall(text or ""))
    latin_count = len(_LATIN_RE.findall(text or ""))
    return chinese_count > 0 and chinese_count >= latin_count


def _texts_are_similar(left: str, right: str, threshold: float = 0.72) -> bool:
    left_norm = normalize_subtitle_text(left)
    right_norm = normalize_subtitle_text(right)
    if not left_norm or not right_norm:
        return False
    if left_norm == right_norm:
        return True
    if min(len(left_norm), len(right_norm)) >= 2 and (
            left_norm in right_norm or right_norm in left_norm):
        length_ratio = min(len(left_norm), len(right_norm)) / max(
            len(left_norm), len(right_norm)
        )
        # Containment alone does not mean two adjacent captions are the same.
        # For example, "妈妈" follows "爸爸妈妈没吵架" as a separate line.
        # Only treat containment as a shortcut when the texts have comparable
        # lengths; otherwise let the requested similarity threshold decide.
        if length_ratio >= 0.65:
            return True
    return SequenceMatcher(None, left_norm, right_norm).ratio() >= threshold


def _make_srt_item(*, line: int, text: str, start_time: int, end_time: int) -> SrtItem:
    start_time = max(0, int(start_time))
    end_time = max(start_time + 1, int(end_time))
    startraw = ms_to_time_string(ms=start_time)
    endraw = ms_to_time_string(ms=end_time)
    return SrtItem(
        line=line,
        text=text.strip(),
        start_time=start_time,
        end_time=end_time,
        startraw=startraw,
        endraw=endraw,
        time=f"{startraw} --> {endraw}",
    )


def _canonical_observation(group: Sequence[Mapping]) -> Tuple[str, float]:
    variants = {}
    for item in group:
        text = str(item.get("text", "")).strip()
        normalized = normalize_subtitle_text(text)
        if not normalized:
            continue
        score = float(item.get("score", 0.0) or 0.0)
        entry = variants.setdefault(normalized, {
            "text": text,
            "votes": 0.0,
            "count": 0,
            "best_score": 0.0,
        })
        entry["votes"] += max(0.1, score)
        entry["count"] += 1
        if score >= entry["best_score"]:
            entry["best_score"] = score
            entry["text"] = text

    if not variants:
        return "", 0.0

    winner = max(
        variants.values(),
        key=lambda item: (
            item["votes"],
            item["count"],
            item["best_score"],
            len(normalize_subtitle_text(item["text"])),
        ),
    )
    mean_score = winner["votes"] / max(1, winner["count"])
    return simplify_ocr_text(winner["text"]), min(1.0, mean_score)


def build_ocr_subtitles(
        observations: Iterable[Mapping], *, sample_ms: int = 250) -> List[SrtItem]:
    """Turn sampled OCR observations into stable, de-duplicated subtitle cues."""
    clean = []
    for item in observations:
        text = str(item.get("text", "")).strip()
        score = float(item.get("score", 0.0) or 0.0)
        if score < 0.65 or not is_spoken_subtitle_text(text):
            continue
        clean.append({
            "time_ms": max(0, int(item.get("time_ms", 0) or 0)),
            "text": text,
            "score": score,
        })
    clean.sort(key=lambda item: item["time_ms"])
    if not clean:
        return []

    groups = []
    current = [clean[0]]
    max_gap = max(350, int(sample_ms * 1.8))
    for item in clean[1:]:
        previous = current[-1]
        if (item["time_ms"] - previous["time_ms"] <= max_gap
                and _texts_are_similar(previous["text"], item["text"])):
            current.append(item)
        else:
            groups.append(current)
            current = [item]
    groups.append(current)

    half_sample = max(1, sample_ms // 2)
    raw_cues = []
    for group in groups:
        text, score = _canonical_observation(group)
        if not text:
            continue
        normalized = normalize_subtitle_text(text)
        start_time = max(0, group[0]["time_ms"] - half_sample)
        end_time = group[-1]["time_ms"] + half_sample
        duration = end_time - start_time
        if len(group) == 1 and (score < 0.92 or duration < 200):
            continue
        if len(normalized) == 1 and (len(group) < 2 or score < 0.85):
            continue
        raw_cues.append({
            "text": text,
            "score": score,
            "start_time": start_time,
            "end_time": end_time,
        })

    merged = []
    # Only bridge a gap that can plausibly be one missed OCR sample. A fixed
    # 750ms window merged two visibly separate, identical captions such as two
    # consecutive "怎么可以" lines.
    merge_gap = max(200, int(sample_ms * 1.25))
    for cue in raw_cues:
        if (merged
                and cue["start_time"] - merged[-1]["end_time"] <= merge_gap
                and _texts_are_similar(merged[-1]["text"], cue["text"], threshold=0.82)):
            previous = merged[-1]
            previous["end_time"] = max(previous["end_time"], cue["end_time"])
            if cue["score"] > previous["score"]:
                previous["text"] = cue["text"]
                previous["score"] = cue["score"]
        else:
            merged.append(cue)

    return [
        _make_srt_item(
            line=index,
            text=cue["text"],
            start_time=cue["start_time"],
            end_time=cue["end_time"],
        )
        for index, cue in enumerate(merged, 1)
    ]


def _overlaps(left, right, padding_ms: int = 600) -> bool:
    return (
        int(left["end_time"]) + padding_ms >= int(right["start_time"])
        and int(right["end_time"]) + padding_ms >= int(left["start_time"])
    )


def _align_ocr_timing(ocr_item, matching_asr: Sequence) -> Tuple[int, int]:
    start_time = int(ocr_item["start_time"])
    end_time = int(ocr_item["end_time"])
    if not matching_asr:
        return start_time, end_time

    asr_text = "".join(str(item["text"]) for item in matching_asr)
    similarity = SequenceMatcher(
        None,
        normalize_subtitle_text(ocr_item["text"]),
        normalize_subtitle_text(asr_text),
    ).ratio()
    if similarity < 0.45:
        return start_time, end_time

    asr_start = min(int(item["start_time"]) for item in matching_asr)
    asr_end = max(int(item["end_time"]) for item in matching_asr)
    if abs(asr_start - start_time) <= 900:
        start_time = min(start_time, asr_start)
    if abs(asr_end - end_time) <= 900:
        end_time = max(end_time, asr_end)
    return start_time, end_time


def _recover_repeated_asr_text(ocr_item, matching_asr: Sequence) -> str:
    """Recover repeated calls hidden by an unchanged hard-subtitle frame.

    OCR sees one continuous cue when the same word remains on screen. Repeat it
    only when two or more adjacent ASR cues contain exactly that word and each
    cue is strongly covered by the OCR interval. The coverage requirement keeps
    partially overlapping ASR splits from duplicating a normal single caption.
    """
    ocr_text = str(ocr_item["text"])
    ocr_normalized = normalize_subtitle_text(ocr_text)
    if not ocr_normalized or len(ocr_normalized) > 4:
        return ocr_text

    strong_matches = []
    for asr_item in sorted(matching_asr, key=lambda item: int(item["start_time"])):
        if normalize_subtitle_text(str(asr_item["text"])) != ocr_normalized:
            continue
        asr_start = int(asr_item["start_time"])
        asr_end = int(asr_item["end_time"])
        duration = max(1, asr_end - asr_start)
        overlap = max(
            0,
            min(int(ocr_item["end_time"]), asr_end)
            - max(int(ocr_item["start_time"]), asr_start),
        )
        if overlap / duration >= 0.7:
            strong_matches.append(asr_item)

    if len(strong_matches) < 2:
        return ocr_text
    if any(
            int(current["start_time"]) - int(previous["end_time"]) > 200
            for previous, current in zip(strong_matches, strong_matches[1:])):
        return ocr_text
    return "".join(str(item["text"]) for item in strong_matches)


def _merge_continuous_ocr_fragments(
        ocr_subtitles: Sequence, asr_subtitles: Sequence) -> List[SrtItem]:
    """Join one spoken sentence that was rendered across consecutive frames."""
    merged: List[SrtItem] = []
    for current in ocr_subtitles:
        current_item = _make_srt_item(
            line=len(merged) + 1,
            text=str(current["text"]),
            start_time=int(current["start_time"]),
            end_time=int(current["end_time"]),
        )
        if not merged:
            merged.append(current_item)
            continue

        previous = merged[-1]
        gap = int(current_item["start_time"]) - int(previous["end_time"])
        previous_text = str(previous["text"])
        combined_text = previous_text + str(current_item["text"])
        combined_normalized = normalize_subtitle_text(combined_text)
        asr_supported = any(
            int(asr_item["start_time"]) < int(previous["end_time"])
            and int(asr_item["end_time"]) > int(current_item["start_time"])
            and _texts_are_similar(
                combined_text,
                str(asr_item["text"]),
                threshold=0.72,
            )
            for asr_item in asr_subtitles
        )
        continuation = (
            bool(previous_text)
            and previous_text[-1] in _CONTINUATION_SUFFIXES
        )
        if -50 <= gap <= 120 and (asr_supported or continuation):
            previous["text"] = combined_text
            _set_item_timing(
                previous,
                int(previous["start_time"]),
                int(current_item["end_time"]),
            )
            continue
        merged.append(current_item)

    for index, item in enumerate(merged, 1):
        item["line"] = index
    return merged


def _set_item_timing(item: SrtItem, start_time: int, end_time: int) -> None:
    item["start_time"] = int(start_time)
    item["end_time"] = int(end_time)
    item["startraw"] = ms_to_time_string(ms=item["start_time"])
    item["endraw"] = ms_to_time_string(ms=item["end_time"])
    item["time"] = f'{item["startraw"]} --> {item["endraw"]}'


def fuse_ocr_with_asr(
        asr_subtitles: Sequence, ocr_subtitles: Sequence, *,
        min_dominant_cues: int = 5) -> Tuple[List[SrtItem], Mapping]:
    """
    Use a stable hard-subtitle track as the authority and ASR for timing/fallback.

    In OCR-dominant mode, ASR-only cues are intentionally omitted. For fully
    captioned short dramas this removes song lyrics and Whisper music
    hallucinations while preserving quiet dialogue recovered from the picture.
    """
    asr_clean = [item for item in asr_subtitles if str(item["text"]).strip()]
    ocr_clean = [item for item in ocr_subtitles if str(item["text"]).strip()]
    ocr_clean = _merge_continuous_ocr_fragments(ocr_clean, asr_clean)
    ocr_chars = sum(len(normalize_subtitle_text(item["text"])) for item in ocr_clean)
    supported_ocr_cues = sum(
        any(_overlaps(ocr_item, asr_item) for asr_item in asr_clean)
        for ocr_item in ocr_clean
    )
    minimum_supported = max(2, math.ceil(len(ocr_clean) * 0.15))
    dominant = (
        len(ocr_clean) >= min_dominant_cues
        and ocr_chars >= 12
        and supported_ocr_cues >= minimum_supported
    )
    if not dominant:
        result = []
        for index, item in enumerate(asr_clean, 1):
            result.append(_make_srt_item(
                line=index,
                text=str(item["text"]),
                start_time=int(item["start_time"]),
                end_time=int(item["end_time"]),
            ))
        return result, {
            "mode": "asr",
            "asr_cues": len(asr_clean),
            "ocr_cues": len(ocr_clean),
            "supported_ocr_cues": supported_ocr_cues,
            "output_cues": len(result),
            "dropped_asr_only": 0,
        }

    result = []
    matched_asr_indexes = set()
    for ocr_item in ocr_clean:
        matching_indexes = [
            index for index, asr_item in enumerate(asr_clean)
            if _overlaps(ocr_item, asr_item, padding_ms=0)
        ]
        if not matching_indexes:
            matching_indexes = [
                index for index, asr_item in enumerate(asr_clean)
                if (_overlaps(ocr_item, asr_item)
                    and _texts_are_similar(
                        str(ocr_item["text"]),
                        str(asr_item["text"]),
                        threshold=0.35,
                    ))
            ]
        matching_asr = [asr_clean[index] for index in matching_indexes]
        matched_asr_indexes.update(matching_indexes)
        start_time, end_time = _align_ocr_timing(ocr_item, matching_asr)
        result.append(_make_srt_item(
            line=len(result) + 1,
            text=_recover_repeated_asr_text(ocr_item, matching_asr),
            start_time=start_time,
            end_time=end_time,
        ))

    result.sort(key=lambda item: (item["start_time"], item["end_time"]))
    for previous, current in zip(result, result[1:]):
        if previous["end_time"] >= current["start_time"]:
            low = int(previous["start_time"]) + 1
            high = int(current["end_time"]) - 2
            if low > high:
                continue
            boundary = (int(previous["end_time"]) + int(current["start_time"])) // 2
            boundary = min(high, max(low, boundary))
            _set_item_timing(previous, previous["start_time"], boundary)
            _set_item_timing(current, boundary + 1, current["end_time"])
    for index, item in enumerate(result, 1):
        item["line"] = index

    return result, {
        "mode": "ocr_dominant",
        "asr_cues": len(asr_clean),
        "ocr_cues": len(ocr_clean),
        "supported_ocr_cues": supported_ocr_cues,
        "matched_asr_cues": len(matched_asr_indexes),
        "dropped_asr_only": len(asr_clean) - len(matched_asr_indexes),
        "output_cues": len(result),
    }
