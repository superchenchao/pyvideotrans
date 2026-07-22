from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, status
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .media_probe import probe_media, validate_probe
from .providers.aliyun_ice import AliyunICEClient, AliyunICEConfig
from .providers.aliyun_oss import AliyunOSSConfig, AliyunOSSError, AliyunOSSStore
from .providers.subtitle_aliyun import AliyunSubtitleConfig, AliyunVideoDetextProvider
from .providers.subtitle_base import SubtitleRemovalProvider
from .providers.subtitle_caca import CacaSubtitleConfig, CacaSubtitleProvider
from .providers.subtitle_local import LocalSubtitleConfig, LocalSubtitleProvider
from .state import FileStateStore, StateStoreError
from .subtitle_models import (
    ProviderSubmission,
    SubtitleCleanupResult,
    SubtitleJobState,
    SubtitleOutputValidation,
    SubtitleProviderHealth,
    SubtitleProviderName,
    SubtitleRemovalJob,
    SubtitleRemovalRequest,
)

_NAMESPACE = "subtitle-removal-jobs"
_SAFE_NAME = re.compile(r"[^0-9A-Za-z._-]+")


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def safe_name(value: str, fallback: str = "clean.mp4") -> str:
    name = PurePosixPath(str(value or "").replace("\\", "/")).name
    name = _SAFE_NAME.sub("-", name).strip("-.")
    return name[:180] or fallback


class SubtitleWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CINEFLOW_SUBTITLE_",
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    host: str = "0.0.0.0"
    port: int = 8095
    state_dir: str = "./state/subtitles"
    bearer_token: str = ""
    max_concurrency: int = Field(default=2, ge=1, le=32)
    provider_order: str = "aliyun,caca,local"
    stale_job_seconds: int = Field(default=7 * 86_400, ge=900)
    max_remote_copy_bytes: int = Field(default=20 * 1024 * 1024 * 1024, ge=1)

    ffprobe_binary: str = "ffprobe"
    ffprobe_timeout_seconds: float = Field(default=120.0, ge=1, le=3600)
    duration_tolerance_seconds: float = Field(default=1.5, ge=0, le=30)
    fps_tolerance: float = Field(default=1.0, ge=0, le=10)
    delete_invalid_output: bool = True

    aliyun_region: str = "cn-beijing"
    aliyun_access_key_id: str = ""
    aliyun_access_key_secret: str = ""
    aliyun_security_token: str = ""
    aliyun_connect_timeout_seconds: float = 15.0
    aliyun_request_timeout_seconds: float = 60.0
    aliyun_poll_interval_seconds: float = 1.0
    aliyun_job_timeout_seconds: float = 1800.0
    aliyun_default_model_id: str = "algo-video-detext-new"

    oss_endpoint: str = "https://oss-cn-beijing.aliyuncs.com"
    oss_bucket: str = ""
    oss_prefix: str = "cineflow/subtitle-removal"
    oss_signed_url_ttl_seconds: int = Field(default=86_400, ge=300, le=604_800)

    caca_base_url: str = ""
    caca_api_key: str = ""
    caca_submit_path: str = "/v1/video/remove-subtitles"
    caca_status_path: str = "/v1/tasks/{task_id}"
    caca_cancel_path: str = ""
    caca_auth_header: str = "Authorization"
    caca_auth_scheme: str = "Bearer"
    caca_job_timeout_seconds: float = 1800.0

    local_command: str = ""
    local_timeout_seconds: float = 1800.0

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


def create_store(settings: SubtitleWorkerSettings) -> AliyunOSSStore:
    return AliyunOSSStore(
        AliyunOSSConfig(
            endpoint=settings.oss_endpoint,
            bucket=settings.oss_bucket,
            prefix=settings.oss_prefix,
            access_key_id=settings.access_key_id(),
            access_key_secret=settings.access_key_secret(),
            security_token=settings.security_token(),
            signed_url_ttl_seconds=settings.oss_signed_url_ttl_seconds,
        )
    )


