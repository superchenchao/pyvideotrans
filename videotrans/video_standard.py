from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from collections import deque
from typing import Callable, Sequence


FINAL_FPS = 25
FINAL_VIDEO_BITRATE = "3000k"
FINAL_VIDEO_BUFSIZE = "6000k"
FINAL_AUDIO_BITRATE = "192k"
FINAL_AUDIO_SAMPLE_RATE = 44100
LANDSCAPE_SIZE = (1920, 1080)
PORTRAIT_SIZE = (1080, 1920)

# This intermediate is intentionally constant-quality and much higher quality
# than the delivery file. The 3000 kbps cap is applied only on final export.
WORK_CRF = 14
WORK_PRESET = "medium"
WORK_PROFILE_VERSION = 1

# Cloud subtitle-removal services receive a smaller, video-only transport
# master.  It is generated directly from the source video so the upload path
# does not add an unnecessary CRF intermediate generation first.
API_WORK_VIDEO_BITRATE = "6000k"
API_WORK_VIDEO_BUFSIZE = "12000k"
API_WORK_PRESET = "medium"
API_WORK_PROFILE_VERSION = 1


class WorkVideoCancelled(RuntimeError):
    pass


def fixed_dimensions(source_width: int, source_height: int) -> tuple[int, int]:
    if source_width <= 0 or source_height <= 0:
        raise ValueError("Source video dimensions must be positive")
    return PORTRAIT_SIZE if source_height > source_width else LANDSCAPE_SIZE


def display_dimensions(
        encoded_width: int, encoded_height: int, rotation: int | float = 0
) -> tuple[int, int]:
    try:
        normalized_rotation = round(float(rotation)) % 360
    except (TypeError, ValueError):
        normalized_rotation = 0
    if normalized_rotation in {90, 270}:
        return encoded_height, encoded_width
    return encoded_width, encoded_height


def map_normalized_rect_to_canvas(
        normalized_rect: Sequence[float], *,
        source_width: int, source_height: int,
        target_width: int, target_height: int,
        reference_aspect_ratio: float = 0.0,
        aspect_tolerance: float = 0.02) -> list[float]:
    if (
            len(normalized_rect) != 4
            or source_width <= 0 or source_height <= 0
            or target_width <= 0 or target_height <= 0
    ):
        raise ValueError("Invalid source, target, or subtitle rectangle")
    current_ratio = source_width / source_height
    if reference_aspect_ratio > 0:
        difference = abs(current_ratio - reference_aspect_ratio) / reference_aspect_ratio
        if difference > aspect_tolerance:
            raise ValueError(
                f"Video aspect ratio {current_ratio:.4f} does not match "
                f"the selected video's ratio {reference_aspect_ratio:.4f}"
            )

    x_ratio, y_ratio, width_ratio, height_ratio = (
        float(value) for value in normalized_rect
    )
    if (
            x_ratio < 0 or y_ratio < 0
            or width_ratio <= 0 or height_ratio <= 0
            or x_ratio + width_ratio > 1.000001
            or y_ratio + height_ratio > 1.000001
    ):
        raise ValueError("Subtitle rectangle is outside the source frame")

    scale = min(target_width / source_width, target_height / source_height)
    scaled_width = source_width * scale
    scaled_height = source_height * scale
    pad_x = (target_width - scaled_width) / 2
    pad_y = (target_height - scaled_height) / 2
    return [
        (pad_x + x_ratio * scaled_width) / target_width,
        (pad_y + y_ratio * scaled_height) / target_height,
        width_ratio * scaled_width / target_width,
        height_ratio * scaled_height / target_height,
    ]


def fixed_visual_filter(target_width: int, target_height: int) -> str:
    return ",".join(
        [
            f"fps={FINAL_FPS}",
            f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease",
            f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:color=black",
            "setsar=1",
            "format=yuv420p",
        ]
    )


def work_profile(target_width: int, target_height: int) -> dict[str, object]:
    return {
        "version": WORK_PROFILE_VERSION,
        "fps": FINAL_FPS,
        "width": target_width,
        "height": target_height,
        "codec": "libx264",
        "rate_control": f"crf-{WORK_CRF}",
        "preset": WORK_PRESET,
        "pixel_format": "yuv420p",
        "audio": "none",
    }


