from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .config import Settings
from .cost import CostEstimator
from .models import CostQuote, JobRequest, ProviderHealth


class AdmissionRejected(RuntimeError):
    """Raised before billing for an invalid, unsupported, or over-budget job."""


class ProviderUnavailable(AdmissionRejected):
    """Raised when no mandatory provider path can currently execute the job."""


@dataclass(frozen=True)
class P95Profile:
    """Initial p95 estimates for a five-minute input on warm workers.

    These numbers guide optimization and user-facing estimates. They never turn
    the 300-second goal into a cancellation deadline.
    """

    media_prepare: float = 105.0
    asr: float = 38.0
    cloud_ocr: float = 55.0
    speaker_fusion: float = 92.0
    translation: float = 14.0
    azure_tts: float = 66.0
    assembly: float = 52.0
    fixed_overhead: float = 8.0

    def predicted_seconds(self, request: JobRequest) -> float:
        duration_factor = max(0.18, request.probe.duration_seconds / 300.0)
        pixels = request.probe.width * request.probe.height
        pixel_factor = max(1.0, min(2.0, pixels / float(1920 * 1080)))
        fps_factor = max(1.0, request.probe.fps / 30.0)
        visual_factor = duration_factor * max(pixel_factor**0.35, fps_factor**0.25)
        codec_penalty = 12.0 if request.probe.codec.casefold() in {"av1", "vp9"} else 0.0
        hard_subtitle_penalty = 18.0 if request.subtitle_mode == "hard" else 0.0

        recognition_paths = []
        if request.subtitle_recognition_mode in {"asr", "hybrid"}:
            recognition_paths.append(self.asr * duration_factor)
        if request.subtitle_recognition_mode in {"ocr", "hybrid"}:
            recognition_paths.append(self.cloud_ocr * visual_factor)
        recognition = max(recognition_paths, default=0.0)
        media = self.media_prepare * visual_factor
        language_path = (
            recognition
            + max(self.speaker_fusion * visual_factor, self.translation * duration_factor)
            + self.azure_tts * duration_factor
            + self.assembly * visual_factor
        )
        return (
            self.fixed_overhead + codec_penalty + hard_subtitle_penalty + max(media, language_path)
        )


class AdmissionController:
    SUPPORTED_CODECS = {"h264", "hevc", "h265", "av1", "vp9"}

    def __init__(
        self,
        settings: Settings,
        p95: P95Profile | None = None,
        cost_estimator: CostEstimator | None = None,
    ) -> None:
        self.settings = settings
        self.p95 = p95 or P95Profile()
        self.cost_estimator = cost_estimator or CostEstimator(
            asr_per_second=settings.cost_asr_per_second,
            ocr_per_minute=settings.cost_ocr_per_minute,
            translation_per_character=settings.cost_translation_per_character,
            azure_tts_per_character=settings.cost_azure_tts_per_character,
            gpu_per_second=settings.cost_gpu_per_second,
            render_per_minute=settings.cost_render_per_minute,
            separation_per_minute=settings.cost_separation_per_minute,
        )
        self._slots = asyncio.Semaphore(settings.max_inflight_jobs)
        self._inflight = 0
        self._queued = 0
        self._capacity_lock = asyncio.Lock()

    def quote(self, request: JobRequest) -> tuple[float, CostQuote]:
        probe = request.probe
        if probe.duration_seconds > self.settings.max_video_seconds:
            raise AdmissionRejected("video duration exceeds the five-minute product limit")
        if probe.input_bytes > self.settings.max_input_bytes:
            raise AdmissionRejected("input is too large for the configured processing profile")
        if probe.codec.casefold() not in self.SUPPORTED_CODECS:
            raise AdmissionRejected(f"unsupported input codec: {probe.codec}")

        predicted = self.p95.predicted_seconds(request)
        cost = self.cost_estimator.quote(request)
        if cost.total_cny > request.max_cost_cny:
            raise AdmissionRejected(
                f"estimated cost {cost.total_cny:.2f} CNY exceeds budget "
                f"{request.max_cost_cny:.2f} CNY"
            )
        return predicted, cost

    def estimate(self, request: JobRequest) -> float:
        return self.quote(request)[0]

    def validate_provider_health(
        self, request: JobRequest, health: list[ProviderHealth]
    ) -> list[str]:
        mandatory = {"media", "deepseek", "azure_tts"}
        if request.subtitle_recognition_mode in {"asr", "hybrid"}:
            mandatory.add("asr")
        if request.subtitle_recognition_mode in {"ocr", "hybrid"}:
            mandatory.add("caption")
        if request.multi_speaker:
            mandatory.add("speaker")
        indexed = {item.name: item for item in health}
        unavailable = [
            name for name in sorted(mandatory) if name not in indexed or not indexed[name].healthy
        ]
        if unavailable:
            raise ProviderUnavailable(
                "mandatory providers are unavailable: " + ", ".join(unavailable)
            )

        cold = [name for name in sorted(mandatory) if not indexed[name].warm]
        return (
            [
                "some workers are cold; the job remains accepted but may miss the "
                f"{self.settings.target_processing_seconds}s target: " + ", ".join(cold)
            ]
            if cold
            else []
        )

    async def acquire(self) -> float:
        """Wait for a processing slot instead of rejecting a valid job."""

        queued_at = time.monotonic()
        async with self._capacity_lock:
            self._queued += 1
        try:
            await self._slots.acquire()
        except BaseException:
            async with self._capacity_lock:
                self._queued = max(0, self._queued - 1)
            raise
        wait_seconds = time.monotonic() - queued_at
        async with self._capacity_lock:
            self._queued = max(0, self._queued - 1)
            self._inflight += 1
        return wait_seconds

    async def release(self) -> None:
        async with self._capacity_lock:
            self._inflight = max(0, self._inflight - 1)
        self._slots.release()

    async def capacity(self) -> dict[str, int]:
        async with self._capacity_lock:
            return {
                "inflight": self._inflight,
                "queued": self._queued,
                "max_inflight": self.settings.max_inflight_jobs,
                "available": max(0, self.settings.max_inflight_jobs - self._inflight),
            }


class TargetTimer:
    """Tracks the five-minute objective without cancelling work when it is missed."""

    def __init__(self, target_seconds: float, *, started: float | None = None) -> None:
        self.started = started if started is not None else time.monotonic()
        self.target_seconds = target_seconds

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @property
    def exceeded(self) -> bool:
        return self.elapsed > self.target_seconds
