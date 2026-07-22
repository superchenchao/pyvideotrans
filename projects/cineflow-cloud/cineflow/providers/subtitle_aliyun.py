from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from typing import Any

from ..subtitle_models import ProviderSubmission, SubtitleRemovalRequest
from .aliyun_ice import AliyunICEClient, AliyunICEError
from .aliyun_oss import AliyunOSSStore


@dataclass(frozen=True)
class AliyunSubtitleConfig:
    default_model_id: str = "algo-video-detext-new"


class AliyunVideoDetextProvider:
    name = "aliyun"

    def __init__(
        self,
        ice: AliyunICEClient,
        store: AliyunOSSStore,
        config: AliyunSubtitleConfig | None = None,
    ) -> None:
        self.ice = ice
        self.store = store
        self.config = config or AliyunSubtitleConfig()

    @property
    def configured(self) -> bool:
        return bool(self.ice.configured and self.store.configured)

    @property
    def detail(self) -> str:
        return (
            "Alibaba ICE VideoDetext ready" if self.configured else "ICE or OSS is not configured"
        )

    @staticmethod
    def _job_params(request: SubtitleRemovalRequest) -> dict[str, object]:
        params: dict[str, object] = {}
        if request.regions:
            params["LimitRegion"] = [region.as_aliyun() for region in request.regions]
        if request.time_ranges:
            values = [item.as_aliyun() for item in request.time_ranges]
            params["Time"] = values[0] if len(values) == 1 else values
        return params

    async def submit(
        self,
        request: SubtitleRemovalRequest,
        *,
        job_id: str,
        output_object_key: str,
    ) -> ProviderSubmission:
        if not self.configured:
            raise AliyunICEError(self.detail)
        input_media = self.store.canonicalize_oss_input(str(request.input_url))
        output_media = self.store.oss_uri(output_object_key)
        external_job_id = await self.ice.submit_i_production(
            name=f"cineflow-detext-{job_id[:16]}",
            function_name="VideoDetext",
            input_media=input_media,
            output_media=output_media,
            client_token=f"detext-{job_id}"[:64],
            job_params=self._job_params(request),
            model_id=request.model_id.strip() or self.config.default_model_id,
        )
        return ProviderSubmission(
            provider=self.name,
            external_job_id=external_job_id,
            output_object_key=output_object_key,
            output_url=self.store.canonical_url(output_object_key),
            completed=False,
            metadata={
                "function_name": "VideoDetext",
                "job_params": self._job_params(request),
                "model_id": request.model_id.strip() or self.config.default_model_id,
            },
        )

    @staticmethod
    def _walk_urls(value: object) -> list[str]:
        urls: list[str] = []
        if isinstance(value, dict):
            for child in value.values():
                urls.extend(AliyunVideoDetextProvider._walk_urls(child))
        elif isinstance(value, list):
            for child in value:
                urls.extend(AliyunVideoDetextProvider._walk_urls(child))
        elif isinstance(value, str):
            text = value.strip()
            if text.startswith(("http://", "https://", "oss://")):
                urls.append(text)
            elif text.startswith(("{", "[")):
                with contextlib.suppress(json.JSONDecodeError):
                    urls.extend(AliyunVideoDetextProvider._walk_urls(json.loads(text)))
        return urls

    async def wait(self, submission: ProviderSubmission) -> ProviderSubmission:
        if not submission.external_job_id:
            raise AliyunICEError("VideoDetext submission is missing an external job ID")
        result = await self.ice.wait_i_production(submission.external_job_id)
        candidates: list[str] = []
        for field in ("OutputUrls", "OutputFiles", "Result", "Output"):
            candidates.extend(self._walk_urls(result.get(field)))
        expected = self.store.canonical_url(submission.output_object_key)
        output_url = next(
            (
                value
                for value in candidates
                if submission.output_object_key and submission.output_object_key in value
            ),
            expected,
        )
        metadata: dict[str, Any] = dict(submission.metadata)
        metadata.update(
            {
                "provider_status": result.get("Status"),
                "output_candidates": candidates,
                "raw_request_id": result.get("RequestId"),
            }
        )
        return submission.model_copy(
            update={
                "output_url": output_url,
                "completed": True,
                "metadata": metadata,
            }
        )

    async def cancel(self, submission: ProviderSubmission) -> None:
        # The currently exposed ICE adapter has no cancellation operation. Keeping
        # this method explicit lets the worker record cancellation and clean OSS.
        del submission