def create_providers(
    settings: SubtitleWorkerSettings,
    store: AliyunOSSStore,
) -> dict[str, SubtitleRemovalProvider]:
    providers: dict[str, SubtitleRemovalProvider] = {}
    if (
        settings.aliyun_region.strip()
        and settings.access_key_id()
        and settings.access_key_secret()
        and store.configured
    ):
        ice = AliyunICEClient(
            AliyunICEConfig(
                region=settings.aliyun_region,
                access_key_id=settings.access_key_id(),
                access_key_secret=settings.access_key_secret(),
                security_token=settings.security_token(),
                connect_timeout_seconds=settings.aliyun_connect_timeout_seconds,
                request_timeout_seconds=settings.aliyun_request_timeout_seconds,
                poll_interval_seconds=settings.aliyun_poll_interval_seconds,
                job_timeout_seconds=settings.aliyun_job_timeout_seconds,
            )
        )
        providers["aliyun"] = AliyunVideoDetextProvider(
            ice,
            store,
            AliyunSubtitleConfig(
                default_model_id=settings.aliyun_default_model_id,
            ),
        )
    providers["caca"] = CacaSubtitleProvider(
        CacaSubtitleConfig(
            base_url=settings.caca_base_url,
            api_key=settings.caca_api_key,
            submit_path=settings.caca_submit_path,
            status_path=settings.caca_status_path,
            cancel_path=settings.caca_cancel_path,
            auth_header=settings.caca_auth_header,
            auth_scheme=settings.caca_auth_scheme,
            job_timeout_seconds=settings.caca_job_timeout_seconds,
        ),
        store,
    )
    providers["local"] = LocalSubtitleProvider(
        LocalSubtitleConfig(
            command_template=settings.local_command,
            timeout_seconds=settings.local_timeout_seconds,
            max_download_bytes=settings.max_remote_copy_bytes,
        ),
        store,
    )
    return providers


