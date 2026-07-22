from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from ..models import JobRequest, Transcript
from ..subtitle_recognition import parse_srt
from .aliyun_ice import AliyunICEClient, AliyunICEError
from .aliyun_oss import AliyunOSSStore


class AliyunCaptionError(RuntimeError):
    """Normalized Alibaba CaptionExtraction failure."""


@dataclass(frozen=True)
class AliyunCaptionConfig:
    download_timeout_seconds: float = 60.0
    model_id: str = ""


def _walk_urls(value: object) -> list[str]:
    result: list[str] = []
    if isinstance(value, dict):
        for child in value.values():
            result.extend(_walk_urls(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_walk_urls(child))
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith(("http://", "https://", "oss://")):
            result.append(text)
        elif text.startswith(("[", "{")):
            try:
                result.extend(_walk_urls(json.loads(text)))
            except json.JSONDecodeError:
                pass
    return result


def caption_output_urls(result: dict[str, Any], store: AliyunOSSStore) -> list[str]:
    candidates = _walk_urls(result.get("OutputUrls", []))
    candidates.extend(_walk_urls(result.get("Result", "")))
    output_files = result.get("OutputFiles", [])
    if isinstance(output_files, str):
        try:
            output_files = json.loads(output_files)
        except json.JSONDecodeError:
            output_files = [output_files]
    if isinstance(output_files, list):
        for item in output_files:
            text = str(item or "").strip()
            if not text:
                continue
            candidates.append(
                text
                if text.startswith(("http://", "https://", "oss://"))
                else store.canonical_url(text)
            )
    unique: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


class AliyunCaptionExtractor:
    """Extract visible subtitles with Alibaba ICE CaptionExtraction.

    Recognition happens in Alibaba Cloud. This adapter does not extract frames and
    does not run a local OCR engine.
    """

    name = "aliyun_caption_extraction"

    def __init__(
        self,
        ice: AliyunICEClient,
        store: AliyunOSSStore,
        config: AliyunCaptionConfig | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.ice = ice
        self.store = store
        self.config = config or AliyunCaptionConfig()
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.ice.configured and self.store.configured)

    @staticmethod
    def job_params(request: JobRequest) -> dict[str, object]:
        params: dict[str, object] = {
            "fps": request.ocr_fps,
            "lang": request.ocr_language,
            "sep": False,
            "track": request.ocr_track,
        }
        if request.ocr_region is not None:
            region = request.ocr_region
            params["roi"] = [
                [round(region.y, 6), round(region.y + region.height, 6)],
                [round(region.x, 6), round(region.x + region.width, 6)],
            ]
        return params

    async def _download_text(self, value: str) -> str:
        object_key = self.store.object_key_from_url(value)
        url = self.store.signed_url(object_key) if object_key else value
        if url.startswith("oss://"):
            raise AliyunCaptionError("CaptionExtraction returned an unresolvable OSS URI")
        try:
            async with httpx.AsyncClient(
                timeout=self.config.download_timeout_seconds,
                follow_redirects=True,
                transport=self.transport,
                trust_env=False,
            ) as client:
                response = await client.get(url)
            response.raise_for_status()
        except Exception as exc:
            raise AliyunCaptionError(f"failed to download OCR subtitle output: {exc}") from exc
        response.encoding = response.encoding or "utf-8"
        return response.text

    async def extract(self, request: JobRequest) -> Transcript:
        if not self.configured:
            raise AliyunCaptionError("Alibaba CaptionExtraction credentials or OSS are missing")
        operation_id = uuid.uuid4().hex
        output_key = self.store.key("captions", operation_id, "source.srt")
        input_media = self.store.canonicalize_oss_input(str(request.input_url))
        try:
            job_id = await self.ice.submit_i_production(
                name=f"cineflow-caption-{operation_id[:12]}",
                function_name="CaptionExtraction",
                input_media=input_media,
                output_media=self.store.oss_uri(output_key),
                client_token=f"caption-{operation_id}",
                job_params=self.job_params(request),
                model_id=self.config.model_id,
            )
            result = await self.ice.wait_i_production(job_id)
        except AliyunICEError as exc:
            raise AliyunCaptionError(str(exc)) from exc

        urls = caption_output_urls(result, self.store)
        preferred = next((url for url in urls if url.lower().split("?", 1)[0].endswith(".srt")), None)
        output_url = preferred or (urls[0] if urls else self.store.canonical_url(output_key))
        content = await self._download_text(output_url)
        transcript = parse_srt(
            content,
            language=request.source_language,
            provider=self.name,
            task_id=job_id,
        )
        metadata = dict(transcript.metadata)
        metadata.update(
            {
                "ocr_engine": "api",
                "ocr_local_inference": False,
                "input_video_url": input_media,
                "output_urls": urls,
                "output_object_key": output_key,
                "job_params": self.job_params(request),
                "request_id": result.get("RequestId", ""),
            }
        )
        return transcript.model_copy(update={"metadata": metadata})
