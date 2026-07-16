"""Subtitle removal helpers and engine integration."""

from .engine import EngineSpec, find_subtitle_remover_engine
from .automation import normalize_rect, remove_burned_subtitles, scale_normalized_rect
from .temporal_boxes import merge_temporal_frame_boxes

__all__ = [
    "EngineSpec",
    "find_subtitle_remover_engine",
    "merge_temporal_frame_boxes",
    "normalize_rect",
    "remove_burned_subtitles",
    "scale_normalized_rect",
]