class SubtitleRuntime:
    def __init__(
        self,
        settings: SubtitleWorkerSettings,
        *,
        state_store: FileStateStore | None = None,
        oss_store: AliyunOSSStore | None = None,
        providers: dict[str, SubtitleRemovalProvider] | None = None,
    ) -> None:
        self.settings = settings
        self.state = state_store or FileStateStore(settings.state_dir)
        self.store = oss_store or create_store(settings)
        self.providers = providers or create_providers(settings, self.store)
        self.semaphore = asyncio.Semaphore(settings.max_concurrency)
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self._tasks_lock = asyncio.Lock()

    def provider_health(self) -> list[SubtitleProviderHealth]:
        rows = []
        for name in ("aliyun", "caca", "local"):
            provider = self.providers.get(name)
            rows.append(
                SubtitleProviderHealth(
                    name=name,
                    configured=bool(provider and provider.configured),
                    detail=provider.detail if provider else "provider is not configured",
                )
            )
        return rows

    def health(self) -> dict[str, object]:
        providers = self.provider_health()
        ready = any(row.configured for row in providers)
        return {
            "ok": ready,
            "healthy": ready,
            "warm": ready,
            "backend": "subtitle-removal-router",
            "providers": [row.model_dump() for row in providers],
            "state_dir": str(Path(self.settings.state_dir).resolve()),
            "max_concurrency": self.settings.max_concurrency,
            "ffprobe_available": bool(shutil.which(self.settings.ffprobe_binary)),
        }

    async def _load(self, job_id: str) -> SubtitleRemovalJob:
        payload = await self.state.get(_NAMESPACE, job_id)
        if payload is None:
            raise KeyError(job_id)
        return SubtitleRemovalJob.model_validate(payload)

    async def _save(self, job: SubtitleRemovalJob) -> None:
        await self.state.put(_NAMESPACE, job.job_id, job.model_dump(mode="json"))

    def _select_provider(self, requested: SubtitleProviderName) -> SubtitleRemovalProvider:
        if requested != SubtitleProviderName.AUTO:
            provider = self.providers.get(requested.value)
            if provider is None or not provider.configured:
                detail = provider.detail if provider else "not installed"
                raise RuntimeError(f"subtitle provider {requested.value} is unavailable: {detail}")
            return provider
        for name in self.settings.provider_order.split(","):
            normalized = name.strip().lower()
            provider = self.providers.get(normalized)
            if provider is not None and provider.configured:
                return provider
        raise RuntimeError("no subtitle-removal provider is configured")

    async def submit(self, request: SubtitleRemovalRequest) -> SubtitleRemovalJob:
        provider = self._select_provider(request.provider)
        job_id = uuid.uuid4().hex
        now = utc_now()
        output_key = self.store.key(
            datetime.now(UTC).strftime("%Y/%m/%d"),
            job_id,
            safe_name(request.output_filename),
        )
        job = SubtitleRemovalJob(
            job_id=job_id,
            state=SubtitleJobState.ACCEPTED,
            request=request,
            selected_provider=provider.name,
            output_object_key=output_key,
            canonical_output_url=self.store.canonical_url(output_key),
            created_at=now,
            updated_at=now,
        )
        await self._save(job)
        await self.schedule(job_id)
        return job

    async def schedule(self, job_id: str) -> None:
        async with self._tasks_lock:
            existing = self.tasks.get(job_id)
            if existing is not None and not existing.done():
                return
            task = asyncio.create_task(self._run(job_id), name=f"subtitle-removal:{job_id}")
            self.tasks[job_id] = task
            task.add_done_callback(lambda _task: self.tasks.pop(job_id, None))

    async def recover(self) -> list[str]:
        recovered = []
        terminal = {
            SubtitleJobState.SUCCEEDED,
            SubtitleJobState.DEGRADED,
            SubtitleJobState.FAILED,
            SubtitleJobState.CANCELED,
        }
        for payload in await self.state.list(_NAMESPACE):
            job = SubtitleRemovalJob.model_validate(payload)
            if job.state not in terminal:
                await self.schedule(job.job_id)
                recovered.append(job.job_id)
        return recovered

    def _submission_from_job(self, job: SubtitleRemovalJob) -> ProviderSubmission:
        stored = job.metadata.get("provider_submission")
        if isinstance(stored, dict):
            return ProviderSubmission.model_validate(stored)
        return ProviderSubmission(
            provider=job.selected_provider,
            external_job_id=job.external_job_id,
            output_object_key=job.output_object_key,
            output_url=job.canonical_output_url,
            completed=False,
        )

    async def _persist_submission(
        self,
        job: SubtitleRemovalJob,
        submission: ProviderSubmission,
    ) -> SubtitleRemovalJob:
        metadata = dict(job.metadata)
        metadata["provider_submission"] = submission.model_dump(mode="json")
        updated = job.model_copy(
            update={
                "state": SubtitleJobState.RUNNING,
                "external_job_id": submission.external_job_id,
                "canonical_output_url": (submission.output_url or job.canonical_output_url),
                "updated_at": utc_now(),
                "metadata": metadata,
            }
        )
        await self._save(updated)
        return updated

    async def _copy_external_output(
        self,
        source_url: str,
        output_object_key: str,
    ) -> str:
        owned_key = self.store.object_key_from_url(source_url)
        if owned_key:
            return owned_key
        if not source_url.startswith(("http://", "https://")):
            raise RuntimeError(f"unsupported subtitle-removal output URL: {source_url}")
        with tempfile.TemporaryDirectory(prefix="cineflow-detext-copy-") as directory:
            target = Path(directory) / "output.mp4"
            total = 0
            async with (
                httpx.AsyncClient(
                    timeout=600.0,
                    follow_redirects=True,
                    trust_env=False,
                ) as client,
                client.stream("GET", source_url) as response,
            ):
                response.raise_for_status()
                with target.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self.settings.max_remote_copy_bytes:
                            raise RuntimeError(
                                "subtitle-removal output exceeds the configured copy limit"
                            )
                        output.write(chunk)
            if total <= 0:
                raise RuntimeError("subtitle-removal provider returned an empty output")
            await asyncio.to_thread(
                self.store.put_file,
                output_object_key,
                target,
                content_type="video/mp4",
            )
        return output_object_key

    async def _validate_output(
        self,
        job: SubtitleRemovalJob,
        object_key: str,
    ) -> SubtitleOutputValidation:
        head = await asyncio.to_thread(self.store.head_object, object_key)
        if not job.request.validate_output:
            return SubtitleOutputValidation(
                valid=int(head.get("content_length", 0) or 0) > 0,
                size_bytes=int(head.get("content_length", 0) or 0),
            )
        signed = self.store.signed_url(object_key)
        probe = await probe_media(
            signed,
            ffprobe_binary=self.settings.ffprobe_binary,
            timeout_seconds=self.settings.ffprobe_timeout_seconds,
        )
        validation = validate_probe(
            job.request,
            probe,
            duration_tolerance_seconds=self.settings.duration_tolerance_seconds,
            fps_tolerance=self.settings.fps_tolerance,
        )
        if validation.size_bytes <= 0:
            validation = validation.model_copy(
                update={"size_bytes": int(head.get("content_length", 0) or 0)}
            )
        return validation

    async def _run(self, job_id: str) -> None:
        async with self.semaphore:
            try:
                job = await self._load(job_id)
                if job.state in {
                    SubtitleJobState.SUCCEEDED,
                    SubtitleJobState.DEGRADED,
                    SubtitleJobState.CANCELED,
                }:
                    return
                provider = self.providers.get(job.selected_provider)
                if provider is None or not provider.configured:
                    raise RuntimeError(
                        f"subtitle provider {job.selected_provider} is unavailable during resume"
                    )
                job = job.model_copy(
                    update={
                        "state": SubtitleJobState.SUBMITTING,
                        "attempts": job.attempts + 1,
                        "updated_at": utc_now(),
                        "error": "",
                    }
                )
                await self._save(job)

                submission = self._submission_from_job(job)
                output_exists = bool(
                    submission.output_object_key
                    and await asyncio.to_thread(
                        self.store.object_exists,
                        submission.output_object_key,
                    )
                )
                if provider.name == "local":
                    has_existing_submission = output_exists and submission.completed
                else:
                    has_existing_submission = bool(
                        submission.external_job_id or submission.completed or output_exists
                    )
                if not has_existing_submission:
                    submission = await provider.submit(
                        job.request,
                        job_id=job.job_id,
                        output_object_key=job.output_object_key,
                    )
                    job = await self._persist_submission(job, submission)
                if not submission.completed:
                    submission = await provider.wait(submission)
                    job = await self._persist_submission(job, submission)

                object_key = await self._copy_external_output(
                    submission.output_url or job.canonical_output_url,
                    job.output_object_key,
                )
                job = job.model_copy(
                    update={
                        "state": SubtitleJobState.VALIDATING,
                        "output_object_key": object_key,
                        "canonical_output_url": self.store.canonical_url(object_key),
                        "updated_at": utc_now(),
                    }
                )
                await self._save(job)
                validation = await self._validate_output(job, object_key)
                if not validation.valid:
                    if self.settings.delete_invalid_output:
                        await asyncio.to_thread(self.store.delete_object, object_key)
                    raise RuntimeError(
                        "; ".join(validation.warnings) or "generated video validation failed"
                    )

                warnings = [*job.warnings, *validation.warnings]
                completed_at = utc_now()
                final_state = (
                    SubtitleJobState.DEGRADED if validation.warnings else SubtitleJobState.SUCCEEDED
                )
                completed = job.model_copy(
                    update={
                        "state": final_state,
                        "output_url": self.store.signed_url(object_key),
                        "canonical_output_url": self.store.canonical_url(object_key),
                        "validation": validation,
                        "warnings": warnings,
                        "updated_at": completed_at,
                        "completed_at": completed_at,
                        "error": "",
                    }
                )
                await self._save(completed)
            except Exception as exc:
                try:
                    job = await self._load(job_id)
                    failed = job.model_copy(
                        update={
                            "state": SubtitleJobState.FAILED,
                            "updated_at": utc_now(),
                            "completed_at": utc_now(),
                            "error": str(exc),
                        }
                    )
                    await self._save(failed)
                except Exception:
                    pass

    async def cancel(
        self,
        job_id: str,
        *,
        delete_output: bool,
        delete_input: bool,
    ) -> SubtitleCleanupResult:
        job = await self._load(job_id)
        task = self.tasks.get(job_id)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        provider = self.providers.get(job.selected_provider)
        warnings: list[str] = []
        submission = self._submission_from_job(job)
        if provider is not None:
            try:
                await provider.cancel(submission)
            except Exception as exc:
                warnings.append(str(exc))
        deleted_output = False
        if delete_output and job.output_object_key:
            try:
                exists = await asyncio.to_thread(
                    self.store.object_exists,
                    job.output_object_key,
                )
                if exists:
                    await asyncio.to_thread(self.store.delete_object, job.output_object_key)
                    deleted_output = True
            except Exception as exc:
                warnings.append(str(exc))
        deleted_input = False
        if delete_input or job.request.cleanup_input:
            input_key = self.store.object_key_from_url(str(job.request.input_url))
            if input_key:
                try:
                    exists = await asyncio.to_thread(self.store.object_exists, input_key)
                    if exists:
                        await asyncio.to_thread(self.store.delete_object, input_key)
                        deleted_input = True
                except Exception as exc:
                    warnings.append(str(exc))
            else:
                warnings.append("input is not owned by the configured OSS bucket; not deleted")
        canceled = job.model_copy(
            update={
                "state": SubtitleJobState.CANCELED,
                "updated_at": utc_now(),
                "completed_at": utc_now(),
                "warnings": [*job.warnings, *warnings],
            }
        )
        await self._save(canceled)
        return SubtitleCleanupResult(
            job_id=job_id,
            state=canceled.state,
            deleted_output=deleted_output,
            deleted_input=deleted_input,
            warnings=warnings,
        )

    async def cleanup(
        self,
        *,
        older_than_seconds: int,
        delete_successful_outputs: bool,
    ) -> dict[str, object]:
        threshold = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
        cleaned: list[str] = []
        failed: dict[str, str] = {}
        for payload in await self.state.list(_NAMESPACE):
            job = SubtitleRemovalJob.model_validate(payload)
            try:
                timestamp = parse_utc(job.updated_at)
            except ValueError:
                timestamp = datetime.min.replace(tzinfo=UTC)
            if timestamp > threshold:
                continue
            if (
                job.state
                in {
                    SubtitleJobState.SUCCEEDED,
                    SubtitleJobState.DEGRADED,
                }
                and not delete_successful_outputs
            ):
                continue
            try:
                await self.cancel(
                    job.job_id,
                    delete_output=True,
                    delete_input=False,
                )
                cleaned.append(job.job_id)
            except Exception as exc:
                failed[job.job_id] = str(exc)
        return {"cleaned": cleaned, "failed": failed}


