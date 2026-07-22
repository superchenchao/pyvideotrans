from __future__ import annotations

from .models import CostQuote, JobRequest


class CostEstimator:
    """Conservative admission quote for one accepted job.

    Prices are defaults derived from the deployment assumptions and must remain
    configurable in production billing. This quote is a guardrail, not an invoice.
    """

    def __init__(
        self,
        *,
        asr_per_second: float = 0.00022,
        translation_per_character: float = 0.000003,
        azure_tts_per_character: float = 0.0000954,
        gpu_per_second: float = 0.0062,
        render_per_minute: float = 0.0651,
        separation_per_minute: float = 0.10,
        subtitle_removal_per_minute: float = 0.40,
    ) -> None:
        self.asr_per_second = asr_per_second
        self.translation_per_character = translation_per_character
        self.azure_tts_per_character = azure_tts_per_character
        self.gpu_per_second = gpu_per_second
        self.render_per_minute = render_per_minute
        self.separation_per_minute = separation_per_minute
        self.subtitle_removal_per_minute = subtitle_removal_per_minute

    def quote(self, request: JobRequest) -> CostQuote:
        duration = request.probe.duration_seconds
        minutes = duration / 60.0
        speech_seconds = duration * 0.72
        characters = request.estimated_tts_characters
        if characters is None:
            characters = max(100, round(speech_seconds * 8.0))

        # Translation is tiny relative to video AI and is deliberately padded.
        translation = max(0.01, characters * self.translation_per_character)
        gpu_active_seconds = min(90.0, 12.0 + duration * 0.16)
        breakdown = {
            "asr": speech_seconds * self.asr_per_second,
            "translation": translation,
            "azure_tts": characters * self.azure_tts_per_character,
            "multimodal_gpu": gpu_active_seconds * self.gpu_per_second,
            "render": minutes * self.render_per_minute,
            "background_separation": (
                minutes * self.separation_per_minute if request.separate_background else 0.0
            ),
            "subtitle_removal": (
                minutes * self.subtitle_removal_per_minute
                if request.remove_burned_subtitles
                else 0.0
            ),
        }
        rounded = {key: round(value, 4) for key, value in breakdown.items()}
        return CostQuote(total_cny=round(sum(breakdown.values()), 4), breakdown=rounded)
