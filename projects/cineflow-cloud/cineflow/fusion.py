from __future__ import annotations

import math
from collections import defaultdict

from .models import CandidateScore, LineEvidence, SpeakerDecision, SubtitleLine

_EPSILON = 1e-6


def _candidate_map(items: list[CandidateScore]) -> dict[str, float]:
    return {item.character_id: max(_EPSILON, item.score) for item in items}


def _weights(evidence: LineEvidence) -> tuple[float, float, float]:
    if evidence.offscreen:
        return 0.72, 0.0, 0.28
    visual = 0.52 * evidence.av_sync_confidence
    audio = 0.30 + (0.12 * (1.0 - evidence.av_sync_confidence))
    text = max(0.10, 1.0 - audio - visual)
    if evidence.overlap_speech:
        audio *= 0.82
        text += 0.08
    total = audio + visual + text
    return audio / total, visual / total, text / total


def _line_scores(evidence: LineEvidence) -> dict[str, float]:
    audio = _candidate_map(evidence.audio)
    visual = _candidate_map(evidence.visual)
    text = _candidate_map(evidence.text)
    candidates = set(audio) | set(visual) | set(text)
    if not candidates:
        return {"unknown": math.log(_EPSILON)}
    wa, wv, wt = _weights(evidence)
    return {
        candidate: (
            wa * math.log(audio.get(candidate, _EPSILON))
            + wv * math.log(visual.get(candidate, _EPSILON))
            + wt * math.log(text.get(candidate, _EPSILON))
        )
        for candidate in candidates
    }


def fuse_speakers(
    lines: list[SubtitleLine],
    evidence_rows: list[LineEvidence],
    *,
    switch_penalty: float = 0.16,
    review_threshold: float = 0.72,
) -> list[SpeakerDecision]:
    """Decode the most plausible speaker sequence with dynamic multimodal weights."""

    by_line = {row.line_id: row for row in evidence_rows}
    scores = [
        _line_scores(by_line.get(line.line_id, LineEvidence(line_id=line.line_id)))
        for line in lines
    ]
    if not scores:
        return []

    paths: list[dict[str, tuple[float, str | None]]] = []
    paths.append({candidate: (score, None) for candidate, score in scores[0].items()})
    for index in range(1, len(scores)):
        current: dict[str, tuple[float, str | None]] = {}
        previous = paths[-1]
        row = by_line.get(lines[index].line_id)
        local_penalty = switch_penalty * (0.35 if row and row.overlap_speech else 1.0)
        for candidate, emission in scores[index].items():
            best_score = -math.inf
            best_previous: str | None = None
            for previous_candidate, (previous_score, _) in previous.items():
                transition = 0.0 if candidate == previous_candidate else -local_penalty
                total = previous_score + transition + emission
                if total > best_score:
                    best_score = total
                    best_previous = previous_candidate
            current[candidate] = (best_score, best_previous)
        paths.append(current)

    final_candidate = max(paths[-1], key=lambda candidate: paths[-1][candidate][0])
    decoded = [final_candidate]
    for index in range(len(paths) - 1, 0, -1):
        previous_candidate = paths[index][decoded[-1]][1]
        decoded.append(previous_candidate or decoded[-1])
    decoded.reverse()

    decisions: list[SpeakerDecision] = []
    for line, selected, line_scores in zip(lines, decoded, scores, strict=True):
        ranked = sorted(line_scores.values(), reverse=True)
        margin = ranked[0] - ranked[1] if len(ranked) > 1 else 4.0
        confidence = 1.0 / (1.0 + math.exp(-margin))
        decisions.append(
            SpeakerDecision(
                line_id=line.line_id,
                character_id=selected,
                confidence=round(confidence, 4),
                needs_review=confidence < review_threshold,
            )
        )
    return decisions


def merge_evidence(*groups: list[LineEvidence]) -> list[LineEvidence]:
    """Merge evidence emitted by independent audio, visual and text workers."""

    merged: dict[int, dict[str, object]] = defaultdict(
        lambda: {
            "audio": [],
            "visual": [],
            "text": [],
            "offscreen": False,
            "overlap_speech": False,
            "av_sync_confidence": 1.0,
        }
    )
    for group in groups:
        for row in group:
            target = merged[row.line_id]
            target["audio"].extend(row.audio)  # type: ignore[union-attr]
            target["visual"].extend(row.visual)  # type: ignore[union-attr]
            target["text"].extend(row.text)  # type: ignore[union-attr]
            target["offscreen"] = bool(target["offscreen"] or row.offscreen)
            target["overlap_speech"] = bool(target["overlap_speech"] or row.overlap_speech)
            target["av_sync_confidence"] = min(
                float(target["av_sync_confidence"]), row.av_sync_confidence
            )
    return [LineEvidence(line_id=line_id, **values) for line_id, values in sorted(merged.items())]
