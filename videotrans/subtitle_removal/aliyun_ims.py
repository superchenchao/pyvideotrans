from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Mapping, Sequence


SUPPORTED_REGIONS = {
    "cn-shanghai", "cn-beijing", "ap-southeast-1", "us-west-1",
}


@dataclass(frozen=True)
class ImsConfig:
    region: str = "cn-shanghai"
    quality: str = "premium"

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "ImsConfig":
        region = str(values.get("subtitle_oss_region", "cn-shanghai")).strip()
        quality = str(values.get("subtitle_ims_quality", "premium")).strip().lower()
        return cls(
            region=region,
            quality=quality if quality in {"normal", "premium"} else "premium",
        )


def _credentials():
    try:
        from aliyunsdkcore.auth.credentials import (
            AccessKeyCredential,
            StsTokenCredential,
        )
    except ImportError as error:  # pragma: no cover - project dependency
        raise RuntimeError("缺少 aliyun-python-sdk-core，无法调用阿里云 IMS") from error
    access_key_id = (
        os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "").strip()
        or os.getenv("OSS_ACCESS_KEY_ID", "").strip()
    )
    access_key_secret = (
        os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "").strip()
        or os.getenv("OSS_ACCESS_KEY_SECRET", "").strip()
    )
    security_token = (
        os.getenv("ALIBABA_CLOUD_SECURITY_TOKEN", "").strip()
        or os.getenv("OSS_SESSION_TOKEN", "").strip()
    )
    if not access_key_id or not access_key_secret:
        raise ValueError(
            "未配置阿里云 IMS 凭据。请设置 ALIBABA_CLOUD_ACCESS_KEY_ID "
            "和 ALIBABA_CLOUD_ACCESS_KEY_SECRET 环境变量"
        )
    if security_token:
        return StsTokenCredential(access_key_id, access_key_secret, security_token)
    return AccessKeyCredential(access_key_id, access_key_secret)


class ImsClient:
    def __init__(self, config: ImsConfig, client=None):
        if config.region not in SUPPORTED_REGIONS:
            raise ValueError(
                "阿里云 IMS 字幕擦除不支持该地域："
                f"{config.region}；请选择上海、北京、新加坡或美国西部"
            )
        self.config = config
        if client is None:
            from aliyunsdkcore.client import AcsClient

            client = AcsClient(
                region_id=config.region,
                credential=_credentials(),
                connect_timeout=15,
                timeout=60,
            )
        self.client = client

    def _call(self, action: str, parameters: Mapping[str, object]) -> dict[str, object]:
        from aliyunsdkcore.request import CommonRequest

        request = CommonRequest(
            domain=f"ice.{self.config.region}.aliyuncs.com",
            version="2020-11-09",
            action_name=action,
            product="ICE",
        )
        request.set_accept_format("json")
        request.set_method("POST")
        request.set_protocol_type("https")
        for key, value in parameters.items():
            if value is not None and value != "":
                request.add_query_param(key, value)
        try:
            raw = self.client.do_action_with_exception(request)
            result = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except Exception as error:
            raise RuntimeError(f"阿里云 IMS {action} 调用失败：{error}") from error
        if not isinstance(result, dict):
            raise RuntimeError(f"阿里云 IMS {action} 返回格式异常")
        return result

    def submit(
            self, *, input_oss_uri: str, output_oss_uri: str,
            normalized_rect: Sequence[float], name: str) -> dict[str, object]:
        job_params = {
            "LimitRegion": [[round(float(value), 8) for value in normalized_rect]],
        }
        result = self._call(
            "SubmitIProductionJob",
            {
                "Name": name[:100],
                "FunctionName": "VideoDetext",
                "Input": json.dumps(
                    {"Type": "OSS", "Media": input_oss_uri},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "Output": json.dumps(
                    {"Type": "OSS", "Media": output_oss_uri},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "JobParams": json.dumps(
                    job_params, ensure_ascii=False, separators=(",", ":")
                ),
                "ModelId": (
                    "algo-video-detext-new"
                    if self.config.quality == "premium" else None
                ),
            },
        )
        job_id = str(result.get("JobId", "")).strip()
        if not job_id:
            raise RuntimeError("阿里云 IMS 未返回 JobId")
        return result

    def query(self, job_id: str) -> dict[str, object]:
        return self._call("QueryIProductionJob", {"JobId": job_id})

    def cancel(self, job_id: str) -> bool:
        try:
            self._call("CancelIProductionJob", {"JobId": job_id})
            return True
        except Exception:
            return False


def classify_status(result: Mapping[str, object]) -> tuple[str, str]:
    status = str(result.get("Status", "")).strip().lower()
    if status == "success":
        return "success", "success"
    if status == "fail":
        message = str(result.get("Message") or result.get("Result") or "IMS 任务失败")
        return "failed", message
    if status in {"queuing", "analysing"}:
        return "processing", status
    return "processing", status or "processing"
