from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, status
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .ingest_models import (
    MultipartRegistration,
    UploadCompleteRequest,
    UploadCredentials,
    UploadSession,
    UploadSessionRequest,
    UploadState,
    UploadValidation,
)
from .providers.aliyun_oss import AliyunOSSConfig, AliyunOSSError, AliyunOSSStore
from .providers.aliyun_sts import AliyunSTSClient, AliyunSTSConfig, AliyunSTSError
from .state import FileStateStore, StateStoreError

_SAFE_FILENAME = re.compile(r"[^0-9A-Za-z._-]+")
_NAMESPACE = "upload-sessions"


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def safe_filename(value: str) -> str:
    name = PurePosixPath(str(value or "").replace("\\", "/")).name
    name = _SAFE_FILENAME.sub("-", name).strip("-.")
    return name[:180] or "video.mp4"


class UploadWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CINEFLOW_UPLOAD_",
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    host: str = "0.0.0.0"
    port: int = 8094
    state_dir: str = "./state/uploads"
    bearer_token: str = ""
    max_file_bytes: int = Field(default=20 * 1024 * 1024 * 1024, ge=1)
    credential_duration_seconds: int = Field(default=3600, ge=900, le=43_200)
    stale_session_seconds: int = Field(default=86_400, ge=900)

    aliyun_region: str = "cn-beijing"
    aliyun_role_arn: str = ""
    aliyun_sts_endpoint: str = ""
    aliyun_access_key_id: str = ""
    aliyun_access_key_secret: str = ""
    aliyun_security_token: str = ""

    oss_endpoint: str = "https://oss-cn-beijing.aliyuncs.com"
    oss_bucket: str = ""
    oss_source_prefix: str = "cineflow/sources"
    oss_signed_url_ttl_seconds: int = Field(default=86_400, ge=300, le=604_800)

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


def create_oss_store(settings: UploadWorkerSettings) -> AliyunOSSStore:
    return AliyunOSSStore(
        AliyunOSSConfig(
            endpoint=settings.oss_endpoint,
            bucket=settings.oss_bucket,
            prefix=settings.oss_source_prefix,
            access_key_id=settings.access_key_id(),
            access_key_secret=settings.access_key_secret(),
            security_token=settings.security_token(),
            signed_url_ttl_seconds=settings.oss_signed_url_ttl_seconds,
        )
    )


def create_sts_client(settings: UploadWorkerSettings) -> AliyunSTSClient:
    return AliyunSTSClient(
        AliyunSTSConfig(
            region=settings.aliyun_region,
            role_arn=settings.aliyun_role_arn,
            endpoint=settings.aliyun_sts_endpoint,
            access_key_id=settings.access_key_id(),
            access_key_secret=settings.access_key_secret(),
            security_token=settings.security_token(),
            duration_seconds=settings.credential_duration_seconds,
        )
    )


