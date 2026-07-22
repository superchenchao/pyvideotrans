from __future__ import annotations

import asyncio
import os
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import JobRequest, Transcript
from .providers.aliyun_asr import AliyunFunASRClient, AliyunFunASRConfig
from .providers.asr_common import ASRProviderError
from .providers.volcengine_asr import (
    VolcengineASRConfig,
    VolcengineFlashASRClient,
)


class ASRWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CINEFLOW_ASR_",
        env_file=".env",
        extra="ignore",
    )

    backend: Literal["aliyun", "volcengine"] = "aliyun"
    host: str = "0.0.0.0"
    port: int = 8091
    max_concurrency: int = Field(default=4, ge=1, le=32)

    aliyun_api_key: str = ""
    aliyun_workspace_id: str = ""
    aliyun_region: str = "cn-beijing"
    aliyun_base_url: str = ""
    aliyun_model: str = "fun-asr"
    aliyun_poll_interval_seconds: float = 0.75
    aliyun_task_timeout_seconds: float = 240.0

    volcengine_api_key: str = ""
    volcengine_app_id: str = ""
    volcengine_access_token: str = ""
    volcengine_endpoint: str = (
        "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
    )
    volcengine_resource_id: str = "volc.bigasr.auc_turbo"
    volcengine_model_version: str = "400"
    volcengine_ffmpeg_binary: str = "ffmpeg"
    volcengine_attempts: int = 3


def create_provider(settings: ASRWorkerSettings):
    if settings.backend == "aliyun":
        return AliyunFunASRClient(
            AliyunFunASRConfig(
                api_key=settings.aliyun_api_key,
                workspace_id=settings.aliyun_workspace_id,
                region=settings.aliyun_region,
                base_url=settings.aliyun_base_url,
                model=settings.aliyun_model,
                poll_interval_seconds=settings.aliyun_poll_interval_seconds,
                task_timeout_seconds=settings.aliyun_task_timeout_seconds,
            )
        )
    return VolcengineFlashASRClient(
        VolcengineASRConfig(
            api_key=settings.volcengine_api_key,
            app_id=settings.volcengine_app_id,
            access_token=settings.volcengine_access_token,
            endpoint=settings.volcengine_endpoint,
            resource_id=settings.volcengine_resource_id,
            model_version=settings.volcengine_model_version,
            ffmpeg_binary=settings.volcengine_ffmpeg_binary,
            attempts=settings.volcengine_attempts,
        )
    )


class ASRRuntime:
    def __init__(self, settings: ASRWorkerSettings, provider=None) -> None:
        self.settings = settings
        self.provider = provider or create_provider(settings)
        self.semaphore = asyncio.Semaphore(settings.max_concurrency)

    def health(self) -> dict[str, object]:
        configured = bool(self.provider.configured)
        ready = bool(getattr(self.provider, "ready", configured))
        return {
            "ok": ready,
            "healthy": ready,
            "warm": ready,
            "backend": self.settings.backend,
            "detail": self.provider.health_detail(),
            "max_concurrency": self.settings.max_concurrency,
        }

    async def transcribe(self, request: JobRequest) -> Transcript:
        ready = bool(getattr(self.provider, "ready", bool(self.provider.configured)))
        if not ready:
            raise ASRProviderError(self.provider.health_detail())
        async with self.semaphore:
            return await self.provider.transcribe(request)


def create_app(
    settings: ASRWorkerSettings | None = None,
    provider=None,
) -> FastAPI:
    resolved = settings or ASRWorkerSettings()
    runtime = ASRRuntime(resolved, provider=provider)
    app = FastAPI(
        title="CineFlow ASR Worker",
        version="0.3.0",
        description=(
            "Normalized cloud ASR adapter. Deploy Alibaba Fun-ASR as the preferred "
            "worker and Volcengine BigModel Flash as the fallback worker."
        ),
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return runtime.health()

    @app.post("/v1/transcribe", response_model=Transcript)
    async def transcribe(request: JobRequest) -> Transcript:
        try:
            return await runtime.transcribe(request)
        except ASRProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    app.state.runtime = runtime
    return app


settings = ASRWorkerSettings()
app = create_app(settings)


def run() -> None:
    uvicorn.run(
        "cineflow.asr_worker:app",
        host=os.getenv("CINEFLOW_ASR_HOST", settings.host),
        port=int(os.getenv("CINEFLOW_ASR_PORT", str(settings.port))),
    )
