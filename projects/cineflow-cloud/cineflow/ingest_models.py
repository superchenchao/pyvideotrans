from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field, field_validator


class UploadState(StrEnum):
    CREATED = "created"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    ABORTED = "aborted"
    FAILED = "failed"


class UploadSessionRequest(BaseModel):
    filename: Annotated[str, Field(min_length=1, max_length=240)]
    size_bytes: Annotated[int, Field(gt=0)]
    content_type: str = "application/octet-stream"
    sha256: str = ""
    project_id: str = "default"

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized and (
            len(normalized) != 64
            or any(character not in "0123456789abcdef" for character in normalized)
        ):
            raise ValueError("sha256 must be a 64-character hexadecimal digest")
        return normalized


class UploadCredentials(BaseModel):
    access_key_id: str
    access_key_secret: str
    security_token: str
    expiration: str
    expiration_epoch: int


class UploadSession(BaseModel):
    session_id: str
    state: UploadState
    endpoint: str
    bucket: str
    object_key: str
    canonical_url: str
    credentials: UploadCredentials
    size_bytes: int
    content_type: str
    sha256: str = ""
    multipart_upload_id: str = ""
    created_at: str
    updated_at: str
    completed_at: str = ""
    download_url: str = ""
    error: str = ""


class MultipartRegistration(BaseModel):
    upload_id: Annotated[str, Field(min_length=1, max_length=256)]


class UploadCompleteRequest(BaseModel):
    size_bytes: Annotated[int, Field(gt=0)]
    sha256: str = ""

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized and (
            len(normalized) != 64
            or any(character not in "0123456789abcdef" for character in normalized)
        ):
            raise ValueError("sha256 must be a 64-character hexadecimal digest")
        return normalized


class UploadValidation(BaseModel):
    valid: bool
    object_key: str
    size_bytes: int = 0
    expected_size_bytes: int = 0
    content_type: str = ""
    sha256: str = ""
    expected_sha256: str = ""
    etag: str = ""
    download_url: str = ""
    warnings: list[str] = Field(default_factory=list)
