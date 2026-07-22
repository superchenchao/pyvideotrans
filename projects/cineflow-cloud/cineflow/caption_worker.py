from __future__ import annotations

import asyncio
import importlib.util
import os

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import JobRequest, Transcript
from .providers.aliyun_caption import (
    AliyunCaptionConfig,
    AliyunCaptionError,
    AliyunCaptionExtractor,
)
from .providers.aliyun_ice import AliyunICEClient, AliyunICEConfig
from .providers.aliyun_oss import AliyunOSSConfig, AliyunOSSStore


class CaptionWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CINEFLOW_CAPTION_",
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    host: str = "0.0.0.0"
    port: int = 8096
    bearer_token: str = ""
    max_concurrency: int = Field(default=4, ge=1, le=32)

    aliyun_region: str = "cn-beijing"
    aliyun_access_key_id: str = ""
    aliyun_access_key_secret: str = ""
    aliyun_security_token: str = ""
    aliyun_connect_timeout_seconds: float = 15.0
    aliyun_request_timeout_seconds: float = 60.0
    aliyun_poll_interval_seconds: float = 1.0
    aliyun_job_timeout_seconds: float = 900.0
    aliyun_model_id: str = ""

    oss_endpoint: str = "https://oss-cn-beijing.aliyuncs.com"
    oss_bucket: str = ""
    oss_prefix: str = "cineflow/captions"
    oss_signed_url_ttl_seconds: int = Field(default=86_400, ge=300, le=604_800)
    output_download_timeout_seconds: float = Field(default=60.0, ge=5, le=600)

    def access_key_id(self) -> str:
        return (
            self.aliyun_access_key_id.strip()
            or os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "").strip()
            or os.getenv("OSS_ACCESS_KEY_ID", "").strip()
        )

    def access_key_secret(self) -> str:
        return (
            self.aliyun_access_key_secret.strip()
            or os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "").strip()
            or os.getenv("OSS_ACCESS_KEY_SECRET", "").strip()
        )

    def security_token(self) -> str:
        return (
            self.aliyun_security_token.strip()
            or os.getenv("ALIBABA_CLOUD_SECURITY_TOKEN", "").strip()
            or os.getenv("OSS_SESSION_TOKEN", "").strip()
        )


def create_extractor(settings: CaptionWorkerSettings) -> AliyunCaptionExtractor:
    access_key_id = settings.access_key_id()
    access_key_secret = settings.access_key_secret()
    security_token = settings.security_token()
    ice = AliyunICEClient(
        AliyunICEConfig(
            region=settings.aliyun_region,
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            security_token=security_token,
            connect_timeout_seconds=settings.aliyun_connect_timeout_seconds,
            request_timeout_seconds=settings.aliyun_request_timeout_seconds,
            poll_interval_seconds=settings.aliyun_poll_interval_seconds,
            job_timeout_seconds=settings.aliyun_job_timeout_seconds,
        )
    )
    store = AliyunOSSStore(
        AliyunOSSConfig(
            endpoint=settings.oss_endpoint,
            bucket=settings.oss_bucket,
            prefix=settings.oss_prefix,
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            security_token=security_token,
            signed_url_ttl_seconds=settings.oss_signed_url_ttl_seconds,
        )
    )
    return AliyunCaptionExtractor(
        ice,
        store,
        AliyunCaptionConfig(
            download_timeout_seconds=settings.output_download_timeout_seconds,
            model_id=settings.aliyun_model_id,
        ),
    )


class CaptionRuntime:
    def __init__(
        self,
        settings: CaptionWorkerSettings,
        *,
        extractor: AliyunCaptionExtractor | None = None,
    ) -> None:
        self.settings = settings
        self._extractor_injected = extractor is not None
        self.extractor = extractor or create_extractor(settings)
        self.semaphore = asyncio.Semaphore(settings.max_concurrency)

    def health(self) -> dict[str, object]:
        sdk_ready = self._extractor_injected or bool(
            importlib.util.find_spec("aliyunsdkcore") and importlib.util.find_spec("oss2")
        )
        ready = bool(self.extractor.configured and sdk_ready)
        details = []
        if not self.extractor.configured:
            details.append("Alibaba ICE/OSS credentials, region, endpoint, or bucket missing")
        if not sdk_ready:
            details.append("install the aliyun optional dependencies")
        return {
            "ok": ready,
            "healthy": ready,
            "warm": ready,
            "backend": "aliyun_caption_extraction_api",
            "detail": "; ".join(details) if details else "Alibaba cloud OCR ready",
            "local_ocr": False,
            "max_concurrency": self.settings.max_concurrency,
            "region": self.settings.aliyun_region,
        }

    async def extract(self, request: JobRequest) -> Transcript:
        async with self.semaphore:
            return await self.extractor.extract(request)


def create_app(
    settings: CaptionWorkerSettings | None = None,
    *,
    extractor: AliyunCaptionExtractor | None = None,
) -> FastAPI:
    resolved = settings or CaptionWorkerSettings()
    runtime = CaptionRuntime(resolved, extractor=extractor)
    app = FastAPI(
        title="CineFlow Alibaba Cloud OCR Caption Worker",
        version="0.7.0",
        description=(
            "Visible subtitle extraction through Alibaba CaptionExtraction. "
            "No local OCR model or local frame inference is used."
        ),
    )

    def authorize(authorization: str | None) -> None:
        token = resolved.bearer_token.strip()
        if token and authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="invalid caption worker bearer token")

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return runtime.health()

    @app.post("/v1/extract", response_model=Transcript)
    async def extract(
        request: JobRequest,
        authorization: str | None = Header(default=None),
    ) -> Transcript:
        authorize(authorization)
        try:
            return await runtime.extract(request)
        except (AliyunCaptionError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    app.state.runtime = runtime
    return app


settings = CaptionWorkerSettings()
app = create_app(settings)


def run() -> None:
    uvicorn.run(
        "cineflow.caption_worker:app",
        host=os.getenv("CINEFLOW_CAPTION_HOST", settings.host),
        port=int(os.getenv("CINEFLOW_CAPTION_PORT", str(settings.port))),
    )
