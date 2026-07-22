from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator


class SubtitleProviderName(StrEnum):
    AUTO = "auto"
    ALIYUN = "aliyun"
    CACA = "caca"
    LOCAL = "local"


class SubtitleJobState(StrEnum):
    ACCEPTED = "accepted"
    SUBMITTING = "submitting"
    RUNNING = "running"
    VALIDATING = "validating"
    SUCCEEDED = "succeeded"
    DEGRADED = "degraded"
    FAILED = "failed"
    CANCELED = "canceled"


class NormalizedRegion(BaseModel):
    x: Annotated[float, Field(ge=0, le=1)]
    y: Annotated[float, Field(ge=0, le=1)]
    width: Annotated[float, Field(gt=0, le=1)]
    height: Annotated[float, Field(gt=0, le=1)]

    @model_validator(mode="after")
    def validate_bounds(self) -> NormalizedRegion:
        if self.x + self.width > 1.000001 or self.y + self.height > 1.000001:
            raise ValueError("subtitle region must remain inside the normalized frame")
        return self

    def as_aliyun(self) -> list[float]:
        return [self.x, self.y, self.width, self.height]


class TimeRange(BaseModel):
    start_seconds: Annotated[float, Field(ge=0)]
    end_seconds: Annotated[float, Field(gt=0)]

    @model_validator(mode="after")
    def validate_range(self) -> TimeRange:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        return self

    def as_aliyun(self) -> list[float]:
        return [self.start_seconds, self.end_seconds]


class SubtitleRemovalRequest(BaseModel):
    input_url: HttpUrl
    provider: SubtitleProviderName = SubtitleProviderName.AUTO
    regions: list[NormalizedRegion] = Field(default_factory=list)
    time_ranges: list[TimeRange] = Field(default_factory=list)
    model_id: str = ""
    output_filename: str = "clean.mp4"
    expected_duration_seconds: Annotated[float, Field(gt=0)] | None = None
    expected_width: Annotated[int, Field(gt=0)] | None = None
    expected_height: Annotated[int, Field(gt=0)] | None = None
    expected_fps: Annotated[float, Field(gt=0)] | None = None
    validate_output: bool = True
    cleanup_input: bool = False
    metadata: dict[str, object] = Field(default_factory=dict)


class ProviderSubmission(BaseModel):
    provider: str
    external_job_id: str = ""
    output_object_key: str = ""
    output_url: str = ""
    completed: bool = False
    metadata: dict[str, object] = Field(default_factory=dict)


class SubtitleOutputValidation(BaseModel):
    valid: bool
    size_bytes: int = 0
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    codec: str = ""
    warnings: list[str] = Field(default_factory=list)


class SubtitleRemovalJob(BaseModel):
    job_id: str
    state: SubtitleJobState
    request: SubtitleRemovalRequest
    selected_provider: str = ""
    external_job_id: str = ""
    output_object_key: str = ""
    output_url: str = ""
    canonical_output_url: str = ""
    created_at: str
    updated_at: str
    completed_at: str = ""
    attempts: int = 0
    validation: SubtitleOutputValidation | None = None
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)
    error: str = ""


class SubtitleCleanupResult(BaseModel):
    job_id: str
    state: SubtitleJobState
    deleted_output: bool = False
    deleted_input: bool = False
    warnings: list[str] = Field(default_factory=list)


class SubtitleProviderHealth(BaseModel):
    name: Literal["aliyun", "caca", "local"]
    configured: bool
    detail: str = ""
