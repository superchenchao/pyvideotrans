from __future__ import annotations

from collections.abc import Iterable

from ..models import JobRequest, SubtitleLine, Transcript, WordTiming


class ASRProviderError(RuntimeError):
    """Normalized failure raised by an external speech recognition provider."""


def select_input_url(request: JobRequest) -> str:
    """Prefer an existing audio artifact, then cleaned video, then the source video."""

    source = request.source_audio_url or request.clean_video_url or request.input_url
    return str(source)


def normalize_language(language: str) -> str:
    value = str(language or "").strip().lower().replace("_", "-")
    if value.startswith(("zh", "cmn", "yue")):
        return "zh"
    aliases = {"fil": "tl", "nb": "no"}
    primary = value.split("-", 1)[0]
    return aliases.get(primary, primary or "zh")


def normalize_speaker(value: object) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).strip().lower()
    for prefix in ("speaker_", "speaker", "spk_", "spk"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    return f"spk{text or '0'}"


def transcript_from_rows(
    *,
    language: str,
    provider: str,
    rows: Iterable[dict[str, object]],
    task_id: str = "",
    usage_seconds: float | None = None,
    metadata: dict[str, object] | None = None,
) -> Transcript:
    normalized: list[SubtitleLine] = []
    for row in rows:
        try:
            start_ms = int(row["start_ms"])
            end_ms = int(row["end_ms"])
        except (KeyError, TypeError, ValueError):
            continue
        text = str(row.get("text", "") or "").strip()
        if not text or end_ms <= start_ms:
            continue

        words: list[WordTiming] = []
        for word in row.get("words", []) or []:
            try:
                word_start = int(word["start_ms"])
                word_end = int(word["end_ms"])
            except (KeyError, TypeError, ValueError):
                continue
            if word_end < word_start:
                continue
            words.append(
                WordTiming(
                    start_ms=word_start,
                    end_ms=word_end,
                    text=str(word.get("text", "") or ""),
                    punctuation=str(word.get("punctuation", "") or ""),
                )
            )

        normalized.append(
            SubtitleLine(
                line_id=len(normalized) + 1,
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
                speaker_id=normalize_speaker(row.get("speaker_id")),
                words=words,
            )
        )

    normalized.sort(key=lambda item: (item.start_ms, item.end_ms, item.line_id))
    for index, item in enumerate(normalized, 1):
        item.line_id = index
    if not normalized:
        raise ASRProviderError(f"{provider} returned no usable transcription lines")

    return Transcript(
        language=language,
        lines=normalized,
        provider=provider,
        task_id=task_id,
        usage_seconds=usage_seconds,
        metadata=metadata or {},
    )