class UploadRuntime:
    def __init__(
        self,
        settings: UploadWorkerSettings,
        *,
        state_store: FileStateStore | None = None,
        oss_store: AliyunOSSStore | None = None,
        sts_client: AliyunSTSClient | None = None,
    ) -> None:
        self.settings = settings
        self.state = state_store or FileStateStore(settings.state_dir)
        self.oss = oss_store or create_oss_store(settings)
        self.sts = sts_client or create_sts_client(settings)

    def health(self) -> dict[str, object]:
        ready = bool(self.oss.configured and self.sts.configured)
        details = []
        if not self.oss.configured:
            details.append("OSS endpoint, bucket, or server credentials missing")
        if not self.sts.configured:
            details.append("STS role ARN, region, or server credentials missing")
        return {
            "ok": ready,
            "healthy": ready,
            "warm": ready,
            "backend": "aliyun_sts+oss_multipart",
            "detail": "; ".join(details) if details else "Alibaba STS and OSS ready",
            "bucket": self.settings.oss_bucket,
            "endpoint": self.settings.oss_endpoint,
            "max_file_bytes": self.settings.max_file_bytes,
        }

    async def _load(self, session_id: str) -> UploadSession:
        payload = await self.state.get(_NAMESPACE, session_id)
        if payload is None:
            raise KeyError(session_id)
        return UploadSession.model_validate(payload)

    async def _save(self, session: UploadSession) -> None:
        persisted = session.model_copy(update={"credentials": None})
        await self.state.put(_NAMESPACE, session.session_id, persisted.model_dump(mode="json"))

    async def _credentials(self, session: UploadSession) -> UploadCredentials:
        policy = self.sts.object_policy(
            session.bucket,
            session.object_key,
            allow_read=True,
        )
        temporary = await self.sts.aassume_role(
            role_session_name=f"cineflow-upload-{session.session_id[:24]}",
            policy=policy,
            duration_seconds=self.settings.credential_duration_seconds,
        )
        return UploadCredentials(
            access_key_id=temporary.access_key_id,
            access_key_secret=temporary.access_key_secret,
            security_token=temporary.security_token,
            expiration=temporary.expiration,
            expiration_epoch=temporary.expiration_epoch,
        )

    async def create_session(self, request: UploadSessionRequest) -> UploadSession:
        if request.size_bytes > self.settings.max_file_bytes:
            raise ValueError(
                f"file size {request.size_bytes} exceeds {self.settings.max_file_bytes} byte limit"
            )
        session_id = uuid.uuid4().hex
        now = utc_now()
        object_key = self.oss.key(
            datetime.now(UTC).strftime("%Y/%m/%d"),
            safe_filename(request.project_id),
            session_id,
            safe_filename(request.filename),
        )
        session = UploadSession(
            session_id=session_id,
            state=UploadState.CREATED,
            endpoint=self.settings.oss_endpoint,
            bucket=self.settings.oss_bucket,
            object_key=object_key,
            canonical_url=self.oss.canonical_url(object_key),
            size_bytes=request.size_bytes,
            content_type=(
                request.content_type.strip()
                or mimetypes.guess_type(request.filename)[0]
                or "application/octet-stream"
            ),
            sha256=request.sha256,
            created_at=now,
            updated_at=now,
        )
        await self._save(session)
        credentials = await self._credentials(session)
        return session.model_copy(update={"credentials": credentials})

    async def refresh_credentials(self, session_id: str) -> UploadSession:
        session = await self._load(session_id)
        if session.state in {UploadState.ABORTED, UploadState.FAILED}:
            raise ValueError(f"upload session is {session.state.value}")
        if session.state == UploadState.COMPLETED:
            return session.model_copy(
                update={"download_url": self.oss.signed_url(session.object_key)}
            )
        credentials = await self._credentials(session)
        return session.model_copy(update={"credentials": credentials})

    async def register_multipart(
        self,
        session_id: str,
        request: MultipartRegistration,
    ) -> UploadSession:
        session = await self._load(session_id)
        if session.state == UploadState.COMPLETED:
            raise ValueError("upload session is already completed")
        if session.state == UploadState.ABORTED:
            raise ValueError("upload session is aborted")
        updated = session.model_copy(
            update={
                "state": UploadState.UPLOADING,
                "multipart_upload_id": request.upload_id,
                "updated_at": utc_now(),
            }
        )
        await self._save(updated)
        return updated

    async def validate(self, session: UploadSession) -> UploadValidation:
        head = await asyncio.to_thread(self.oss.head_object, session.object_key)
        actual_size = int(head.get("content_length", 0) or 0)
        metadata = head.get("metadata", {})
        actual_sha = str(metadata.get("sha256", "") or "").lower()
        warnings: list[str] = []
        valid = actual_size == session.size_bytes
        if session.sha256:
            if not actual_sha:
                warnings.append("OSS object is missing x-oss-meta-sha256")
                valid = False
            elif actual_sha != session.sha256:
                warnings.append("OSS SHA-256 metadata does not match the upload session")
                valid = False
        content_type = str(head.get("content_type", "") or "")
        if session.content_type and content_type and content_type != session.content_type:
            warnings.append(
                f"content type differs: expected {session.content_type}, got {content_type}"
            )
        download_url = self.oss.signed_url(session.object_key) if valid else ""
        return UploadValidation(
            valid=valid,
            object_key=session.object_key,
            size_bytes=actual_size,
            expected_size_bytes=session.size_bytes,
            content_type=content_type,
            sha256=actual_sha,
            expected_sha256=session.sha256,
            etag=str(head.get("etag", "") or ""),
            download_url=download_url,
            warnings=warnings,
        )

    async def complete(
        self,
        session_id: str,
        request: UploadCompleteRequest,
    ) -> tuple[UploadSession, UploadValidation]:
        session = await self._load(session_id)
        if request.size_bytes != session.size_bytes:
            raise ValueError("client completion size does not match the upload session")
        if session.sha256 and request.sha256 and request.sha256 != session.sha256:
            raise ValueError("client completion SHA-256 does not match the upload session")
        validation = await self.validate(session)
        now = utc_now()
        if not validation.valid:
            failed = session.model_copy(
                update={
                    "state": UploadState.FAILED,
                    "updated_at": now,
                    "error": "; ".join(validation.warnings) or "OSS validation failed",
                }
            )
            await self._save(failed)
            return failed, validation
        completed = session.model_copy(
            update={
                "state": UploadState.COMPLETED,
                "updated_at": now,
                "completed_at": now,
                "download_url": validation.download_url,
                "error": "",
            }
        )
        await self._save(completed)
        return completed, validation

    async def abort(self, session_id: str, *, delete_object: bool) -> UploadSession:
        session = await self._load(session_id)
        errors: list[str] = []
        if session.multipart_upload_id and session.state != UploadState.COMPLETED:
            try:
                await asyncio.to_thread(
                    self.oss.abort_multipart_upload,
                    session.object_key,
                    session.multipart_upload_id,
                )
            except AliyunOSSError as exc:
                errors.append(str(exc))
        if delete_object:
            try:
                exists = await asyncio.to_thread(self.oss.object_exists, session.object_key)
                if exists:
                    await asyncio.to_thread(self.oss.delete_object, session.object_key)
            except AliyunOSSError as exc:
                errors.append(str(exc))
        updated = session.model_copy(
            update={
                "state": UploadState.ABORTED,
                "updated_at": utc_now(),
                "download_url": "",
                "error": "; ".join(errors),
            }
        )
        await self._save(updated)
        return updated

    async def cleanup(
        self,
        *,
        older_than_seconds: int,
        delete_completed: bool,
    ) -> dict[str, object]:
        threshold = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
        cleaned: list[str] = []
        failed: dict[str, str] = {}
        for payload in await self.state.list(_NAMESPACE):
            session = UploadSession.model_validate(payload)
            try:
                updated_at = parse_utc(session.updated_at)
            except ValueError:
                updated_at = datetime.min.replace(tzinfo=UTC)
            if updated_at > threshold:
                continue
            if session.state == UploadState.COMPLETED and not delete_completed:
                continue
            try:
                await self.abort(session.session_id, delete_object=True)
                cleaned.append(session.session_id)
            except Exception as exc:
                failed[session.session_id] = str(exc)
        return {"cleaned": cleaned, "failed": failed}


