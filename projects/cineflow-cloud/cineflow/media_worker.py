from __future__ import annotations

import asyncio
import base64
import binascii
import importlib.util
import os

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import DubbingArtifact, JobRequest, MediaArtifacts, OutputArtifact, Transcript
from .providers.aliyun_ice import AliyunICEClient, AliyunICEConfig, AliyunICEError
from .providers.aliyun_media import (
    AliyunMediaConfig,
    AliyunMediaError,
    AliyunMediaService,
)
from .providers.aliyun_oss import AliyunOSSConfig, AliyunOSSError, AliyunOSSStore


class MediaWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CINEFLOW_MEDIA_",
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    host: str = "0.0.0.0"
    port: int = 8093
    max_concurrency: int = Field(default=4, ge=1, le=32)
    bearer_token: str = ""
    max_artifact_bytes: int = Field(default=32 * 1024 * 1024, ge=1024)

    aliyun_region: str = "cn-beijing"
    aliyun_access_key_id: str = ""
    aliyun_access_key_secret: str = ""
    aliyun_security_token: str = ""
    aliyun_connect_timeout_seconds: float = 15.0
    aliyun_request_timeout_seconds: float = 60.0
    aliyun_poll_interval_seconds: float = 1.0
    aliyun_job_timeout_seconds: float = 900.0

    oss_endpoint: str = "https://oss-cn-beijing.aliyuncs.com"
    oss_bucket: str = ""
    oss_prefix: str = "cineflow"
    oss_signed_url_ttl_seconds: int = Field(default=86400, ge=300, le=604800)

    background_gain: float = Field(default=0.25, ge=0, le=4)
    output_bitrate_kbps: int = Field(default=3000, ge=256, le=5000)
    hard_subtitle_font: str = "Alibaba PuHuiTi"
    hard_subtitle_font_size: int = Field(default=54, ge=12, le=240)
    hard_subtitle_y: float = Field(default=0.88, ge=0, le=1)
    hard_subtitle_text_width: float = Field(default=0.9, gt=0, le=1)
    hard_subtitle_outline: int = Field(default=2, ge=0, le=20)

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


class ArtifactUploadRequest(BaseModel):
    name: str = Field(min_length=1, max_length=240)
    content_base64: str = Field(min_length=1)


class AssembleRequest(BaseModel):
    job: JobRequest
    media: MediaArtifacts
    translated: Transcript
    dubbing: DubbingArtifact


def create_service(settings: MediaWorkerSettings) -> AliyunMediaService:
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
    media_config = AliyunMediaConfig(
        background_gain=settings.background_gain,
        output_bitrate_kbps=settings.output_bitrate_kbps,
        hard_subtitle_font=settings.hard_subtitle_font,
        hard_subtitle_font_size=settings.hard_subtitle_font_size,
        hard_subtitle_y=settings.hard_subtitle_y,
        hard_subtitle_text_width=settings.hard_subtitle_text_width,
        hard_subtitle_outline=settings.hard_subtitle_outline,
    )
    return AliyunMediaService(ice, store, media_config)


class MediaRuntime:
    def __init__(
        self,
        settings: MediaWorkerSettings,
        *,
        service: AliyunMediaService | None = None,
    ) -> None:
        self.settings = settings
        self._service_injected = service is not None
        self.service = service or create_service(settings)
        self.semaphore = asyncio.Semaphore(settings.max_concurrency)

    def health(self) -> dict[str, object]:
        ice_ready = bool(self.service.ice.configured)
        oss_ready = bool(self.service.store.configured)
        sdk_ready = self._service_injected or bool(
            importlib.util.find_spec("aliyunsdkcore")
            and importlib.util.find_spec("oss2")
        )
        ready = ice_ready and oss_ready and sdk_ready
        details = []
        if not ice_ready:
            details.append("Alibaba ICE credentials or region missing")
        if not oss_ready:
            details.append("OSS endpoint, bucket, or credentials missing")
        if not sdk_ready:
            details.append("install the aliyun optional dependencies")
        return {
            "ok": ready,
            "healthy": ready,
            "warm": ready,
            "backend": "aliyun_ice",
            "detail": "; ".join(details) if details else "Alibaba ICE and OSS ready",
            "max_concurrency": self.settings.max_concurrency,
            "region": self.settings.aliyun_region,
            "bucket": self.settings.oss_bucket,
        }

    async def prepare(self, request: JobRequest) -> MediaArtifacts:
        async with self.semaphore:
            return await self.service.prepare(request)

    async def save_artifact(self, request: ArtifactUploadRequest) -> dict[str, str]:
        try:
            content = base64.b64decode(request.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise AliyunMediaError("content_base64 is not valid Base64") from exc
        if len(content) > self.settings.max_artifact_bytes:
            raise AliyunMediaError(
                f"artifact exceeds {self.settings.max_artifact_bytes} byte limit"
            )
        async with self.semaphore:
            return await self.service.save_artifact(request.name, content)

    async def assemble(self, request: AssembleRequest) -> OutputArtifact:
        async with self.semaphore:
            return await self.service.assemble(
                request.job,
                request.media,
                request.translated,
                request.dubbing,
            )


def create_app(
    settings: MediaWorkerSettings | None = None,
    *,
    service: AliyunMediaService | None = None,
) -> FastAPI:
    resolved = settings or MediaWorkerSettings()
    runtime = MediaRuntime(resolved, service=service)
    app = FastAPI(
        title="CineFlow Alibaba Media Worker",
        version="0.4.0",
        description=(
            "Alibaba ICE media preparation, MusicDemix, generated-artifact OSS "
            "storage, timeline dubbing, hard subtitles, and final assembly."
        ),
    )

    def authorize(authorization: str | None) -> None:
        token = resolved.bearer_token.strip()
        if token and authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="invalid worker bearer token")

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return runtime.health()

    @app.post("/v1/prepare", response_model=MediaArtifacts)
    async def prepare(
        request: JobRequest,
        authorization: str | None = Header(default=None),
    ) -> MediaArtifacts:
        authorize(authorization)
        try:
            return await runtime.prepare(request)
        except (AliyunICEError, AliyunOSSError, AliyunMediaError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/v1/artifacts/base64")
    async def save_artifact(
        request: ArtifactUploadRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, str]:
        authorize(authorization)
        try:
            return await runtime.save_artifact(request)
        except (AliyunICEError, AliyunOSSError, AliyunMediaError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/v1/assemble", response_model=OutputArtifact)
    async def assemble(
        request: AssembleRequest,
        authorization: str | None = Header(default=None),
    ) -> OutputArtifact:
        authorize(authorization)
        try:
            return await runtime.assemble(request)
        except (AliyunICEError, AliyunOSSError, AliyunMediaError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    app.state.runtime = runtime
    return app


settings = MediaWorkerSettings()
app = create_app(settings)


def run() -> None:
    uvicorn.run(
        "cineflow.media_worker:app",
        host=os.getenv("CINEFLOW_MEDIA_HOST", settings.host),
        port=int(os.getenv("CINEFLOW_MEDIA_PORT", str(settings.port))),
    )
