from __future__ import annotations

from .models import CostQuote, JobRequest


class CostEstimator:
    """Conservative quote for the full standalone cloud workflow.

    Prices are configurable guardrails, not a replacement for provider billing
    records. Cloud OCR is charged only when OCR or hybrid recognition is selected.
    """

    def __init__(
        self,
        *,
        asr_per_second: float = 0.00022,
        ocr_per_minute: float = 0.10,
        translation_per_character: float = 0.000003,
        azure_tts_per_character: float = 0.0000954,
        gpu_per_second: float = 0.0062,
        render_per_minute: float = 0.0651,
        separation_per_minute: float = 0.10,
    ) -> None:
        self.asr_per_second = asr_per_second
        self.ocr_per_minute = ocr_per_minute
        self.translation_per_character = translation_per_character
        self.azure_tts_per_character = azure_tts_per_character
        self.gpu_per_second = gpu_per_second
        self.render_per_minute = render_per_minute
        self.separation_per_minute = separation_per_minute

    def quote(self, request: JobRequest) -> CostQuote:
        duration = request.probe.duration_seconds
        minutes = duration / 60.0
        speech_seconds = duration * 0.72
        characters = request.estimated_tts_characters
        if characters is None:
            characters = max(100, round(speech_seconds * 8.0))

        translation = max(0.01, characters * self.translation_per_character)
        gpu_active_seconds = min(90.0, 12.0 + duration * 0.16)
        use_asr = request.subtitle_recognition_mode in {"asr", "hybrid"}
        use_ocr = request.subtitle_recognition_mode in {"ocr", "hybrid"}
        breakdown = {
            "asr": speech_seconds * self.asr_per_second if use_asr else 0.0,
            "cloud_ocr": minutes * self.ocr_per_minute if use_ocr else 0.0,
            "deepseek_translation": translation,
            "azure_tts": characters * self.azure_tts_per_character,
            "multimodal_gpu": gpu_active_seconds * self.gpu_per_second,
            "render": minutes * self.render_per_minute,
            "background_separation": (
                minutes * self.separation_per_minute if request.separate_background else 0.0
            ),
        }
        rounded = {key: round(value, 4) for key, value in breakdown.items()}
        return CostQuote(total_cny=round(sum(breakdown.values()), 4), breakdown=rounded)
