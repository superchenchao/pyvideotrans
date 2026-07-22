from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator


class JobState(StrEnum):
    ACCEPTED = "accepted"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    DEGRADED = "degraded"
    REJECTED = "rejected"
    FAILED = "failed"


class VideoProbe(BaseModel):
    duration_seconds: Annotated[float, Field(gt=0, le=300)]
    input_bytes: Annotated[int, Field(gt=0)]
    codec: str = "h264"
    width: Annotated[int, Field(gt=0, le=3840)] = 1920
    height: Annotated[int, Field(gt=0, le=3840)] = 1080
    fps: Annotated[float, Field(gt=0, le=60)] = 25.0


class OCRRegion(BaseModel):
    """Normalized subtitle area in the original, not-yet-cleaned video."""

    x: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    y: Annotated[float, Field(ge=0.0, le=1.0)] = 0.65
    width: Annotated[float, Field(gt=0.0, le=1.0)] = 1.0
    height: Annotated[float, Field(gt=0.0, le=1.0)] = 0.35

    @model_validator(mode="after")
    def validate_bounds(self) -> OCRRegion:
        if self.x + self.width > 1.0 or self.y + self.height > 1.0:
            raise ValueError("OCR region must stay inside normalized video bounds")
        return self


class JobRequest(BaseModel):
    # input_url is the uploaded original video and remains the OCR source.
    # clean_video_url, when present, is the subtitle-removal output used for visual
    # speaker analysis and final assembly. All recognition calls run in cloud workers.
    input_url: HttpUrl
    clean_video_url: HttpUrl | None = None
    source_audio_url: HttpUrl | None = None
    probe: VideoProbe
    source_language: str = "zh-CN"
    target_language: str
    subtitle_recognition_mode: Literal["hybrid", "asr", "ocr"] = "hybrid"
    ocr_region: OCRRegion | None = None
    ocr_fps: Annotated[int, Field(ge=2, le=10)] = 5
    ocr_language: Literal["ch", "en", "ch_ml"] = "ch_ml"
    ocr_track: Literal["main", "all"] = "main"
    ocr_required: bool = False
    translation_engine: Literal["deepseek"] = "deepseek"
    target_voice: str = ""
    character_voices: dict[str, str] = Field(default_factory=dict)
    character_names: dict[str, str] = Field(default_factory=dict)
    glossary: dict[str, str] = Field(default_factory=dict)
    subtitle_mode: Literal["soft", "hard", "none"] = "soft"
    separate_background: bool = True
    multi_speaker: bool = True
    optimize_for_target: bool = True
    max_cost_cny: Annotated[float, Field(gt=0, le=100)] = 5.0
    estimated_tts_characters: Annotated[int, Field(ge=0)] | None = None
    expected_speakers: Annotated[int, Field(ge=1, le=20)] | None = None

    @model_validator(mode="after")
    def validate_languages(self) -> JobRequest:
        if self.source_language.casefold() == self.target_language.casefold():
            raise ValueError("target_language must differ from source_language")
        return self


class WordTiming(BaseModel):
    start_ms: int
    end_ms: int
    text: str
    punctuation: str = ""

    @model_validator(mode="after")
    def validate_time_range(self) -> WordTiming:
        if self.end_ms < self.start_ms:
            raise ValueError("word end_ms must be greater than or equal to start_ms")
        return self


class SubtitleLine(BaseModel):
    line_id: int
    start_ms: int
    end_ms: int
    text: str
    speaker_id: str | None = None
    words: list[WordTiming] = Field(default_factory=list)
    source: str = ""
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    metadata: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_time_range(self) -> SubtitleLine:
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        return self


class Transcript(BaseModel):
    language: str
    lines: list[SubtitleLine]
    provider: str = ""
    task_id: str = ""
    usage_seconds: float | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


class CandidateScore(BaseModel):
    character_id: str
    score: Annotated[float, Field(ge=0.0, le=1.0)]


class LineEvidence(BaseModel):
    line_id: int
    audio: list[CandidateScore] = Field(default_factory=list)
    visual: list[CandidateScore] = Field(default_factory=list)
    text: list[CandidateScore] = Field(default_factory=list)
    offscreen: bool = False
    overlap_speech: bool = False
    av_sync_confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0


class SpeakerDecision(BaseModel):
    line_id: int
    character_id: str
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    needs_review: bool = False


class MediaArtifacts(BaseModel):
    video_url: str
    background_url: str | None = None
    vocal_url: str | None = None
    source_audio_url: str | None = None
    provider: str = ""
    task_ids: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, object] = Field(default_factory=dict)
    degraded_features: list[str] = Field(default_factory=list)


class DubbingClip(BaseModel):
    line_id: int
    character_id: str
    audio_url: str
    duration_ms: int | None = None
    target_duration_ms: int | None = None
    rate_percent: int = 0
    timing_overflow_ms: int = 0
    within_target: bool = True
    output_format: str = ""
    metadata: dict[str, object] = Field(default_factory=dict)


class DubbingArtifact(BaseModel):
    clips: list[DubbingClip]


class OutputArtifact(BaseModel):
    video_url: str
    subtitle_url: str | None = None
    provider: str = ""
    task_id: str = ""
    metadata: dict[str, object] = Field(default_factory=dict)


class AdmissionResult(BaseModel):
    accepted: bool
    predicted_seconds: float
    target_seconds: int
    likely_within_target: bool
    estimated_cost_cny: float = 0.0
    cost_breakdown: dict[str, float] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    reason: str = ""


class ProviderHealth(BaseModel):
    name: str
    healthy: bool
    warm: bool = True
    detail: str = ""
    latency_ms: float = 0.0


class CostQuote(BaseModel):
    total_cny: float
    breakdown: dict[str, float] = Field(default_factory=dict)


class JobEvent(BaseModel):
    event_type: str
    stage: str = ""
    progress: int | None = None
    message: str = ""
    data: dict[str, object] = Field(default_factory=dict)


class StageMetric(BaseModel):
    stage: str
    elapsed_seconds: float
    degraded: bool = False
    detail: str = ""


class JobRecord(BaseModel):
    job_id: str
    state: JobState
    accepted_at_monotonic: float
    processing_started_at_monotonic: float | None = None
    target_seconds: int
    request: JobRequest
    predicted_seconds: float
    likely_within_target: bool
    estimated_cost_cny: float = 0.0
    cost_breakdown: dict[str, float] = Field(default_factory=dict)
    queue_wait_seconds: float = 0.0
    elapsed_seconds: float = 0.0
    target_exceeded: bool = False
    progress: int = 0
    current_stage: str = "accepted"
    result: OutputArtifact | None = None
    decisions: list[SpeakerDecision] = Field(default_factory=list)
    metrics: list[StageMetric] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: str = ""