def api_work_profile(target_width: int, target_height: int) -> dict[str, object]:
    return {
        "version": API_WORK_PROFILE_VERSION,
        "purpose": "cloud-subtitle-removal",
        "fps": FINAL_FPS,
        "width": target_width,
        "height": target_height,
        "codec": "libx264",
        "rate_control": f"constrained-{API_WORK_VIDEO_BITRATE}",
        "maxrate": API_WORK_VIDEO_BITRATE,
        "bufsize": API_WORK_VIDEO_BUFSIZE,
        "preset": API_WORK_PRESET,
        "pixel_format": "yuv420p",
        "audio": "none",
    }


def build_work_video_args(
        input_file: str, output_file: str,
        target_width: int, target_height: int) -> list[str]:
    return [
        "-y",
        "-i", str(input_file),
        "-map", "0:v:0",
        "-an",
        "-vf", fixed_visual_filter(target_width, target_height),
        "-c:v", "libx264",
        "-preset", WORK_PRESET,
        "-crf", str(WORK_CRF),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_file),
    ]


def build_api_work_video_args(
        input_file: str, output_file: str,
        target_width: int, target_height: int) -> list[str]:
    return [
        "-y",
        "-i", str(input_file),
        "-map", "0:v:0",
        "-an",
        "-vf", fixed_visual_filter(target_width, target_height),
        "-c:v", "libx264",
        "-preset", API_WORK_PRESET,
        "-b:v", API_WORK_VIDEO_BITRATE,
        "-maxrate", API_WORK_VIDEO_BITRATE,
        "-bufsize", API_WORK_VIDEO_BUFSIZE,
        "-pix_fmt", "yuv420p",
        "-r", str(FINAL_FPS),
        "-fps_mode", "cfr",
        "-movflags", "+faststart",
        str(output_file),
    ]


def final_video_codec_args() -> list[str]:
    # x264 HRD CBR inserts filler where necessary. This is not merely an
    # average bitrate request using -b:v.
    return [
        "-c:v", "libx264",
        "-preset", "medium",
        "-b:v", FINAL_VIDEO_BITRATE,
        "-minrate", FINAL_VIDEO_BITRATE,
        "-maxrate", FINAL_VIDEO_BITRATE,
        "-bufsize", FINAL_VIDEO_BUFSIZE,
        "-x264-params", "nal-hrd=cbr:force-cfr=1",
        "-pix_fmt", "yuv420p",
        "-r", str(FINAL_FPS),
        "-fps_mode", "cfr",
    ]


def final_audio_codec_args() -> list[str]:
    return [
        "-c:a", "aac",
        "-b:a", FINAL_AUDIO_BITRATE,
        "-ar", str(FINAL_AUDIO_SAMPLE_RATE),
        "-ac", "2",
    ]


def _terminate_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )
    else:
        process.terminate()


def run_work_video_ffmpeg(
        args: Sequence[str], *, duration_ms: int,
        cancel_callback: Callable[[], bool] | None = None,
        progress_callback: Callable[[int], None] | None = None) -> None:
    command = [
        "ffmpeg", "-hide_banner", "-nostdin", "-ignore_unknown", "-threads", "0",
        *args[:-1], "-progress", "pipe:1", "-nostats", args[-1],
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creationflags,
    )
    progress_lines: queue.Queue[str] = queue.Queue()
    error_tail: deque[str] = deque(maxlen=30)

    def read_progress() -> None:
        if process.stdout is not None:
            for line in process.stdout:
                progress_lines.put(line.strip())

    def read_errors() -> None:
        if process.stderr is not None:
            for line in process.stderr:
                if line.strip():
                    error_tail.append(line.strip())

    stdout_thread = threading.Thread(target=read_progress, daemon=True)
    stderr_thread = threading.Thread(target=read_errors, daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    last_progress = -1
    try:
        while process.poll() is None:
            if cancel_callback and cancel_callback():
                _terminate_process_tree(process)
                process.wait(timeout=10)
                raise WorkVideoCancelled("25 FPS working video generation cancelled")
            try:
                line = progress_lines.get(timeout=0.2)
            except queue.Empty:
                continue
            if line.startswith("out_time_us=") and duration_ms > 0:
                try:
                    processed_ms = int(line.split("=", 1)[1]) / 1000
                    value = max(0, min(100, round(processed_ms * 100 / duration_ms)))
                except (TypeError, ValueError, ZeroDivisionError):
                    continue
                if progress_callback and value != last_progress:
                    progress_callback(value)
                    last_progress = value
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
        if process.returncode != 0:
            detail = "\n".join(error_tail) or f"ffmpeg exited with code {process.returncode}"
            raise RuntimeError(f"25 FPS working video generation failed:\n{detail}")
        if progress_callback and last_progress != 100:
            progress_callback(100)
    finally:
        if process.poll() is None:
            _terminate_process_tree(process)
