from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any


class AliyunICEError(RuntimeError):
    """Normalized Alibaba Cloud Intelligent Media Services failure."""


@dataclass(frozen=True)
class AliyunICEConfig:
    region: str
    access_key_id: str
    access_key_secret: str
    security_token: str = ""
    connect_timeout_seconds: float = 15.0
    request_timeout_seconds: float = 60.0
    poll_interval_seconds: float = 1.0
    max_poll_interval_seconds: float = 5.0
    job_timeout_seconds: float = 900.0


class AliyunICEClient:
    """Small independent client for the ICE 2020-11-09 RPC API."""

    def __init__(self, config: AliyunICEConfig, *, rpc_client=None) -> None:
        self.config = config
        self._client = rpc_client

    @property
    def configured(self) -> bool:
        return bool(
            self.config.region.strip()
            and self.config.access_key_id.strip()
            and self.config.access_key_secret.strip()
        )

    def _build_client(self):
        if not self.configured:
            raise AliyunICEError("Alibaba ICE credentials or region are missing")
        try:
            from aliyunsdkcore.auth.credentials import (
                AccessKeyCredential,
                StsTokenCredential,
            )
            from aliyunsdkcore.client import AcsClient
        except ImportError as exc:  # pragma: no cover - production dependency
            raise AliyunICEError(
                "install the 'aliyun' optional dependencies to call Alibaba ICE"
            ) from exc

        if self.config.security_token.strip():
            credential = StsTokenCredential(
                self.config.access_key_id.strip(),
                self.config.access_key_secret.strip(),
                self.config.security_token.strip(),
            )
        else:
            credential = AccessKeyCredential(
                self.config.access_key_id.strip(),
                self.config.access_key_secret.strip(),
            )
        return AcsClient(
            region_id=self.config.region.strip(),
            credential=credential,
            connect_timeout=self.config.connect_timeout_seconds,
            timeout=self.config.request_timeout_seconds,
        )

    def _rpc_client(self):
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def call(self, action: str, parameters: dict[str, object]) -> dict[str, Any]:
        try:
            from aliyunsdkcore.request import CommonRequest
        except ImportError as exc:  # pragma: no cover - production dependency
            raise AliyunICEError(
                "install the 'aliyun' optional dependencies to call Alibaba ICE"
            ) from exc

        request = CommonRequest(
            domain=f"ice.{self.config.region.strip()}.aliyuncs.com",
            version="2020-11-09",
            action_name=action,
            product="ICE",
        )
        request.set_accept_format("json")
        request.set_method("POST")
        request.set_protocol_type("https")
        for key, value in parameters.items():
            if value is None or value == "":
                continue
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            request.add_query_param(key, value)
        try:
            raw = self._rpc_client().do_action_with_exception(request)
            payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except Exception as exc:
            raise AliyunICEError(f"Alibaba ICE {action} failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise AliyunICEError(f"Alibaba ICE {action} returned a non-object response")
        return payload

    async def acall(self, action: str, parameters: dict[str, object]) -> dict[str, Any]:
        return await asyncio.to_thread(self.call, action, parameters)

    async def submit_i_production(
        self,
        *,
        name: str,
        function_name: str,
        input_media: str,
        output_media: str,
        client_token: str,
        job_params: dict[str, object] | None = None,
        model_id: str = "",
    ) -> str:
        result = await self.acall(
            "SubmitIProductionJob",
            {
                "Name": name[:100],
                "FunctionName": function_name,
                "Input": {"Type": "OSS", "Media": input_media},
                "Output": {"Type": "OSS", "Media": output_media},
                "JobParams": job_params or None,
                "ModelId": model_id or None,
                "ClientToken": client_token[:64],
            },
        )
        job_id = str(result.get("JobId", "") or "").strip()
        if not job_id:
            raise AliyunICEError(
                f"Alibaba ICE {function_name} submit response is missing JobId"
            )
        return job_id

    async def query_i_production(self, job_id: str) -> dict[str, Any]:
        return await self.acall("QueryIProductionJob", {"JobId": job_id})

    async def wait_i_production(self, job_id: str) -> dict[str, Any]:
        started = time.monotonic()
        delay = max(0.05, self.config.poll_interval_seconds)
        while True:
            if time.monotonic() - started >= self.config.job_timeout_seconds:
                raise AliyunICEError(
                    f"Alibaba ICE intelligent production job {job_id} exceeded "
                    f"{self.config.job_timeout_seconds:.0f}s polling timeout"
                )
            result = await self.query_i_production(job_id)
            status = str(result.get("Status", "") or "").strip().lower()
            if status == "success":
                return result
            if status in {"fail", "failed", "canceled", "cancelled"}:
                detail = result.get("Message") or result.get("Result") or status
                raise AliyunICEError(
                    f"Alibaba ICE intelligent production job {job_id} failed: {detail}"
                )
            if status not in {
                "",
                "init",
                "queuing",
                "queueing",
                "analysing",
                "analyzing",
                "processing",
            }:
                raise AliyunICEError(
                    f"Alibaba ICE intelligent production job {job_id} returned "
                    f"unexpected status {status}"
                )
            await asyncio.sleep(delay)
            delay = min(self.config.max_poll_interval_seconds, delay * 1.35)

    async def submit_media_producing(
        self,
        *,
        timeline: dict[str, object],
        output_media_url: str,
        client_token: str,
        output_config: dict[str, object] | None = None,
        user_data: dict[str, object] | None = None,
    ) -> str:
        config = {"MediaURL": output_media_url, **(output_config or {})}
        result = await self.acall(
            "SubmitMediaProducingJob",
            {
                "Timeline": timeline,
                "OutputMediaTarget": "oss-object",
                "OutputMediaConfig": config,
                "UserData": user_data or None,
                "ClientToken": client_token[:64],
                "Source": "OpenAPI",
            },
        )
        job_id = str(result.get("JobId", "") or "").strip()
        if not job_id:
            raise AliyunICEError(
                "Alibaba ICE media producing submit response is missing JobId"
            )
        return job_id

    async def get_media_producing(self, job_id: str) -> dict[str, Any]:
        return await self.acall("GetMediaProducingJob", {"JobId": job_id})

    async def wait_media_producing(self, job_id: str) -> dict[str, Any]:
        started = time.monotonic()
        delay = max(0.05, self.config.poll_interval_seconds)
        while True:
            if time.monotonic() - started >= self.config.job_timeout_seconds:
                raise AliyunICEError(
                    f"Alibaba ICE media producing job {job_id} exceeded "
                    f"{self.config.job_timeout_seconds:.0f}s polling timeout"
                )
            result = await self.get_media_producing(job_id)
            job = result.get("MediaProducingJob", result)
            status = str(job.get("Status", "") or "").strip().lower()
            if status == "success":
                return job
            if status in {"failed", "fail", "canceled", "cancelled"}:
                detail = job.get("Message") or job.get("Code") or status
                raise AliyunICEError(
                    f"Alibaba ICE media producing job {job_id} failed: {detail}"
                )
            if status not in {"", "init", "queuing", "queueing", "processing"}:
                raise AliyunICEError(
                    f"Alibaba ICE media producing job {job_id} returned "
                    f"unexpected status {status}"
                )
            await asyncio.sleep(delay)
            delay = min(self.config.max_poll_interval_seconds, delay * 1.35)
