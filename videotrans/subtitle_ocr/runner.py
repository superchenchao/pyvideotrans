from __future__ import annotations

import json
import math
import os
import subprocess
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from videotrans.configure.config import ROOT_DIR, logger
from videotrans.subtitle_removal.engine import find_subtitle_remover_engine

from .fusion import build_ocr_subtitles


EVENT_PREFIX = "PYVT_OCR_EVENT "


def _creation_flags() -> int:
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def _worker_environment(python: Path) -> dict[str, str]:
    """Build an explicit environment for the external OCR interpreter."""
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"

    scripts_dir = python.parent
    env["VIRTUAL_ENV"] = str(scripts_dir.parent)
    current_path = env.get("PATH", "")
    env["PATH"] = str(scripts_dir) + (os.pathsep + current_path if current_path else "")
    return env


def _roi_command_args(normalized_rect: Optional[Sequence[float]]) -> list[str]:
    if not normalized_rect:
        return []
    if len(normalized_rect) != 4:
        raise ValueError("OCR subtitle region must contain x, y, width and height")

    left, top, width, height = (float(value) for value in normalized_rect)
    if not all(math.isfinite(value) for value in (left, top, width, height)):
        raise ValueError("OCR subtitle region must contain finite numbers")
    right = left + width
    bottom = top + height
    if (
            left < 0 or top < 0 or width <= 0 or height <= 0
            or right > 1.000001 or bottom > 1.000001
    ):
        raise ValueError("OCR subtitle region must stay inside the video frame")
    right = min(1.0, right)
    bottom = min(1.0, bottom)
    return [
        "--roi-left", f"{left:.12g}",
        "--roi-top", f"{top:.12g}",
        "--roi-right", f"{right:.12g}",
        "--roi-bottom", f"{bottom:.12g}",
    ]


def extract_burned_subtitles(
        video_path: str, cache_folder: str, *, sample_ms: int = 250,
        normalized_rect: Optional[Sequence[float]] = None,
        progress_callback: Optional[Callable[[int], None]] = None,
        cancel_callback: Optional[Callable[[], bool]] = None) -> List:
    """Extract Chinese burned-in dialogue using the installed subtitle OCR engine."""
    engine = find_subtitle_remover_engine(ROOT_DIR)
    if not engine:
        logger.info("未找到硬字幕 OCR 引擎，保留 ASR 结果")
        return []

    detector_model = engine.root / "backend" / "models" / "V5" / "ch_det_fast" / "inference.json"
    worker = Path(ROOT_DIR) / "scripts" / "subtitle_ocr_worker.py"
    if not detector_model.is_file() or not worker.is_file():
        logger.info("硬字幕 OCR 检测模型或 worker 不完整，保留 ASR 结果")
        return []

    cache_path = Path(cache_folder)
    cache_path.mkdir(parents=True, exist_ok=True)
    output_file = cache_path / "burned-subtitle-ocr.json"
    command = [
        str(engine.python),
        str(worker),
        "--video", str(Path(video_path).resolve()),
        "--output", str(output_file.resolve()),
        "--engine-root", str(engine.root),
        "--sample-ms", str(sample_ms),
    ]
    command.extend(_roi_command_args(normalized_rect))
    env = _worker_environment(engine.python)
    if normalized_rect:
        logger.info(f"开始硬字幕 OCR（使用框选区域）：{video_path}")
    else:
        logger.info(f"开始硬字幕 OCR（使用默认区域）：{video_path}")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=_creation_flags(),
    )
    logs = []
    if process.stdout:
        for line in process.stdout:
            if cancel_callback and cancel_callback():
                process.terminate()
                process.wait(timeout=10)
                logger.info("硬字幕 OCR 已取消")
                return []
            line = line.rstrip()
            if line.startswith(EVENT_PREFIX):
                try:
                    event = json.loads(line[len(EVENT_PREFIX):])
                    if event.get("type") == "progress" and progress_callback:
                        progress_callback(int(event.get("value", 0)))
                    elif event.get("type") == "log":
                        logger.info(str(event.get("message", "")))
                except (TypeError, ValueError, json.JSONDecodeError):
                    logger.debug(line)
            elif line:
                logs.append(line)
                logger.debug(f"[subtitle-ocr] {line}")
    return_code = process.wait()
    if return_code != 0:
        logger.error(
            "硬字幕 OCR 子进程失败，保留 ASR 结果："
            + "\n".join(logs[-12:])
        )
        return []
    try:
        payload = json.loads(output_file.read_text(encoding="utf-8"))
        observations = payload.get("observations", [])
        actual_sample_ms = int(payload.get("sample_ms", sample_ms))
        subtitles = build_ocr_subtitles(observations, sample_ms=actual_sample_ms)
        logger.info(
            f"硬字幕 OCR 完成：{len(observations)} 个采样结果，"
            f"合并为 {len(subtitles)} 条字幕"
        )
        return subtitles
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        logger.exception(f"读取硬字幕 OCR 结果失败，保留 ASR 结果：{error}", exc_info=True)
        return []
