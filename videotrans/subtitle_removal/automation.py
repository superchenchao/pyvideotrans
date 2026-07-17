from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

from videotrans.configure.config import ROOT_DIR

from .engine import find_subtitle_remover_engine
from .strategy import AUTO_BACKEND, PROPAINTER_BACKEND, STTN_BACKEND, requested_backend


EVENT_PREFIX = "PYVT_EVENT "
_REMOVAL_LOCK = threading.Lock()


def _write_backend_record(output_file: str, backend: str) -> None:
    try:
        Path(f"{output_file}.inpaint.json").write_text(
            json.dumps({"backend": backend}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def normalize_rect(rect: Sequence[int], width: int, height: int) -> list[float]:
    if width <= 0 or height <= 0 or len(rect) != 4:
        raise ValueError("Invalid video size or subtitle removal rectangle")
    x, y, rect_width, rect_height = (int(value) for value in rect)
    if rect_width <= 0 or rect_height <= 0:
        raise ValueError("Subtitle removal rectangle must not be empty")
    return [
        max(0.0, min(1.0, x / width)),
        max(0.0, min(1.0, y / height)),
        max(0.0, min(1.0, rect_width / width)),
        max(0.0, min(1.0, rect_height / height)),
    ]


def scale_normalized_rect(
        normalized_rect: Sequence[float], width: int, height: int, *,
        reference_aspect_ratio: float = 0.0,
        aspect_tolerance: float = 0.02) -> Tuple[int, int, int, int]:
    if width <= 0 or height <= 0 or len(normalized_rect) != 4:
        raise ValueError("Invalid video size or normalized subtitle removal rectangle")
    current_ratio = width / height
    if reference_aspect_ratio > 0:
        relative_difference = abs(current_ratio - reference_aspect_ratio) / reference_aspect_ratio
        if relative_difference > aspect_tolerance:
            raise ValueError(
                f"Video aspect ratio {current_ratio:.4f} does not match "
                f"the selected video's ratio {reference_aspect_ratio:.4f}"
            )

    x_ratio, y_ratio, width_ratio, height_ratio = (
        float(value) for value in normalized_rect
    )
    x = max(0, min(width - 1, round(x_ratio * width)))
    y = max(0, min(height - 1, round(y_ratio * height)))
    right = max(x + 1, min(width, round((x_ratio + width_ratio) * width)))
    bottom = max(y + 1, min(height, round((y_ratio + height_ratio) * height)))
    return x, y, right - x, bottom - y


def remove_burned_subtitles(
        *, input_file: str, output_file: str,
        rect: Sequence[int], duration_ms: int,
        progress_callback: Optional[Callable[[int], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        cancel_callback: Optional[Callable[[], bool]] = None,
        inpaint_engine: Optional[str] = None) -> str:
    engine = find_subtitle_remover_engine(ROOT_DIR)
    if not engine:
        raise RuntimeError("Subtitle removal engine is not installed")
    selected_request = inpaint_engine or requested_backend()
    required_backend = (
        selected_request if selected_request != AUTO_BACKEND else STTN_BACKEND
    )
    missing = engine.missing_files("auto", inpaint_backend=required_backend)
    if missing:
        raise RuntimeError(
            "Subtitle removal model is incomplete: "
            + ", ".join(str(path) for path in missing)
        )

    worker_script = Path(ROOT_DIR) / "scripts" / "subtitle_remove_worker.py"
    command = [
        str(engine.python),
        "-u",
        str(worker_script),
        "--project-root", str(ROOT_DIR),
        "--engine-root", str(engine.root),
        "--input", str(Path(input_file).resolve()),
        "--output", str(Path(output_file).resolve()),
        "--mode", "auto",
        "--start-ms", "0",
        "--end-ms", str(max(1, int(duration_ms))),
        "--rect", *(str(int(value)) for value in rect),
        "--inpaint-engine", selected_request,
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    output_tail = []
    worker_selected_backend = ""
    failure_message = ""
    Path(output_file).parent.mkdir(parents=True, exist_ok=True)

    with _REMOVAL_LOCK:
        process = subprocess.Popen(
            command,
            cwd=engine.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        try:
            assert process.stdout is not None
            for raw_line in process.stdout:
                if cancel_callback and cancel_callback():
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=creationflags,
                            check=False,
                        )
                    else:
                        process.terminate()
                    process.wait(timeout=10)
                    raise RuntimeError("Subtitle removal cancelled")

                line = raw_line.strip()
                if not line:
                    continue
                output_tail.append(line)
                output_tail = output_tail[-20:]
                event_position = line.rfind(EVENT_PREFIX)
                if event_position < 0:
                    continue
                try:
                    event = json.loads(line[event_position + len(EVENT_PREFIX):])
                except json.JSONDecodeError:
                    continue
                event_type = event.get("type")
                if event_type == "progress" and progress_callback:
                    progress_callback(max(0, min(100, int(event.get("value", 0)))))
                elif event_type in {"log", "error"} and log_callback:
                    log_callback(str(event.get("message", "")))
                elif event_type == "backend_selected":
                    worker_selected_backend = str(event.get("backend", ""))
                    if log_callback:
                        log_callback(
                            "Subtitle inpaint backend: "
                            f"{worker_selected_backend} on {event.get('gpu', 'unknown')} "
                            f"({event.get('reason', '')})"
                        )

            return_code = process.wait()
            if return_code != 0:
                failure_message = (
                    "Subtitle removal failed\n" + "\n".join(output_tail[-8:])
                )
            elif not Path(output_file).is_file():
                failure_message = "Subtitle remover did not create the output video"
            else:
                _write_backend_record(
                    output_file,
                    worker_selected_backend or selected_request,
                )
                return str(Path(output_file).resolve())
        finally:
            if process.poll() is None:
                process.terminate()

    if (
            selected_request == AUTO_BACKEND
            and worker_selected_backend == PROPAINTER_BACKEND):
        if log_callback:
            log_callback(
                "ProPainter failed or produced an invalid repair; retrying with STTN"
            )
        return remove_burned_subtitles(
            input_file=input_file,
            output_file=output_file,
            rect=rect,
            duration_ms=duration_ms,
            progress_callback=progress_callback,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            inpaint_engine=STTN_BACKEND,
        )
    raise RuntimeError(failure_message or "Subtitle removal failed")
