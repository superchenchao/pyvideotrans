from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..models import JobRequest, Transcript
from .asr_common import ASRProviderError, select_input_url, transcript_from_rows


@dataclass(frozen=True)
class VolcengineASRConfig:
    api_key: str = ""
    app_id: str = ""
    access_token: str = ""
    endpoint: str = (
        "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
    )
    resource_id: str = "volc.bigasr.auc_turbo"
    model_version: str = "400"
    request_timeout_seconds: float = 150.0
    download_timeout_seconds: float = 90.0
    attempts: int = 3
    ffmpeg_binary: str = "ffmpeg"
    max_download_bytes: int = 768 * 1024 * 1024


class VolcengineFlashASRClient:
    """Volcengine BigModel Flash client normalized to CineFlow Transcript."""

    provider_name = "volcengine_bigmodel_flash"

    def __init__(
        self,
        config: VolcengineASRConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(
            self.config.api_key.strip()
            or (self.config.app_id.strip() and self.config.access_token.strip())
        )

    @property
    def ready(self) -> bool:
        return self.configured and bool(shutil.which(self.config.ffmpeg_binary))

    def health_detail(self) -> str:
        if not self.configured:
            return (
                "configure CINEFLOW_ASR_VOLCENGINE_API_KEY or both "
                "CINEFLOW_ASR_VOLCENGINE_APP_ID and "
                "CINEFLOW_ASR_VOLCENGINE_ACCESS_TOKEN"
            )
        if not shutil.which(self.config.ffmpeg_binary):
            return f"ffmpeg binary not found: {self.config.ffmpeg_binary}"
        return (
            f"resource_id={self.config.resource_id}, "
            f"model_version={self.config.model_version}"
        )

    async def transcribe(self, request: JobRequest) -> Transcript:
        if not self.ready:
            raise ASRProviderError(self.health_detail())

        source_url = select_input_url(request)
        with tempfile.TemporaryDirectory(prefix="cineflow-volc-asr-") as directory:
            work = Path(directory)
            source = work / self._source_filename(source_url)
            await self._download(source_url, source)
            audio = work / "speech-16k-64k.mp3"
            await asyncio.to_thread(self._transcode, source, audio)
            audio_bytes = audio.read_bytes()

        return await self.transcribe_audio_bytes(
            request,
            audio_bytes,
            audio_format="mp3",
        )

    async def transcribe_audio_bytes(
        self,
        request: JobRequest,
        audio_bytes: bytes,
        *,
        audio_format: str,
    ) -> Transcript:
        if not audio_bytes:
            raise ASRProviderError("Volcengine ASR audio payload is empty")

        payload = {
            "user": {"uid": self.config.app_id.strip() or "cineflow-cloud"},
            "audio": {
                "data": base64.b64encode(audio_bytes).decode("ascii"),
                "format": audio_format.lstrip(".").lower(),
            },
            "request": {
                "model_name": "bigmodel",
                "model_version": self.config.model_version,
                "enable_itn": True,
                "enable_punc": True,
                "enable_ddc": True,
                "show_utterances": True,
                "enable_speaker_info": bool(request.multi_speaker),
            },
        }
        last_error: Exception | None = None
        attempt_count = max(1, self.config.attempts)
        timeout = httpx.Timeout(self.config.request_timeout_seconds)
        async with httpx.AsyncClient(
            timeout=timeout,
            transport=self.transport,
            trust_env=False,
        ) as client:
            for attempt in range(attempt_count):
                request_id = str(uuid.uuid4())
                try:
                    response = await client.post(
                        self.config.endpoint,
                        headers=self._headers(request_id),
                        json=payload,
                    )
                    response.raise_for_status()
                    code = str(response.headers.get("X-Api-Status-Code", ""))
                    if code != "20000000":
                        message = response.headers.get("X-Api-Message", "")
                        error = ASRProviderError(
                            f"Volcengine ASR returned code "
                            f"{code or 'unknown'}: {message}"
                        )
                        if not code.startswith("55"):
                            raise error
                        last_error = error
                    else:
                        trace_id = str(
                            response.headers.get("X-Tt-Logid", "") or ""
                        )
                        return self.parse_result(
                            response.json(),
                            language=request.source_language,
                            task_id=request_id,
                            trace_id=trace_id,
                        )
                except (
                    httpx.TimeoutException,
                    httpx.NetworkError,
                    httpx.HTTPStatusError,
                ) as exc:
                    last_error = exc
                    if (
                        isinstance(exc, httpx.HTTPStatusError)
                        and exc.response.status_code < 500
                    ):
                        break
                except ASRProviderError:
                    raise

                if attempt + 1 < attempt_count:
                    await asyncio.sleep(1.5 * (attempt + 1))

        raise ASRProviderError(
            f"Volcengine ASR failed after retries: {last_error}"
        ) from last_error

    async def _download(self, url: str, destination: Path) -> None:
        total = 0
        timeout = httpx.Timeout(self.config.download_timeout_seconds)
        async with httpx.AsyncClient(
            timeout=timeout,
            transport=self.transport,
            follow_redirects=True,
            trust_env=False,
        ) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                with destination.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self.config.max_download_bytes:
                            raise ASRProviderError(
                                "Volcengine ASR source exceeds configured download limit"
                            )
                        output.write(chunk)
        if total <= 0:
            raise ASRProviderError(
                "Volcengine ASR downloaded an empty source file"
            )

    def _transcode(self, source: Path, destination: Path) -> None:
        command = [
            self.config.ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "64k",
            "-map_metadata",
            "-1",
            str(destination),
        ]
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=max(30.0, self.config.request_timeout_seconds),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            detail = getattr(exc, "stderr", "") or str(exc)
            raise ASRProviderError(
                f"Volcengine ASR ffmpeg preparation failed: {detail}"
            ) from exc
        if not destination.is_file() or destination.stat().st_size <= 0:
            raise ASRProviderError("Volcengine ASR ffmpeg output is empty")

    def _headers(self, request_id: str) -> dict[str, str]:
        headers = {
            "X-Api-Resource-Id": self.config.resource_id,
            "X-Api-Request-Id": request_id,
            "X-Api-Sequence": "-1",
        }
        if self.config.api_key.strip():
            headers["X-Api-Key"] = self.config.api_key.strip()
        else:
            headers["X-Api-App-Key"] = self.config.app_id.strip()
            headers["X-Api-Access-Key"] = self.config.access_token.strip()
        return headers

    @classmethod
    def parse_result(
        cls,
        payload: dict,
        *,
        language: str,
        task_id: str,
        trace_id: str,
    ) -> Transcript:
        utterances = payload.get("result", {}).get("utterances") or []
        rows: list[dict[str, object]] = []
        max_end_ms = 0
        for item in utterances:
            additions = item.get("additions") or {}
            if isinstance(additions, str):
                try:
                    additions = json.loads(additions)
                except json.JSONDecodeError:
                    additions = {}

            words = [
                {
                    "start_ms": word.get("start_time", word.get("start", 0)),
                    "end_ms": word.get("end_time", word.get("end", 0)),
                    "text": word.get("text", word.get("word", "")),
                    "punctuation": word.get("punctuation", ""),
                }
                for word in item.get("words", []) or []
            ]
            try:
                max_end_ms = max(max_end_ms, int(item.get("end_time", 0)))
            except (TypeError, ValueError):
                pass
            rows.append(
                {
                    "start_ms": item.get("start_time", 0),
                    "end_ms": item.get("end_time", 0),
                    "text": item.get("text", ""),
                    "speaker_id": additions.get("speaker"),
                    "words": words,
                }
            )

        return transcript_from_rows(
            language=language,
            provider=cls.provider_name,
            rows=rows,
            task_id=task_id,
            usage_seconds=max_end_ms / 1000 if max_end_ms else None,
            metadata={"trace_id": trace_id},
        )

    @staticmethod
    def _source_filename(url: str) -> str:
        suffix = Path(urlparse(url).path).suffix.lower()
        return f"input{suffix if suffix else '.bin'}"
