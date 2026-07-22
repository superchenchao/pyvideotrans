from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from ..subtitle_models import ProviderSubmission, SubtitleRemovalRequest
from .aliyun_oss import AliyunOSSStore


class CacaSubtitleError(RuntimeError):
    """Normalized Caca subtitle-removal API failure."""


@dataclass(frozen=True)
class CacaSubtitleConfig:
    base_url: str
    api_key: str
    submit_path: str = "/v1/video/remove-subtitles"
    status_path: str = "/v1/tasks/{task_id}"
    cancel_path: str = ""
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    submit_timeout_seconds: float = 60.0
    status_timeout_seconds: float = 20.0
    poll_interval_seconds: float = 2.0
    max_poll_interval_seconds: float = 8.0
    job_timeout_seconds: float = 1800.0


class CacaSubtitleProvider:
    """Configurable adapter for the existing Caca removal service.

    Caca installations differ in endpoint naming. Paths are environment-driven,
    while response parsing accepts common task/status/output field variants. The
    generated result is normalized into CineFlow's configured OSS bucket.
    """

    name = "caca"

    def __init__(
        self,
        config: CacaSubtitleConfig,
        store: AliyunOSSStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(
            self.config.base_url.strip()
            and self.config.api_key.strip()
            and self.store.configured
        )

    @property
    def detail(self) -> str:
        if self.configured:
            return "Caca API and result OSS storage configured"
        missing = []
        if not self.config.base_url.strip():
            missing.append("base URL")
        if not self.config.api_key.strip():
            missing.append("API key")
        if not self.store.configured:
            missing.append("OSS result store")
        return "Caca missing " + ", ".join(missing)

    @property
    def headers(self) -> dict[str, str]:
        value = self.config.api_key.strip()
        if self.config.auth_scheme.strip():
            value = f"{self.config.auth_scheme.strip()} {value}"
        return {
            self.config.auth_header.strip() or "Authorization": value,
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.config.base_url.rstrip('/')}/{path.lstrip('/')}"

    @staticmethod
    def _usable(value: object | None) -> bool:
        return value is not None and value != "" and value != [] and value != {}

    @staticmethod
    def _find(payload: object, keys: tuple[str, ...]) -> object | None:
        if isinstance(payload, dict):
            lowered = {str(key).lower(): value for key, value in payload.items()}
            for key in keys:
                if key.lower() in lowered:
                    return lowered[key.lower()]
            for value in payload.values():
                found = CacaSubtitleProvider._find(value, keys)
                if CacaSubtitleProvider._usable(found):
                    return found
        elif isinstance(payload, list):
            for value in payload:
                found = CacaSubtitleProvider._find(value, keys)
                if CacaSubtitleProvider._usable(found):
                    return found
        return None

    @classmethod
    def _task_id(cls, payload: object) -> str:
        value = cls._find(
            payload,
            ("task_id", "taskid", "job_id", "jobid", "id"),
        )
        return str(value or "").strip()

    @classmethod
    def _status(cls, payload: object) -> str:
        value = cls._find(payload, ("status", "state", "task_status", "job_status"))
        return str(value or "").strip().lower()

    @classmethod
    def _output_url(cls, payload: object) -> str:
        value = cls._find(
            payload,
            (
                "output_url",
                "outputurl",
                "result_url",
                "resulturl",
                "video_url",
                "videourl",
                "download_url",
                "downloadurl",
                "url",
            ),
        )
        text = str(value or "").strip()
        return text if text.startswith(("http://", "https://", "oss://")) else ""

    async def submit(
        self,
        request: SubtitleRemovalRequest,
        *,
        job_id: str,
        output_object_key: str,
    ) -> ProviderSubmission:
        if not self.configured:
            raise CacaSubtitleError(self.detail)
        payload = {
            "video_url": str(request.input_url),
            "input_url": str(request.input_url),
            "output_url": self.store.canonical_url(output_object_key),
            "output_oss_uri": self.store.oss_uri(output_object_key),
            "regions": [region.model_dump() for region in request.regions],
            "time_ranges": [item.model_dump() for item in request.time_ranges],
            "model_id": request.model_id,
            "client_job_id": job_id,
            "metadata": request.metadata,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.config.submit_timeout_seconds,
                transport=self.transport,
                trust_env=False,
            ) as client:
                response = await client.post(
                    self._url(self.config.submit_path),
                    headers=self.headers,
                    json=payload,
                )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            raise CacaSubtitleError(f"Caca submit failed: {exc}") from exc

        task_id = self._task_id(body)
        output_url = self._output_url(body)
        status_value = self._status(body)
        completed = bool(output_url) and status_value in {
            "",
            "success",
            "succeeded",
            "completed",
            "done",
            "finish",
            "finished",
        }
        if not task_id and not completed:
            raise CacaSubtitleError("Caca submit response has neither task ID nor output URL")
        return ProviderSubmission(
            provider=self.name,
            external_job_id=task_id,
            output_object_key=output_object_key,
            output_url=output_url,
            completed=completed,
            metadata={"submit_response": body},
        )

    async def wait(self, submission: ProviderSubmission) -> ProviderSubmission:
        if submission.completed:
            return submission
        if not submission.external_job_id:
            raise CacaSubtitleError("Caca submission is missing a task ID")
        started = time.monotonic()
        delay = max(0.2, self.config.poll_interval_seconds)
        while True:
            if time.monotonic() - started >= self.config.job_timeout_seconds:
                raise CacaSubtitleError(
                    f"Caca task {submission.external_job_id} exceeded "
                    f"{self.config.job_timeout_seconds:.0f}s"
                )
            path = self.config.status_path.format(task_id=submission.external_job_id)
            try:
                async with httpx.AsyncClient(
                    timeout=self.config.status_timeout_seconds,
                    transport=self.transport,
                    trust_env=False,
                ) as client:
                    response = await client.get(self._url(path), headers=self.headers)
                response.raise_for_status()
                body = response.json()
            except Exception as exc:
                raise CacaSubtitleError(
                    f"Caca status query failed for {submission.external_job_id}: {exc}"
                ) from exc

            status_value = self._status(body)
            output_url = self._output_url(body)
            if status_value in {
                "success",
                "succeeded",
                "completed",
                "done",
                "finish",
                "finished",
            }:
                if not output_url:
                    raise CacaSubtitleError("Caca task succeeded without an output URL")
                metadata = dict(submission.metadata)
                metadata["status_response"] = body
                return submission.model_copy(
                    update={
                        "output_url": output_url,
                        "completed": True,
                        "metadata": metadata,
                    }
                )
            if status_value in {
                "failed",
                "fail",
                "error",
                "canceled",
                "cancelled",
                "rejected",
            }:
                message = self._find(body, ("message", "error", "detail", "reason"))
                raise CacaSubtitleError(
                    f"Caca task {submission.external_job_id} failed: {message or status_value}"
                )
            await asyncio.sleep(delay)
            delay = min(self.config.max_poll_interval_seconds, delay * 1.35)

    async def cancel(self, submission: ProviderSubmission) -> None:
        if not self.config.cancel_path.strip() or not submission.external_job_id:
            return
        path = self.config.cancel_path.format(task_id=submission.external_job_id)
        try:
            async with httpx.AsyncClient(
                timeout=self.config.status_timeout_seconds,
                transport=self.transport,
                trust_env=False,
            ) as client:
                response = await client.post(self._url(path), headers=self.headers)
            response.raise_for_status()
        except Exception as exc:
            raise CacaSubtitleError(
                f"Caca cancel failed for {submission.external_job_id}: {exc}"
            ) from exc
