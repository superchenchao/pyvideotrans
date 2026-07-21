from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Callable, Mapping, Sequence

import requests

from .aliyun_ims import ImsClient, ImsConfig, classify_status as classify_ims
from .caca_api import CacaClient, CacaConfig, classify_status as classify_caca
from .cloud_state import matching_state, write_state
from .cloud_storage import (
    CloudTransferCancelled,
    OssConfig,
    download_object,
    head_object,
    signed_download_url,
    upload_file,
)


LOCAL_PROVIDER = "local"
CACA_PROVIDER = "caca_link"
ALIYUN_IMS_PROVIDER = "aliyun_ims"
CLOUD_PROVIDERS = {CACA_PROVIDER, ALIYUN_IMS_PROVIDER}

ProgressCallback = Callable[[int], None]
CancelCallback = Callable[[], bool]
LogCallback = Callable[[str], None]


class CloudSubtitleRemovalCancelled(RuntimeError):
    pass


def normalize_provider(value: object) -> str:
    provider = str(value or LOCAL_PROVIDER).strip().lower()
    return provider if provider in {LOCAL_PROVIDER, *CLOUD_PROVIDERS} else LOCAL_PROVIDER


def validate_cloud_configuration(
        provider: str, values: Mapping[str, object]) -> None:
    provider = normalize_provider(provider)
    if provider == LOCAL_PROVIDER:
        return
    OssConfig.from_mapping(values).validate()
    # Constructing the clients validates credentials without making a request.
    if provider == CACA_PROVIDER:
        CacaClient(CacaConfig.from_mapping(values))
    elif provider == ALIYUN_IMS_PROVIDER:
        ImsClient(ImsConfig.from_mapping(values))


def cloud_strategy_key(provider: str, values: Mapping[str, object]) -> dict[str, object]:
    oss = OssConfig.from_mapping(values)
    provider = normalize_provider(provider)
    base: dict[str, object] = {
        "provider": provider,
        "region": oss.region,
        "bucket": oss.bucket,
        "prefix": oss.prefix,
    }
    if provider == CACA_PROVIDER:
        config = CacaConfig.from_mapping(values)
        base.update({
            "base_url": config.base_url,
            "mode": config.mode,
            "send_region": config.send_region,
        })
    elif provider == ALIYUN_IMS_PROVIDER:
        base["quality"] = ImsConfig.from_mapping(values).quality
    return base


def _identity_token(identity: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(identity), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _cancelled(cancel_callback: CancelCallback | None) -> bool:
    return bool(cancel_callback and cancel_callback())


def _poll_sleep(seconds: int, cancel_callback: CancelCallback | None) -> None:
    deadline = time.monotonic() + max(0, seconds)
    while time.monotonic() < deadline:
        if _cancelled(cancel_callback):
            raise CloudSubtitleRemovalCancelled("云端字幕消除已取消")
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))


