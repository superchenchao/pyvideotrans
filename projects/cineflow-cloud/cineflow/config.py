from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CINEFLOW_",
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    mode: Literal["demo", "production"] = "demo"
    hard_sla_seconds: int = Field(default=300, ge=60, le=300)
    max_video_seconds: int = Field(default=300, ge=1, le=300)
    max_input_bytes: int = Field(default=512 * 1024 * 1024, ge=1)
    max_inflight_jobs: int = Field(default=2, ge=1, le=64)
    sla_reserve_seconds: int = Field(default=20, ge=5, le=60)

    media_worker_url: str = ""
    asr_worker_url: str = ""
    speaker_worker_url: str = ""
    secondary_asr_worker_url: str = ""
    worker_bearer_token: str = ""

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"

    azure_speech_key: str = ""
    azure_speech_region: str = ""
    azure_speech_secondary_key: str = ""
    azure_speech_secondary_region: str = ""
    azure_tts_concurrency: int = Field(default=8, ge=1, le=20)

    # Conservative billing guardrails. Override whenever provider prices change.
    cost_asr_per_second: float = Field(default=0.00022, ge=0)
    cost_translation_per_character: float = Field(default=0.000003, ge=0)
    cost_azure_tts_per_character: float = Field(default=0.0000954, ge=0)
    cost_gpu_per_second: float = Field(default=0.0062, ge=0)
    cost_render_per_minute: float = Field(default=0.0651, ge=0)
    cost_separation_per_minute: float = Field(default=0.10, ge=0)
    cost_subtitle_removal_per_minute: float = Field(default=0.40, ge=0)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
