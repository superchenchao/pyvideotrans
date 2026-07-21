"""Subtitle removal helpers and engine integration."""

from .engine import EngineSpec, find_subtitle_remover_engine
from .automation import normalize_rect, remove_burned_subtitles, scale_normalized_rect
from .cloud import (
    ALIYUN_IMS_PROVIDER,
    CACA_PROVIDER,
    CLOUD_PROVIDERS,
    cleanup_cloud_objects,
    LOCAL_PROVIDER,
    cloud_strategy_key,
    normalize_provider,
    remove_burned_subtitles_cloud,
    validate_cloud_configuration,
)
from .strategy import strategy_cache_key
from .temporal_boxes import merge_temporal_frame_boxes

__all__ = [
    "EngineSpec",
    "ALIYUN_IMS_PROVIDER",
    "CACA_PROVIDER",
    "CLOUD_PROVIDERS",
    "cleanup_cloud_objects",
    "LOCAL_PROVIDER",
    "cloud_strategy_key",
    "find_subtitle_remover_engine",
    "merge_temporal_frame_boxes",
    "normalize_rect",
    "remove_burned_subtitles",
    "remove_burned_subtitles_cloud",
    "normalize_provider",
    "strategy_cache_key",
    "scale_normalized_rect",
    "validate_cloud_configuration",
]
