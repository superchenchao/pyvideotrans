from __future__ import annotations

import re
from difflib import SequenceMatcher

from .models import SubtitleLine, Transcript

_TIMESTAMP = re.compile(
    r"^(?P<hours>\d{1,2}):(?P<minutes>\d{2}):(?P<seconds>\d{2})"
    r"[,.](?P<millis>\d{1,3})$"
)


def _timestamp_ms(value: str) -> int:
    match = _TIMESTAMP.match(value.strip())
    if match is None:
        raise ValueError(f"invalid SRT timestamp: {value}")
    millis = int(match.group("millis").ljust(3, "0"))
    return (
        int(match.group("hours")) * 3_600_000
        + int(match.group("minutes")) * 60_000
        + int(match.group("seconds")) * 1000
        + millis
    )


def parse_srt(content: str, *, language: str, provider: str, task_id: str = "") -> Transcript:
    """Parse a provider-generated SRT without running any local OCR model."""

    text = content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines: list[SubtitleLine] = []
    for block in re.split(r"\n\s*\n", text):
        rows = [row.strip() for row in block.splitlines()]
        rows = [row for row in rows if row]
        if not rows:
            continue
        timing_index = next((index for index, row in enumerate(rows) if "-->" in row), None)
        if timing_index is None:
            continue
        start_text, end_text = [part.strip() for part in rows[timing_index].split("-->", 1)]
        try:
            start_ms = _timestamp_ms(start_text.split()[0])
            end_ms = _timestamp_ms(end_text.split()[0])
        except (IndexError, ValueError):
            continue
        subtitle_text = "\n".join(rows[timing_index + 1 :]).strip()
        if not subtitle_text or end_ms <= start_ms:
            continue
        lines.append(
            SubtitleLine(
                line_id=len(lines) + 1,
                start_ms=start_ms,
                end_ms=end_ms,
                text=subtitle_text,
                source="ocr",
                metadata={"provider_line_number": rows[0] if timing_index > 0 else ""},
            )
        )
    if not lines:
        raise ValueError("cloud OCR returned an SRT without usable subtitle lines")
    return Transcript(
        language=language,
        lines=lines,
        provider=provider,
        task_id=task_id,
        metadata={"recognition_source": "ocr", "line_count": len(lines)},
    )


def _overlap_ms(left: SubtitleLine, right: SubtitleLine) -> int:
    return max(0, min(left.end_ms, right.end_ms) - max(left.start_ms, right.start_ms))


def _coverage(subject: SubtitleLine, other: SubtitleLine) -> float:
    duration = max(1, subject.end_ms - subject.start_ms)
    return _overlap_ms(subject, other) / duration


def _normalized_text(value: str) -> str:
    return "".join(character.casefold() for character in value if character.isalnum())


def _similarity(left: str, right: str) -> float:
    normalized_left = _normalized_text(left)
    normalized_right = _normalized_text(right)
    if not normalized_left or not normalized_right:
        return 0.0
    return SequenceMatcher(None, normalized_left, normalized_right).ratio()


def fuse_asr_and_ocr(
    asr: Transcript,
    ocr: Transcript,
    *,
    asr_only_threshold: float = 0.35,
) -> Transcript:
    """Use OCR text/timing for visible captions and ASR for speakers and missing speech.

    This is deterministic server-side transcript fusion. It does not run Whisper,
    Tesseract, PaddleOCR, OpenCV, or any other local recognition model.
    """

    fused: list[SubtitleLine] = []
    conflict_line_ids: list[int] = []
    matched_asr_ids: set[int] = set()

    for ocr_line in ocr.lines:
        ranked = sorted(
            asr.lines,
            key=lambda line: (
                _overlap_ms(ocr_line, line),
                _similarity(ocr_line.text, line.text),
            ),
            reverse=True,
        )
        best = ranked[0] if ranked and _overlap_ms(ocr_line, ranked[0]) > 0 else None
        metadata = dict(ocr_line.metadata)
        metadata.update({"recognition_source": "ocr", "ocr_line_id": ocr_line.line_id})
        speaker_id = None
        words = []
        if best is not None:
            matched_asr_ids.add(best.line_id)
            overlap = _coverage(ocr_line, best)
            text_similarity = _similarity(ocr_line.text, best.text)
            metadata.update(
                {
                    "matched_asr_line_id": best.line_id,
                    "asr_overlap_ratio": round(overlap, 4),
                    "asr_text_similarity": round(text_similarity, 4),
                    "asr_text": best.text,
                }
            )
            speaker_id = best.speaker_id
            words = best.words
            if overlap >= 0.5 and text_similarity < 0.4:
                conflict_line_ids.append(ocr_line.line_id)
        fused.append(
            SubtitleLine(
                line_id=0,
                start_ms=ocr_line.start_ms,
                end_ms=ocr_line.end_ms,
                text=ocr_line.text,
                speaker_id=speaker_id,
                words=words,
                source="ocr",
                confidence=ocr_line.confidence,
                metadata=metadata,
            )
        )

    asr_only_count = 0
    for asr_line in asr.lines:
        maximum_coverage = max(
            (_coverage(asr_line, ocr_line) for ocr_line in ocr.lines),
            default=0.0,
        )
        if maximum_coverage >= asr_only_threshold:
            continue
        asr_only_count += 1
        metadata = dict(asr_line.metadata)
        metadata.update(
            {
                "recognition_source": "asr_only",
                "asr_line_id": asr_line.line_id,
                "maximum_ocr_coverage": round(maximum_coverage, 4),
            }
        )
        fused.append(
            asr_line.model_copy(
                update={
                    "line_id": 0,
                    "source": "asr",
                    "metadata": metadata,
                }
            )
        )

    fused.sort(key=lambda line: (line.start_ms, line.end_ms, line.source))
    reindexed = [line.model_copy(update={"line_id": index}) for index, line in enumerate(fused, 1)]
    return Transcript(
        language=ocr.language or asr.language,
        lines=reindexed,
        provider="hybrid_cloud_ocr_asr",
        task_id="|".join(value for value in (ocr.task_id, asr.task_id) if value),
        usage_seconds=asr.usage_seconds,
        metadata={
            "recognition_source": "hybrid",
            "ocr_provider": ocr.provider,
            "asr_provider": asr.provider,
            "ocr_line_count": len(ocr.lines),
            "asr_line_count": len(asr.lines),
            "asr_only_line_count": asr_only_count,
            "matched_asr_line_count": len(matched_asr_ids),
            "text_conflict_ocr_line_ids": conflict_line_ids,
        },
    )