def _download_http(
        url: str, destination: Path, *,
        progress_callback: ProgressCallback | None,
        cancel_callback: CancelCallback | None) -> Path:
    if not str(url).lower().startswith("https://"):
        raise RuntimeError("云端字幕消除结果必须使用 HTTPS 下载")
    part_path = Path(f"{destination}.part")
    part_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = part_path.stat().st_size if part_path.is_file() else 0
        headers = {"Range": f"bytes={existing}-"} if existing else {}
        with requests.get(
                url, headers=headers, stream=True, timeout=(30, 900)
        ) as response:
            response.raise_for_status()
            partial = existing > 0 and response.status_code == 206
            if existing and not partial:
                existing = 0
            content_length = int(response.headers.get("Content-Length", "0") or 0)
            content_range = response.headers.get("Content-Range", "")
            try:
                total = int(content_range.rsplit("/", 1)[1]) if "/" in content_range else 0
            except (TypeError, ValueError):
                total = 0
            total = total or (existing + content_length if content_length else 0)
            consumed = existing
            with part_path.open("ab" if partial else "wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if _cancelled(cancel_callback):
                        raise CloudSubtitleRemovalCancelled("云端结果下载已取消")
                    if not chunk:
                        continue
                    output.write(chunk)
                    consumed += len(chunk)
                    if progress_callback and total:
                        progress_callback(85 + round(consumed * 15 / total))
    except CloudSubtitleRemovalCancelled:
        raise
    except Exception as error:
        raise RuntimeError(f"云端字幕消除结果下载失败：{error}") from error
    if total and part_path.stat().st_size != total:
        raise RuntimeError("云端字幕消除结果大小校验失败")
    part_path.replace(destination)
    return destination


def remove_burned_subtitles_cloud(
        *, provider: str, input_file: str, output_file: str,
        normalized_rect: Sequence[float], duration_ms: int,
        identity: Mapping[str, object], settings_values: Mapping[str, object],
        progress_callback: ProgressCallback | None = None,
        log_callback: LogCallback | None = None,
        cancel_callback: CancelCallback | None = None) -> str:
    provider = normalize_provider(provider)
    if provider not in CLOUD_PROVIDERS:
        raise ValueError(f"不是云端字幕消除方式：{provider}")
    if len(normalized_rect) != 4:
        raise ValueError("云端字幕消除需要一个归一化字幕区域")

    oss = OssConfig.from_mapping(settings_values)
    validate_cloud_configuration(provider, settings_values)
    full_identity = {
        **dict(identity),
        "cloud_strategy": cloud_strategy_key(provider, settings_values),
        "rect": [round(float(value), 8) for value in normalized_rect],
        "duration_ms": int(duration_ms),
    }
    token = _identity_token(full_identity)
    prefix = f"{oss.prefix}/{provider}/{token}".strip("/")
    input_key = f"{prefix}/input-30fps-1080p-6000k-noaudio.mp4"
    output_key = f"{prefix}/output-clean.mp4"
    state_path = Path(f"{output_file}.cloud.json")
    state = matching_state(state_path, full_identity)
    if not state:
        state = {
            "identity": full_identity,
            "provider": provider,
            "status": "new",
            "input_object_key": input_key,
            "output_object_key": output_key,
        }
        write_state(state_path, state)

    if _cancelled(cancel_callback):
        raise CloudSubtitleRemovalCancelled("云端字幕消除已取消")

    if state.get("status") == "submit_unknown" and not (
            state.get("task_id") or state.get("job_id")):
        raise RuntimeError(
            "上次云端任务提交结果未知。为避免重复计费，程序不会自动重提；"
            f"请人工核对后处理状态文件：{state_path}"
        )

    upload_needed = state.get("status") == "new"
    if not upload_needed:
        try:
            remote = head_object(oss, input_key)
            upload_needed = int(remote.get("content_length", 0)) != Path(input_file).stat().st_size
        except Exception:
            upload_needed = True
    if upload_needed:
        upload_file(
            oss,
            input_file,
            input_key,
            checkpoint_root=Path(input_file).parent / ".oss-resumable",
            progress_callback=progress_callback,
            cancel_callback=cancel_callback,
            log_callback=log_callback,
        )
        state["status"] = "uploaded"
        write_state(state_path, state)

    try:
        poll_seconds = int(settings_values.get("subtitle_cloud_poll_seconds", 30))
    except (TypeError, ValueError):
        poll_seconds = 30
    poll_seconds = max(30, min(300, poll_seconds))

    if provider == CACA_PROVIDER:
        client = CacaClient(CacaConfig.from_mapping(settings_values))
        task_id = str(state.get("task_id", "")).strip()
        if not task_id:
            state["status"] = "submitting"
            write_state(state_path, state)
            try:
                submitted = client.submit(
                    signed_download_url(oss, input_key), normalized_rect
                )
            except Exception:
                state["status"] = "submit_unknown"
                write_state(state_path, state)
                raise RuntimeError(
                    "链接字幕消除任务提交结果未知。为避免重复计费，程序不会自动重提；"
                    f"请保留状态文件：{state_path}"
                )
            task_id = str(submitted["taskId"])
            state.update({"task_id": task_id, "status": "processing"})
            write_state(state_path, state)

        elapsed_polls = 0
        while True:
            if _cancelled(cancel_callback):
                state["status"] = "cancelled_local"
                write_state(state_path, state)
                raise CloudSubtitleRemovalCancelled("云端字幕消除已取消，远端任务可继续查询")
            result = client.query(task_id)
            status, detail = classify_caca(result)
            state.update({"status": status, "remote_message": detail})
            write_state(state_path, state)
            if status == "success":
                if log_callback:
                    log_callback("链接字幕消除 API 已完成，正在下载结果")
                _download_http(
                    detail,
                    Path(output_file),
                    progress_callback=progress_callback,
                    cancel_callback=cancel_callback,
                )
                break
            if status == "failed":
                raise RuntimeError(f"链接字幕消除 API 失败：{detail}")
            elapsed_polls += 1
            if progress_callback:
                progress_callback(min(84, 30 + elapsed_polls * 2))
            _poll_sleep(poll_seconds, cancel_callback)
    else:
        client = ImsClient(ImsConfig.from_mapping(settings_values))
        job_id = str(state.get("job_id", "")).strip()
        if not job_id:
            state["status"] = "submitting"
            write_state(state_path, state)
            try:
                submitted = client.submit(
                    input_oss_uri=f"oss://{oss.bucket}/{input_key}",
                    output_oss_uri=f"oss://{oss.bucket}/{output_key}",
                    normalized_rect=normalized_rect,
                    name=f"pyVideoTrans-{token}",
                )
            except Exception:
                state["status"] = "submit_unknown"
                write_state(state_path, state)
                raise RuntimeError(
                    "阿里云 IMS 任务提交结果未知。为避免重复计费，程序不会自动重提；"
                    f"请保留状态文件：{state_path}"
                )
            job_id = str(submitted["JobId"])
            state.update({"job_id": job_id, "status": "processing"})
            write_state(state_path, state)

        elapsed_polls = 0
        while True:
            if _cancelled(cancel_callback):
                cancelled = client.cancel(job_id)
                state.update({
                    "status": "cancel_requested" if cancelled else "cancelled_local"
                })
                write_state(state_path, state)
                raise CloudSubtitleRemovalCancelled(
                    "阿里云 IMS 字幕消除已取消；远端取消结果将在下次运行时继续查询"
                )
            result = client.query(job_id)
            status, detail = classify_ims(result)
            state.update({"status": status, "remote_message": detail})
            write_state(state_path, state)
            if status == "success":
                if log_callback:
                    log_callback("阿里云 IMS 已完成，正在从 OSS 下载结果")
                download_object(
                    oss,
                    output_key,
                    output_file,
                    progress_callback=progress_callback,
                    cancel_callback=cancel_callback,
                )
                break
            if status == "failed":
                raise RuntimeError(f"阿里云 IMS 字幕消除失败：{detail}")
            elapsed_polls += 1
            if progress_callback:
                progress_callback(min(84, 30 + elapsed_polls * 2))
            _poll_sleep(poll_seconds, cancel_callback)

    state["status"] = "downloaded"
    write_state(state_path, state)
    if progress_callback:
        progress_callback(100)
    return str(Path(output_file).resolve())