def create_app(
    settings: UploadWorkerSettings | None = None,
    *,
    runtime: UploadRuntime | None = None,
) -> FastAPI:
    resolved = settings or UploadWorkerSettings()
    service = runtime or UploadRuntime(resolved)
    app = FastAPI(
        title="CineFlow OSS Upload Worker",
        version="0.5.0",
        description=(
            "Scoped STS issuance, multipart registration, durable resume state, "
            "OSS object validation, abort, and cleanup."
        ),
    )

    def authorize(authorization: str | None) -> None:
        token = resolved.bearer_token.strip()
        if token and authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="invalid upload worker bearer token")

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return service.health()

    @app.post(
        "/v1/uploads/sessions",
        response_model=UploadSession,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_session(
        request: UploadSessionRequest,
        authorization: str | None = Header(default=None),
    ) -> UploadSession:
        authorize(authorization)
        try:
            return await service.create_session(request)
        except (AliyunSTSError, AliyunOSSError, StateStoreError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/v1/uploads/cleanup")
    async def cleanup(
        older_than_seconds: int = Query(default=resolved.stale_session_seconds, ge=900),
        delete_completed: bool = False,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        return await service.cleanup(
            older_than_seconds=older_than_seconds,
            delete_completed=delete_completed,
        )

    @app.get("/v1/uploads/{session_id}", response_model=UploadSession)
    async def get_session(
        session_id: str,
        authorization: str | None = Header(default=None),
    ) -> UploadSession:
        authorize(authorization)
        try:
            return await service._load(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="upload session not found") from exc

    @app.post("/v1/uploads/{session_id}/credentials", response_model=UploadSession)
    async def refresh_credentials(
        session_id: str,
        authorization: str | None = Header(default=None),
    ) -> UploadSession:
        authorize(authorization)
        try:
            return await service.refresh_credentials(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="upload session not found") from exc
        except (AliyunSTSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/uploads/{session_id}/multipart", response_model=UploadSession)
    async def register_multipart(
        session_id: str,
        request: MultipartRegistration,
        authorization: str | None = Header(default=None),
    ) -> UploadSession:
        authorize(authorization)
        try:
            return await service.register_multipart(session_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="upload session not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/uploads/{session_id}/validate", response_model=UploadValidation)
    async def validate_upload(
        session_id: str,
        authorization: str | None = Header(default=None),
    ) -> UploadValidation:
        authorize(authorization)
        try:
            return await service.validate(await service._load(session_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="upload session not found") from exc
        except AliyunOSSError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/v1/uploads/{session_id}/complete")
    async def complete_upload(
        session_id: str,
        request: UploadCompleteRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        try:
            session, validation = await service.complete(session_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="upload session not found") from exc
        except (AliyunOSSError, StateStoreError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not validation.valid:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "OSS object validation failed",
                    "session": session.model_dump(mode="json"),
                    "validation": validation.model_dump(mode="json"),
                },
            )
        return {
            "session": session.model_dump(mode="json"),
            "validation": validation.model_dump(mode="json"),
        }

    @app.delete("/v1/uploads/{session_id}", response_model=UploadSession)
    async def abort_upload(
        session_id: str,
        delete_object: bool = True,
        authorization: str | None = Header(default=None),
    ) -> UploadSession:
        authorize(authorization)
        try:
            return await service.abort(session_id, delete_object=delete_object)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="upload session not found") from exc

    app.state.runtime = service
    return app


settings = UploadWorkerSettings()
app = create_app(settings)


def run() -> None:
    uvicorn.run(
        "cineflow.upload_worker:app",
        host=os.getenv("CINEFLOW_UPLOAD_HOST", settings.host),
        port=int(os.getenv("CINEFLOW_UPLOAD_PORT", str(settings.port))),
    )
