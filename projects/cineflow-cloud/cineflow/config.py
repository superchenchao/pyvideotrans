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

    # Five minutes is a performance target, never a terminal deadline. Jobs may
    # queue and continue beyond this value until they succeed or a real provider
    # error occurs.
    target_processing_seconds: int = Field(default=300, ge=60, le=1800)
    max_video_seconds: int = Field(default=300, ge=1, le=300)
    max_input_bytes: int = Field(default=512 * 1024 * 1024, ge=1)
    max_inflight_jobs: int = Field(default=2, ge=1, le=64)

    # This standalone service receives an OSS URL produced by the existing
    # upload/subtitle-removal flow. It deliberately does not import that client.
    media_worker_url: str = ""
    secondary_media_worker_url: str = ""
    media_worker_timeout_seconds: float = Field(default=300.0, ge=10, le=3600)
    asr_worker_url: str = ""
    secondary_asr_worker_url: str = ""
    asr_worker_timeout_seconds: float = Field(default=300.0, ge=10, le=3600)
    speaker_worker_url: str = ""
    secondary_speaker_worker_url: str = ""
    speaker_worker_timeout_seconds: float = Field(default=300.0, ge=10, le=3600)
    worker_bearer_token: str = ""

    # Translation is intentionally fixed to the same DeepSeek-compatible path
    # used by the existing project, but implemented here as an independent client.
    translation_provider: Literal["deepseek"] = "deepseek"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_max_tokens: int = Field(default=65536, ge=1024, le=131072)
    deepseek_thinking: bool = False

    azure_speech_key: str = ""
    azure_speech_region: str = ""
    azure_speech_secondary_key: str = ""
    azure_speech_secondary_region: str = ""
    azure_tts_concurrency: int = Field(default=8, ge=1, le=20)

    # Conservative billing guardrails. OSS upload and burned-subtitle removal
    # remain upstream and therefore are intentionally excluded from this quote.
    cost_asr_per_second: float = Field(default=0.00022, ge=0)
    cost_translation_per_character: float = Field(default=0.000003, ge=0)
    cost_azure_tts_per_character: float = Field(default=0.0000954, ge=0)
    cost_gpu_per_second: float = Field(default=0.0062, ge=0)
    cost_render_per_minute: float = Field(default=0.0651, ge=0)
    cost_separation_per_minute: float = Field(default=0.10, ge=0)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
