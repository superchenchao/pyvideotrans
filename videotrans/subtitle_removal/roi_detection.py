from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

Box = tuple[int, int, int, int]
Area = tuple[int, int, int, int]


def interpolate_sampled_boxes(
    sampled: Mapping[int, Sequence[Box]],
    sample_step: int,
) -> dict[int, list[Box]]:
    """Fill small gaps between sampled OCR frames using the earlier mask."""
    if sample_step < 1:
        raise ValueError("sample_step must be positive")

    interpolated: dict[int, list[Box]] = {}
    frame_numbers = sorted(int(frame) for frame in sampled)
    max_gap = sample_step * 2
    for frame, next_frame in zip(frame_numbers, frame_numbers[1:]):
        boxes = list(sampled[frame])
        interpolated[frame] = boxes
        if next_frame - frame <= max_gap:
            for fill_frame in range(frame + 1, next_frame):
                interpolated[fill_frame] = boxes
    if frame_numbers:
        last_frame = frame_numbers[-1]
        interpolated[last_frame] = list(sampled[last_frame])
    return interpolated


def detect_boxes_in_areas(
    image: Any,
    areas: Sequence[Area],
    detect: Callable[[Any], Sequence[Box]],
) -> list[Box]:
    """Run detection on cropped areas and map boxes back to frame coordinates.

    Areas use the subtitle remover's `(ymin, ymax, xmin, xmax)` convention,
    while returned boxes use `(xmin, xmax, ymin, ymax)`.
    """
    if not areas:
        return list(detect(image))

    frame_height, frame_width = image.shape[:2]
    detected: list[Box] = []
    for raw_ymin, raw_ymax, raw_xmin, raw_xmax in areas:
        ymin = max(0, min(int(raw_ymin), frame_height))
        ymax = max(ymin, min(int(raw_ymax), frame_height))
        xmin = max(0, min(int(raw_xmin), frame_width))
        xmax = max(xmin, min(int(raw_xmax), frame_width))
        if xmax <= xmin or ymax <= ymin:
            continue

        crop = image[ymin:ymax, xmin:xmax]
        for box_xmin, box_xmax, box_ymin, box_ymax in detect(crop):
            detected.append(
                (
                    int(box_xmin) + xmin,
                    int(box_xmax) + xmin,
                    int(box_ymin) + ymin,
                    int(box_ymax) + ymin,
                )
            )
    return detected