def create_app(
    settings: SubtitleWorkerSettings | None = None,
    *,
    runtime: SubtitleRuntime | None = None,
) -> FastAPI:
    resolved = settings or SubtitleWorkerSettings()
    service = runtime or SubtitleRuntime(resolved)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await service.recover()
        yield

    app = FastAPI(
        title="CineFlow Subtitle Removal Worker",
        version="0.5.0",
        lifespan=lifespan,
        description=(
            "Durable local, Caca, and Alibaba VideoDetext jobs with recovery, "
            "media validation, and OSS cleanup."
        ),
    )

    def authorize(authorization: str | None) -> None:
        token = resolved.bearer_token.strip()
        if token and authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="invalid subtitle worker bearer token")

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return service.health()

    @app.post(
        "/v1/subtitles/jobs",
        response_model=SubtitleRemovalJob,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_job(
        request: SubtitleRemovalRequest,
        authorization: str | None = Header(default=None),
    ) -> SubtitleRemovalJob:
        authorize(authorization)
        try:
            return await service.submit(request)
        except (RuntimeError, AliyunOSSError, StateStoreError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/v1/subtitles/recover")
    async def recover(
        authorization: str | None = Header(default=None),
    ) -> dict[str, list[str]]:
        authorize(authorization)
        return {"recovered": await service.recover()}

    @app.post("/v1/subtitles/cleanup")
    async def cleanup(
        older_than_seconds: int = Query(default=resolved.stale_job_seconds, ge=900),
        delete_successful_outputs: bool = False,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        return await service.cleanup(
            older_than_seconds=older_than_seconds,
            delete_successful_outputs=delete_successful_outputs,
        )

    @app.get("/v1/subtitles/jobs/{job_id}", response_model=SubtitleRemovalJob)
    async def get_job(
        job_id: str,
        authorization: str | None = Header(default=None),
    ) -> SubtitleRemovalJob:
        authorize(authorization)
        try:
            return await service._load(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="subtitle job not found") from exc

    @app.post("/v1/subtitles/jobs/{job_id}/resume", response_model=SubtitleRemovalJob)
    async def resume_job(
        job_id: str,
        authorization: str | None = Header(default=None),
    ) -> SubtitleRemovalJob:
        authorize(authorization)
        try:
            job = await service._load(job_id)
            if job.state in {SubtitleJobState.SUCCEEDED, SubtitleJobState.DEGRADED}:
                return job
            await service.schedule(job_id)
            return job
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="subtitle job not found") from exc

    @app.delete(
        "/v1/subtitles/jobs/{job_id}",
        response_model=SubtitleCleanupResult,
    )
    async def cancel_job(
        job_id: str,
        delete_output: bool = True,
        delete_input: bool = False,
        authorization: str | None = Header(default=None),
    ) -> SubtitleCleanupResult:
        authorize(authorization)
        try:
            return await service.cancel(
                job_id,
                delete_output=delete_output,
                delete_input=delete_input,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="subtitle job not found") from exc

    app.state.runtime = service
    return app


settings = SubtitleWorkerSettings()
app = create_app(settings)


def run() -> None:
    uvicorn.run(
        "cineflow.subtitle_worker:app",
        host=os.getenv("CINEFLOW_SUBTITLE_HOST", settings.host),
        port=int(os.getenv("CINEFLOW_SUBTITLE_PORT", str(settings.port))),
    )
