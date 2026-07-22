from __future__ import annotations

from typing import Protocol

from ..models import (
    DubbingArtifact,
    JobRequest,
    LineEvidence,
    MediaArtifacts,
    OutputArtifact,
    ProviderHealth,
    SpeakerDecision,
    Transcript,
)


class PipelineProviders(Protocol):
    async def health(self) -> list[ProviderHealth]: ...

    async def prepare_media(self, request: JobRequest) -> MediaArtifacts: ...

    async def transcribe(self, request: JobRequest) -> Transcript: ...

    async def analyze_speakers(
        self, request: JobRequest, transcript: Transcript
    ) -> list[LineEvidence]: ...

    async def translate(self, request: JobRequest, transcript: Transcript) -> Transcript: ...

    async def synthesize(
        self,
        request: JobRequest,
        translated: Transcript,
        decisions: list[SpeakerDecision],
    ) -> DubbingArtifact: ...

    async def assemble(
        self,
        request: JobRequest,
        media: MediaArtifacts,
        translated: Transcript,
        dubbing: DubbingArtifact,
    ) -> OutputArtifact: ...
