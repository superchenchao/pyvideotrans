from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from fractions import Fraction

from .subtitle_models import SubtitleOutputValidation, SubtitleRemovalRequest


class MediaProbeError(RuntimeError):
    """Raised when a generated media file cannot be probed."""


@dataclass(frozen=True)
class MediaProbeResult:
    duration_seconds: float | None
    size_bytes: int
    width: int | None
    height: int | None
    fps: float | None
    codec: str
    has_audio: bool


def _fps(value: str) -> float | None:
    text = str(value or "").strip()
    if not text or text in {"0/0", "N/A"}:
        return None
    try:
        return float(Fraction(text))
    except (ValueError, ZeroDivisionError):
        try:
            return float(text)
        except ValueError:
            return None


async def probe_media(
    url: str,
    *,
    ffprobe_binary: str = "ffprobe",
    timeout_seconds: float = 90.0,
) -> MediaProbeResult:
    process = await asyncio.create_subprocess_exec(
        ffprobe_binary,
        "-v",
        "error",
        "-show_entries",
        "format=duration,size:stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate",
        "-of",
        "json",
        url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=max(1.0, timeout_seconds),
        )
    except TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise MediaProbeError(
            f"ffprobe exceeded {timeout_seconds:.0f}s for generated media"
        ) from exc
    if process.returncode != 0:
        raise MediaProbeError(stderr.decode("utf-8", errors="replace")[:2000])
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise MediaProbeError("ffprobe returned invalid JSON") from exc

    format_row = payload.get("format") or {}
    streams = payload.get("streams") or []
    video = next(
        (row for row in streams if str(row.get("codec_type", "")) == "video"),
        {},
    )
    has_audio = any(str(row.get("codec_type", "")) == "audio" for row in streams)
    duration = format_row.get("duration")
    try:
        duration_seconds = float(duration) if duration not in {None, "N/A"} else None
    except (TypeError, ValueError):
        duration_seconds = None
    try:
        size_bytes = int(format_row.get("size", 0) or 0)
    except (TypeError, ValueError):
        size_bytes = 0
    try:
        width = int(video.get("width")) if video.get("width") is not None else None
        height = int(video.get("height")) if video.get("height") is not None else None
    except (TypeError, ValueError):
        width, height = None, None
    fps = _fps(video.get("avg_frame_rate") or video.get("r_frame_rate") or "")
    return MediaProbeResult(
        duration_seconds=duration_seconds,
        size_bytes=size_bytes,
        width=width,
        height=height,
        fps=fps,
        codec=str(video.get("codec_name", "") or ""),
        has_audio=has_audio,
    )


def validate_probe(
    request: SubtitleRemovalRequest,
    result: MediaProbeResult,
    *,
    duration_tolerance_seconds: float = 1.5,
    fps_tolerance: float = 1.0,
) -> SubtitleOutputValidation:
    warnings: list[str] = []
    valid = result.size_bytes > 0
    if result.size_bytes <= 0:
        warnings.append("generated video is empty")
    if result.width is None or result.height is None:
        valid = False
        warnings.append("generated video has no video stream")
    if not result.has_audio:
        warnings.append("generated video has no audio stream")

    if request.expected_duration_seconds is not None:
        if result.duration_seconds is None:
            valid = False
            warnings.append("generated video duration is unavailable")
        elif abs(result.duration_seconds - request.expected_duration_seconds) > max(
            duration_tolerance_seconds,
            request.expected_duration_seconds * 0.01,
        ):
            valid = False
            warnings.append(
                "generated duration differs from the source: "
                f"expected {request.expected_duration_seconds:.3f}s, "
                f"got {result.duration_seconds:.3f}s"
            )
    if request.expected_width is not None and result.width != request.expected_width:
        valid = False
        warnings.append(
            f"generated width differs: expected {request.expected_width}, got {result.width}"
        )
    if request.expected_height is not None and result.height != request.expected_height:
        valid = False
        warnings.append(
            f"generated height differs: expected {request.expected_height}, got {result.height}"
        )
    if (
        request.expected_fps is not None
        and result.fps is not None
        and abs(result.fps - request.expected_fps) > fps_tolerance
    ):
        valid = False
        warnings.append(
            f"generated FPS differs: expected {request.expected_fps:.3f}, got {result.fps:.3f}"
        )
    return SubtitleOutputValidation(
        valid=valid,
        size_bytes=result.size_bytes,
        duration_seconds=result.duration_seconds,
        width=result.width,
        height=result.height,
        fps=result.fps,
        codec=result.codec,
        warnings=warnings,
    )
