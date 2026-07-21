from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping


ProgressCallback = Callable[[int], None]
CancelCallback = Callable[[], bool]
LogCallback = Callable[[str], None]


class CloudTransferCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class OssConfig:
    bucket: str
    region: str
    endpoint: str
    prefix: str = "pyvideotrans/subtitle-removal"
    signed_url_seconds: int = 12 * 3600

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "OssConfig":
        region = str(values.get("subtitle_oss_region", "cn-shanghai")).strip()
        endpoint = str(values.get("subtitle_oss_endpoint", "")).strip()
        if not endpoint and region:
            endpoint = f"https://oss-{region}.aliyuncs.com"
        try:
            signed_hours = float(values.get("subtitle_oss_signed_url_hours", 12))
        except (TypeError, ValueError):
            signed_hours = 12
        return cls(
            bucket=str(values.get("subtitle_oss_bucket", "")).strip(),
            region=region,
            endpoint=endpoint.rstrip("/"),
            prefix=str(
                values.get(
                    "subtitle_oss_prefix", "pyvideotrans/subtitle-removal"
                )
            ).strip().strip("/"),
            signed_url_seconds=max(1800, min(7 * 86400, int(signed_hours * 3600))),
        )

    def validate(self) -> None:
        missing = [
            name for name, value in (
                ("OSS Bucket", self.bucket),
                ("OSS Region", self.region),
                ("OSS Endpoint", self.endpoint),
            ) if not value
        ]
        if missing:
            raise ValueError("缺少云端字幕消除配置：" + "、".join(missing))
        if not self.endpoint.lower().startswith("https://"):
            raise ValueError("OSS Endpoint 必须使用 HTTPS")


def _credentials() -> tuple[str, str, str]:
    access_key_id = (
        os.getenv("OSS_ACCESS_KEY_ID", "").strip()
        or os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "").strip()
    )
    access_key_secret = (
        os.getenv("OSS_ACCESS_KEY_SECRET", "").strip()
        or os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "").strip()
    )
    security_token = (
        os.getenv("OSS_SESSION_TOKEN", "").strip()
        or os.getenv("ALIBABA_CLOUD_SECURITY_TOKEN", "").strip()
    )
    if not access_key_id or not access_key_secret:
        raise ValueError(
            "未配置 OSS 凭据。请设置 OSS_ACCESS_KEY_ID 和 "
            "OSS_ACCESS_KEY_SECRET 环境变量"
        )
    return access_key_id, access_key_secret, security_token


def create_bucket(config: OssConfig, *, pool_size: int = 4):
    try:
        import oss2
        from oss2.credentials import StaticCredentialsProvider
    except ImportError as error:  # pragma: no cover - dependency is pinned
        raise RuntimeError("缺少 oss2，无法使用云端字幕消除") from error

    config.validate()
    access_key_id, access_key_secret, security_token = _credentials()
    provider = StaticCredentialsProvider(
        access_key_id, access_key_secret, security_token
    )
    auth = oss2.ProviderAuthV4(provider)
    session = oss2.Session(pool_size=max(4, int(pool_size)))
    return oss2.Bucket(
        auth,
        config.endpoint,
        config.bucket,
        region=config.region,
        session=session,
        connect_timeout=(30, 300),
    )


def _progress_callback(
        *, total_size: int, stage_start: int, stage_end: int,
        progress_callback: ProgressCallback | None,
        cancel_callback: CancelCallback | None):
    last_percent = -1
    started_at = time.monotonic()

    def callback(consumed_bytes: int, total_bytes: int | None) -> None:
        nonlocal last_percent
        if cancel_callback and cancel_callback():
            raise CloudTransferCancelled("云端字幕消除传输已取消")
        total = int(total_bytes or total_size or 0)
        percent = int(consumed_bytes * 100 / total) if total else 0
        if progress_callback and (percent >= last_percent + 1 or consumed_bytes == total):
            mapped = stage_start + round(
                max(0, min(100, percent)) * (stage_end - stage_start) / 100
            )
            progress_callback(mapped)
            last_percent = percent
        # Accessing elapsed here intentionally keeps this callback suitable for
        # future speed reporting without logging signed URLs or credentials.
        _ = time.monotonic() - started_at

    return callback


