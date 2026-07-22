from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from ..models import JobRequest, Transcript
from .asr_common import (
    ASRProviderError,
    normalize_language,
    select_input_url,
    transcript_from_rows,
)


@dataclass(frozen=True)
class AliyunFunASRConfig:
    api_key: str
    workspace_id: str = ""
    region: str = "cn-beijing"
    base_url: str = ""
    model: str = "fun-asr"
    poll_interval_seconds: float = 0.75
    max_poll_interval_seconds: float = 3.0
    task_timeout_seconds: float = 240.0
    request_timeout_seconds: float = 30.0

    @property
    def endpoint(self) -> str:
        if self.base_url.strip():
            return self.base_url.rstrip("/")
        if self.workspace_id.strip():
            return f"https://{self.workspace_id.strip()}.{self.region.strip()}.maas.aliyuncs.com"
        if self.region.strip() == "ap-southeast-1":
            return "https://dashscope-intl.aliyuncs.com"
        return "https://dashscope.aliyuncs.com"


class AliyunFunASRClient:
    """Asynchronous Alibaba Cloud Fun-ASR submit/poll/result client."""

    provider_name = "aliyun_fun_asr"

    def __init__(
        self,
        config: AliyunFunASRConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.config.api_key.strip())

    @property
    def ready(self) -> bool:
        return self.configured

    def health_detail(self) -> str:
        if not self.configured:
            return "CINEFLOW_ASR_ALIYUN_API_KEY is missing"
        return f"model={self.config.model}, endpoint={self.config.endpoint}"

    async def transcribe(self, request: JobRequest) -> Transcript:
        if not self.configured:
            raise ASRProviderError(self.health_detail())

        source_url = select_input_url(request)
        language = normalize_language(request.source_language)
        headers = {
            "Authorization": f"Bearer {self.config.api_key.strip()}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }
        parameters: dict[str, object] = {
            "channel_id": [0],
            "diarization_enabled": bool(request.multi_speaker),
            "language_hints": [language],
        }
        if request.multi_speaker and request.expected_speakers and request.expected_speakers >= 2:
            parameters["speaker_count"] = request.expected_speakers

        payload = {
            "model": self.config.model,
            "input": {"file_urls": [source_url]},
            "parameters": parameters,
        }
        timeout = httpx.Timeout(self.config.request_timeout_seconds)
        async with httpx.AsyncClient(
            timeout=timeout,
            transport=self.transport,
            follow_redirects=True,
        ) as client:
            submit = await client.post(
                f"{self.config.endpoint}/api/v1/services/audio/asr/transcription",
                headers=headers,
                json=payload,
            )
            self._raise_http_error(submit, "submit")
            submit_data = submit.json()
            task_id = str(submit_data.get("output", {}).get("task_id", "")).strip()
            if not task_id:
                raise ASRProviderError("Alibaba Fun-ASR submit response is missing task_id")

            task_data = await self._wait_for_task(client, task_id, headers)
            output = task_data.get("output", {})
            results = output.get("results") or []
            if not results:
                raise ASRProviderError("Alibaba Fun-ASR task returned no result entries")
            result = results[0]
            if str(result.get("subtask_status", "")).upper() != "SUCCEEDED":
                code = result.get("code", "")
                message = result.get("message", "subtask failed")
                raise ASRProviderError(f"Alibaba Fun-ASR subtask failed: {code} {message}")

            transcription_url = str(result.get("transcription_url", "")).strip()
            if not transcription_url:
                raise ASRProviderError("Alibaba Fun-ASR task succeeded without transcription_url")
            transcription = await client.get(transcription_url)
            self._raise_http_error(transcription, "download result")
            usage_seconds = self._optional_float(task_data.get("usage", {}).get("duration"))
            return self.parse_result(
                transcription.json(),
                language=request.source_language,
                task_id=task_id,
                usage_seconds=usage_seconds,
                request_id=str(task_data.get("request_id", "") or ""),
            )

    async def _wait_for_task(
        self,
        client: httpx.AsyncClient,
        task_id: str,
        headers: dict[str, str],
    ) -> dict:
        started = time.monotonic()
        delay = max(0.05, self.config.poll_interval_seconds)
        query_headers = {"Authorization": headers["Authorization"]}
        while True:
            if time.monotonic() - started >= self.config.task_timeout_seconds:
                raise ASRProviderError(
                    f"Alibaba Fun-ASR task {task_id} exceeded "
                    f"{self.config.task_timeout_seconds:.0f}s polling timeout"
                )
            response = await client.get(
                f"{self.config.endpoint}/api/v1/tasks/{task_id}",
                headers=query_headers,
            )
            self._raise_http_error(response, "query")
            data = response.json()
            status = str(data.get("output", {}).get("task_status", "")).upper()
            if status == "SUCCEEDED":
                return data
            if status in {"FAILED", "CANCELED", "CANCELLED", "UNKNOWN"}:
                output = data.get("output", {})
                detail = output.get("message") or output.get("code") or status
                raise ASRProviderError(f"Alibaba Fun-ASR task {task_id} failed: {detail}")
            if status not in {"PENDING", "RUNNING", ""}:
                raise ASRProviderError(
                    f"Alibaba Fun-ASR task {task_id} returned unexpected status {status}"
                )
            await asyncio.sleep(delay)
            delay = min(self.config.max_poll_interval_seconds, delay * 1.35)

    @classmethod
    def parse_result(
        cls,
        payload: dict,
        *,
        language: str,
        task_id: str,
        usage_seconds: float | None,
        request_id: str = "",
    ) -> Transcript:
        rows: list[dict[str, object]] = []
        root = payload.get("output", payload)
        for transcript in root.get("transcripts", []) or []:
            for sentence in transcript.get("sentences", []) or []:
                words = [
                    {
                        "start_ms": word.get("begin_time", 0),
                        "end_ms": word.get("end_time", 0),
                        "text": word.get("text", ""),
                        "punctuation": word.get("punctuation", ""),
                    }
                    for word in sentence.get("words", []) or []
                ]
                rows.append(
                    {
                        "start_ms": sentence.get("begin_time", 0),
                        "end_ms": sentence.get("end_time", 0),
                        "text": sentence.get("text", ""),
                        "speaker_id": sentence.get("speaker_id"),
                        "words": words,
                    }
                )
        return transcript_from_rows(
            language=language,
            provider=cls.provider_name,
            rows=rows,
            task_id=task_id,
            usage_seconds=usage_seconds,
            metadata={
                "request_id": request_id,
                "raw_file_url": payload.get("file_url", ""),
            },
        )

    @staticmethod
    def _raise_http_error(response: httpx.Response, action: str) -> None:
        if response.is_success:
            return
        detail = response.text[:1000]
        raise ASRProviderError(
            f"Alibaba Fun-ASR {action} failed with HTTP {response.status_code}: {detail}"
        )

    @staticmethod
    def _optional_float(value: object) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
