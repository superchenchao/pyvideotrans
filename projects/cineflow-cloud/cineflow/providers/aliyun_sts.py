from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


class AliyunSTSError(RuntimeError):
    """Normalized Alibaba Cloud STS failure."""


@dataclass(frozen=True)
class AliyunSTSConfig:
    region: str
    role_arn: str
    access_key_id: str
    access_key_secret: str
    security_token: str = ""
    endpoint: str = ""
    duration_seconds: int = 3600
    connect_timeout_seconds: float = 10.0
    request_timeout_seconds: float = 20.0


@dataclass(frozen=True)
class TemporaryCredentials:
    access_key_id: str
    access_key_secret: str
    security_token: str
    expiration: str

    @property
    def expiration_epoch(self) -> int:
        text = self.expiration.replace("Z", "+00:00")
        return int(datetime.fromisoformat(text).astimezone(UTC).timestamp())


class AliyunSTSClient:
    """Issue short-lived credentials restricted to one OSS object.

    SDK construction is lazy so the upload worker remains importable in demo and
    CI environments where Alibaba credentials or optional dependencies are absent.
    """

    def __init__(self, config: AliyunSTSConfig, *, rpc_client=None) -> None:
        self.config = config
        self._client = rpc_client

    @property
    def configured(self) -> bool:
        return bool(
            self.config.region.strip()
            and self.config.role_arn.strip()
            and self.config.access_key_id.strip()
            and self.config.access_key_secret.strip()
        )

    def _build_client(self):
        if not self.configured:
            raise AliyunSTSError("Alibaba STS role, region, or credentials are missing")
        try:
            from aliyunsdkcore.auth.credentials import (
                AccessKeyCredential,
                StsTokenCredential,
            )
            from aliyunsdkcore.client import AcsClient
        except ImportError as exc:  # pragma: no cover - production dependency
            raise AliyunSTSError(
                "install the 'aliyun' optional dependencies to issue STS credentials"
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

    def _domain(self) -> str:
        configured = self.config.endpoint.strip()
        if configured:
            return configured.removeprefix("https://").removeprefix("http://").rstrip("/")
        return f"sts.{self.config.region.strip()}.aliyuncs.com"

    @staticmethod
    def object_policy(
        bucket: str,
        object_key: str,
        *,
        allow_read: bool = True,
    ) -> dict[str, Any]:
        object_resource = f"acs:oss:*:*:{bucket}/{object_key.lstrip('/')}"
        actions = [
            "oss:PutObject",
            "oss:AbortMultipartUpload",
            "oss:ListParts",
        ]
        if allow_read:
            actions.extend(["oss:GetObject", "oss:GetObjectMeta"])
        return {
            "Version": "1",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": actions,
                    "Resource": [object_resource],
                }
            ],
        }

    def assume_role(
        self,
        *,
        role_session_name: str,
        policy: dict[str, Any],
        duration_seconds: int | None = None,
    ) -> TemporaryCredentials:
        try:
            from aliyunsdkcore.request import CommonRequest
        except ImportError as exc:  # pragma: no cover - production dependency
            raise AliyunSTSError(
                "install the 'aliyun' optional dependencies to issue STS credentials"
            ) from exc

        duration = int(duration_seconds or self.config.duration_seconds)
        duration = min(43_200, max(900, duration))
        request = CommonRequest(
            domain=self._domain(),
            version="2015-04-01",
            action_name="AssumeRole",
            product="Sts",
        )
        request.set_accept_format("json")
        request.set_method("POST")
        request.set_protocol_type("https")
        request.add_query_param("RoleArn", self.config.role_arn.strip())
        request.add_query_param("RoleSessionName", role_session_name[:64])
        request.add_query_param("DurationSeconds", duration)
        request.add_query_param(
            "Policy",
            json.dumps(policy, ensure_ascii=False, separators=(",", ":")),
        )
        try:
            raw = self._rpc_client().do_action_with_exception(request)
            payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except Exception as exc:
            raise AliyunSTSError(f"Alibaba STS AssumeRole failed: {exc}") from exc

        credentials = payload.get("Credentials") if isinstance(payload, dict) else None
        if not isinstance(credentials, dict):
            raise AliyunSTSError("Alibaba STS response is missing Credentials")
        result = TemporaryCredentials(
            access_key_id=str(credentials.get("AccessKeyId", "") or ""),
            access_key_secret=str(credentials.get("AccessKeySecret", "") or ""),
            security_token=str(credentials.get("SecurityToken", "") or ""),
            expiration=str(credentials.get("Expiration", "") or ""),
        )
        if not all(
            [
                result.access_key_id,
                result.access_key_secret,
                result.security_token,
                result.expiration,
            ]
        ):
            raise AliyunSTSError("Alibaba STS returned incomplete credentials")
        return result

    async def aassume_role(
        self,
        *,
        role_session_name: str,
        policy: dict[str, Any],
        duration_seconds: int | None = None,
    ) -> TemporaryCredentials:
        return await asyncio.to_thread(
            self.assume_role,
            role_session_name=role_session_name,
            policy=policy,
            duration_seconds=duration_seconds,
        )