def head_object(config: OssConfig, object_key: str) -> dict[str, object]:
    try:
        result = create_bucket(config).head_object(object_key)
    except Exception as error:
        raise RuntimeError(f"OSS 对象检查失败：{error}") from error
    return {
        "content_length": int(getattr(result, "content_length", 0) or 0),
        "etag": str(getattr(result, "etag", "") or ""),
        "last_modified": getattr(result, "last_modified", None),
    }


def upload_file(
        config: OssConfig, local_path: str | Path, object_key: str, *,
        checkpoint_root: str | Path,
        progress_callback: ProgressCallback | None = None,
        cancel_callback: CancelCallback | None = None,
        log_callback: LogCallback | None = None) -> dict[str, object]:
    import oss2

    source = Path(local_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"待上传视频不存在：{source}")
    bucket = create_bucket(config, pool_size=4)
    checkpoint = Path(checkpoint_root).resolve()
    checkpoint.mkdir(parents=True, exist_ok=True)
    if log_callback:
        log_callback(
            f"上传 API 工作视频到 OSS：bucket={config.bucket}, key={object_key}"
        )
    try:
        result = oss2.resumable_upload(
            bucket,
            object_key,
            str(source),
            store=oss2.ResumableStore(root=str(checkpoint)),
            multipart_threshold=32 * 1024 * 1024,
            part_size=8 * 1024 * 1024,
            progress_callback=_progress_callback(
                total_size=source.stat().st_size,
                stage_start=0,
                stage_end=30,
                progress_callback=progress_callback,
                cancel_callback=cancel_callback,
            ),
            num_threads=4,
        )
    except CloudTransferCancelled:
        raise
    except Exception as error:
        raise RuntimeError(f"OSS 断点续传上传失败：{error}") from error

    metadata = head_object(config, object_key)
    if metadata["content_length"] != source.stat().st_size:
        raise RuntimeError(
            "OSS 上传后大小校验失败："
            f"local={source.stat().st_size}, remote={metadata['content_length']}"
        )
    return {
        **metadata,
        "object_key": object_key,
        "etag": str(getattr(result, "etag", "") or metadata.get("etag", "")),
    }


def signed_download_url(config: OssConfig, object_key: str) -> str:
    try:
        return create_bucket(config).sign_url(
            "GET",
            object_key,
            config.signed_url_seconds,
            slash_safe=True,
        )
    except Exception as error:
        raise RuntimeError(f"生成 OSS 临时下载链接失败：{error}") from error


def download_object(
        config: OssConfig, object_key: str, destination: str | Path, *,
        progress_callback: ProgressCallback | None = None,
        cancel_callback: CancelCallback | None = None) -> Path:
    destination = Path(destination).resolve()
    part_path = Path(f"{destination}.part")
    part_path.parent.mkdir(parents=True, exist_ok=True)
    remote = head_object(config, object_key)
    try:
        import oss2

        checkpoint = destination.parent / ".oss-resumable"
        checkpoint.mkdir(parents=True, exist_ok=True)
        oss2.resumable_download(
            create_bucket(config),
            object_key,
            str(part_path),
            store=oss2.ResumableStore(root=str(checkpoint)),
            multiget_threshold=32 * 1024 * 1024,
            part_size=8 * 1024 * 1024,
            num_threads=4,
            progress_callback=_progress_callback(
                total_size=int(remote["content_length"]),
                stage_start=85,
                stage_end=100,
                progress_callback=progress_callback,
                cancel_callback=cancel_callback,
            ),
        )
    except CloudTransferCancelled:
        raise
    except Exception as error:
        raise RuntimeError(f"OSS 结果下载失败：{error}") from error
    if part_path.stat().st_size != int(remote["content_length"]):
        raise RuntimeError("OSS 结果下载后大小校验失败")
    part_path.replace(destination)
    return destination
