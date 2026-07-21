from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Sequence

import requests


DEFAULT_BASE_URL = (
    "https://env-00jxh2cj3gfd.dev-hz.cloudbasefunction.cn/http/router"
)


def _credentials() -> tuple[str, str]:
    secret_id = os.getenv("PYVIDEOTRANS_CACA_SECRET_ID", "").strip()
    secret_key = os.getenv("PYVIDEOTRANS_CACA_SECRET_KEY", "").strip()
    if not secret_id or not secret_key:
        raise ValueError(
            "未配置链接字幕消除 API 凭据。请设置 "
            "PYVIDEOTRANS_CACA_SECRET_ID 和 "
            "PYVIDEOTRANS_CACA_SECRET_KEY 环境变量"
        )
    return secret_id, secret_key


@dataclass(frozen=True)
class CacaConfig:
    base_url: str = DEFAULT_BASE_URL
    mode: str = "protect"
    timeout_seconds: int = 60
    send_region: bool = False

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "CacaConfig":
        try:
            timeout = int(values.get("subtitle_caca_timeout_seconds", 60))
        except (TypeError, ValueError):
            timeout = 60
        mode = str(values.get("subtitle_caca_mode", "protect")).strip().lower()
        return cls(
            base_url=str(
                values.get("subtitle_caca_base_url", DEFAULT_BASE_URL)
            ).strip().rstrip("/"),
            mode=mode if mode in {"normal", "protect"} else "protect",
            timeout_seconds=max(10, min(300, timeout)),
            send_region=bool(values.get("subtitle_caca_send_region", False)),
        )


class CacaClient:
    def __init__(self, config: CacaConfig, session: requests.Session | None = None):
        if not config.base_url.lower().startswith("https://"):
            raise ValueError("链接字幕消除 API 地址必须使用 HTTPS")
        self.config = config
        self.session = session or requests.Session()
        self.secret_id, self.secret_key = _credentials()

    def _post(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        response = self.session.post(
            f"{self.config.base_url}/user/pub/{action}",
            json={
                **payload,
                "secret_id": self.secret_id,
                "secret_key": self.secret_key,
            },
            timeout=(15, self.config.timeout_seconds),
            allow_redirects=False,
        )
        response.raise_for_status()
        try:
            result = response.json()
        except ValueError as error:
            raise RuntimeError("链接字幕消除 API 返回了非 JSON 内容") from error
        if not isinstance(result, dict):
            raise RuntimeError("链接字幕消除 API 返回格式异常")
        return result

    def submit(
            self, video_link: str, normalized_rect: Sequence[float]
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "video_link": video_link,
            "x1": 0,
            "y1": 0,
            "x2": 0,
            "y2": 0,
            "mode": self.config.mode,
        }
        # The supplied API document does not define the coordinate unit.  Keep
        # automatic detection as the safe default; users can explicitly enable
        # normalized corner coordinates after their provider confirms it.
        if self.config.send_region and len(normalized_rect) == 4:
            x, y, width, height = (float(value) for value in normalized_rect)
            payload.update({"x1": x, "y1": y, "x2": x + width, "y2": y + height})
        result = self._post("cacaLinkPullAsyc", payload)
        task_id = str(result.get("taskId", "")).strip()
        if not task_id or int(result.get("isOk", 0) or 0) != 1:
            raise RuntimeError(
                "链接字幕消除任务提交失败："
                + str(result.get("msg") or "未返回 taskId")
            )
        return result

    def query(self, task_id: str) -> dict[str, object]:
        return self._post("cacaLinkPullStatus", {"taskId": task_id})


def classify_status(result: Mapping[str, object]) -> tuple[str, str]:
    status = str(result.get("status", "")).strip().lower()
    media = str(result.get("media", "")).strip()
    code = int(result.get("code", 0) or 0)
    if media and (code == 0 or status == "finished"):
        return "success", media
    if status in {"failed", "fail"} or code < 0:
        detail = str(result.get("lastError") or result.get("msg") or "远端任务失败")
        return "failed", detail
    return "processing", str(result.get("msg") or status or "processing")
