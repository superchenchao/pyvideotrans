from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, TypeVar

from .config import Settings
from .cost import CostEstimator
from .models import CostQuote, JobRequest, ProviderHealth

T = TypeVar("T")


class AdmissionRejected(RuntimeError):
    """Raised before billing when the five-minute SLA cannot be accepted."""


class CapacityUnavailable(AdmissionRejected):
    """Accepted jobs are never allowed to wait in an internal queue."""


class ProviderUnavailable(AdmissionRejected):
    """A mandatory provider is unhealthy or its model is not pre-warmed."""


@dataclass(frozen=True)
class P95Profile:
    """P95 stage times for a five-minute input on pre-warmed workers."""

    media_prepare: float = 105.0
    asr: float = 38.0
    speaker_fusion: float = 92.0
    translation: float = 14.0
    azure_tts: float = 66.0
    assembly: float = 52.0
    fixed_overhead: float = 8.0

    def predicted_seconds(self, duration_seconds: float) -> float:
        factor = max(0.18, duration_seconds / 300.0)
        media = self.media_prepare * factor
        language_path = (
            self.asr * factor
            + max(self.speaker_fusion * factor, self.translation * factor)
            + self.azure_tts * factor
            + self.assembly * factor
        )
        return self.fixed_overhead + max(media, language_path)


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
            translation_per_character=settings.cost_translation_per_character,
            azure_tts_per_character=settings.cost_azure_tts_per_character,
            gpu_per_second=settings.cost_gpu_per_second,
            render_per_minute=settings.cost_render_per_minute,
            separation_per_minute=settings.cost_separation_per_minute,
            subtitle_removal_per_minute=(
                settings.cost_subtitle_removal_per_minute
            ),
        )
        self._inflight = 0
        self._capacity_lock = asyncio.Lock()

    def quote(self, request: JobRequest) -> tuple[float, CostQuote]:
        probe = request.probe
        if probe.duration_seconds > self.settings.max_video_seconds:
            raise AdmissionRejected("video duration exceeds the five-minute product limit")
        if probe.input_bytes > self.settings.max_input_bytes:
            raise AdmissionRejected(
                "input is too large for the five-minute upload/processing profile"
            )
        if probe.codec.casefold() not in self.SUPPORTED_CODECS:
            raise AdmissionRejected(f"unsupported codec for SLA mode: {probe.codec}")
        if probe.width * probe.height > 1920 * 1080:
            raise AdmissionRejected("strict SLA mode accepts at most 1080p input")
        if request.subtitle_mode == "hard" and probe.fps > 30:
            raise AdmissionRejected("hard-subtitle SLA mode accepts at most 30 FPS")

        predicted = self.p95.predicted_seconds(probe.duration_seconds)
        allowed = self.settings.hard_sla_seconds - self.settings.sla_reserve_seconds
        if request.strict_sla and predicted > allowed:
            raise AdmissionRejected(
                f"predicted p95 {predicted:.1f}s exceeds admission budget {allowed}s"
            )

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
    ) -> None:
        mandatory = {"media", "asr", "deepseek", "azure_tts"}
        if request.multi_speaker:
            mandatory.add("speaker")
        unhealthy = [
            item for item in health if item.name in mandatory and not item.healthy
        ]
        if unhealthy:
            names = ", ".join(item.name for item in unhealthy)
            raise ProviderUnavailable(f"mandatory providers are unhealthy: {names}")
        if request.strict_sla:
            warm_required = {"media", "asr"}
            if request.multi_speaker:
                warm_required.add("speaker")
            cold = [
                item.name
                for item in health
                if item.name in warm_required and not item.warm
            ]
            if cold:
                raise ProviderUnavailable(
                    "strict SLA requires pre-warmed workers: " + ", ".join(cold)
                )

    async def acquire_nowait(self) -> None:
        async with self._capacity_lock:
            if self._inflight >= self.settings.max_inflight_jobs:
                raise CapacityUnavailable("all pre-warmed SLA slots are busy; retry later")
            self._inflight += 1

    async def release(self) -> None:
        async with self._capacity_lock:
            self._inflight = max(0, self._inflight - 1)

    async def capacity(self) -> dict[str, int]:
        async with self._capacity_lock:
            return {
                "inflight": self._inflight,
                "max_inflight": self.settings.max_inflight_jobs,
                "available": max(0, self.settings.max_inflight_jobs - self._inflight),
            }


class Deadline:
    def __init__(self, hard_seconds: float) -> None:
        self.started = time.monotonic()
        self.hard_at = self.started + hard_seconds

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @property
    def remaining(self) -> float:
        return max(0.0, self.hard_at - time.monotonic())

    async def run_before(self, offset_seconds: float, awaitable: Awaitable[T]) -> T:
        stage_deadline = min(self.hard_at, self.started + offset_seconds)
        timeout = stage_deadline - time.monotonic()
        if timeout <= 0:
            raise TimeoutError("stage deadline already expired")
        async with asyncio.timeout(timeout):
            return await awaitable
