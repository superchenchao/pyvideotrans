from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence

Box = tuple[int, int, int, int]


def _normalize_box(box: Sequence[int], frame_size: tuple[int, int] | None, padding: int) -> Box:
    xmin, xmax, ymin, ymax = (int(value) for value in box)
    xmin, xmax = sorted((xmin, xmax))
    ymin, ymax = sorted((ymin, ymax))
    xmin -= padding
    xmax += padding
    ymin -= padding
    ymax += padding
    if frame_size:
        width, height = frame_size
        xmin = max(0, min(xmin, width - 1))
        xmax = max(xmin + 1, min(xmax, width))
        ymin = max(0, min(ymin, height - 1))
        ymax = max(ymin + 1, min(ymax, height))
    return xmin, xmax, ymin, ymax


def _vertical_overlap_ratio(left: Box, right: Box) -> float:
    overlap = max(0, min(left[3], right[3]) - max(left[2], right[2]))
    shortest = max(1, min(left[3] - left[2], right[3] - right[2]))
    return overlap / shortest


def _horizontal_gap(left: Box, right: Box) -> int:
    if left[1] < right[0]:
        return right[0] - left[1]
    if right[1] < left[0]:
        return left[0] - right[1]
    return 0


def _should_merge(left: Box, right: Box, max_horizontal_gap: int) -> bool:
    overlaps = not (
        left[1] < right[0]
        or right[1] < left[0]
        or left[3] < right[2]
        or right[3] < left[2]
    )
    same_text_line = (
        _vertical_overlap_ratio(left, right) >= 0.45
        and _horizontal_gap(left, right) <= max_horizontal_gap
    )
    return overlaps or same_text_line


def _union(left: Box, right: Box) -> Box:
    return (
        min(left[0], right[0]),
        max(left[1], right[1]),
        min(left[2], right[2]),
        max(left[3], right[3]),
    )


def merge_boxes(boxes: Iterable[Box], max_horizontal_gap: int = 24) -> list[Box]:
    """Merge overlapping boxes and neighboring boxes on the same text line."""
    merged: list[Box] = []
    for candidate in sorted(set(boxes), key=lambda item: (item[2], item[0], item[3], item[1])):
        index = 0
        while index < len(merged):
            if _should_merge(candidate, merged[index], max_horizontal_gap):
                candidate = _union(candidate, merged.pop(index))
                index = 0
                continue
            index += 1
        merged.append(candidate)
    return sorted(merged, key=lambda item: (item[2], item[0]))


def merge_temporal_frame_boxes(
    frame_boxes: Mapping[int, Sequence[Sequence[int]]],
    *,
    lookbehind: int = 4,
    lookahead: int = 4,
    first_frame: int = 1,
    last_frame: int | None = None,
    frame_size: tuple[int, int] | None = None,
    spatial_padding: int = 6,
    max_horizontal_gap: int = 24,
) -> dict[int, list[Box]]:
    """Expand OCR boxes in time, then union boxes contributed to each frame.

    OCR commonly misses the first or last few frames of a subtitle. Expanding each
    detection in both directions makes the inpainting mask stable across fades and
    subtitle switches. Boxes from adjacent detections are merged per frame so a
    partially detected sentence still produces one complete mask.
    """
    if lookbehind < 0 or lookahead < 0:
        raise ValueError("lookbehind and lookahead must be non-negative")
    if spatial_padding < 0:
        raise ValueError("spatial_padding must be non-negative")

    expanded: dict[int, list[Box]] = defaultdict(list)
    for raw_frame, raw_boxes in frame_boxes.items():
        frame = int(raw_frame)
        start = max(first_frame, frame - lookbehind)
        end = frame + lookahead
        if last_frame is not None:
            end = min(end, last_frame)
        if end < start:
            continue
        normalized = [
            _normalize_box(box, frame_size, spatial_padding)
            for box in raw_boxes
            if len(box) == 4
        ]
        for target_frame in range(start, end + 1):
            expanded[target_frame].extend(normalized)

    return {
        frame: merge_boxes(boxes, max_horizontal_gap=max_horizontal_gap)
        for frame, boxes in sorted(expanded.items())
        if boxes
    }
