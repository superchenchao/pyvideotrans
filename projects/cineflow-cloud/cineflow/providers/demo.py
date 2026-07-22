from __future__ import annotations

import asyncio

from ..models import (
    CandidateScore,
    DubbingArtifact,
    DubbingClip,
    JobRequest,
    LineEvidence,
    MediaArtifacts,
    OutputArtifact,
    ProviderHealth,
    SpeakerDecision,
    SubtitleLine,
    Transcript,
)


class DemoProviders:
    """Deterministic providers used for local API exploration and CI."""

    def __init__(self, delay_scale: float = 0.0) -> None:
        self.delay_scale = delay_scale

    async def health(self) -> list[ProviderHealth]:
        return [
            ProviderHealth(name="media", healthy=True, warm=True),
            ProviderHealth(name="asr", healthy=True, warm=True),
            ProviderHealth(name="caption", healthy=True, warm=True),
            ProviderHealth(name="speaker", healthy=True, warm=True),
            ProviderHealth(name="deepseek", healthy=True, warm=True),
            ProviderHealth(name="azure_tts", healthy=True, warm=True),
        ]

    async def _sleep(self, nominal_seconds: float) -> None:
        await asyncio.sleep(nominal_seconds * self.delay_scale)

    async def prepare_media(self, request: JobRequest) -> MediaArtifacts:
        await self._sleep(40)
        return MediaArtifacts(
            video_url=str(request.clean_video_url or request.input_url),
            background_url="memory://background.wav" if request.separate_background else None,
            source_audio_url=str(request.source_audio_url or "memory://source.wav"),
        )

    async def transcribe(self, request: JobRequest) -> Transcript:
        await self._sleep(20)
        duration_ms = int(request.probe.duration_seconds * 1000)
        step = max(1000, duration_ms // 3)
        return Transcript(
            language=request.source_language,
            provider="demo_cloud_asr",
            lines=[
                SubtitleLine(
                    line_id=1,
                    start_ms=0,
                    end_ms=step,
                    text="你好。",
                    speaker_id="spk1",
                    source="asr",
                ),
                SubtitleLine(
                    line_id=2,
                    start_ms=step,
                    end_ms=step * 2,
                    text="你是谁？",
                    speaker_id="spk2",
                    source="asr",
                ),
                SubtitleLine(
                    line_id=3,
                    start_ms=step * 2,
                    end_ms=duration_ms,
                    text="我是池沐雪。",
                    speaker_id="spk1",
                    source="asr",
                ),
            ],
        )

    async def extract_visual_subtitles(self, request: JobRequest) -> Transcript:
        await self._sleep(25)
        duration_ms = int(request.probe.duration_seconds * 1000)
        step = max(1000, duration_ms // 3)
        return Transcript(
            language=request.source_language,
            provider="demo_cloud_ocr",
            lines=[
                SubtitleLine(
                    line_id=1,
                    start_ms=0,
                    end_ms=step,
                    text="你好。",
                    source="ocr",
                ),
                SubtitleLine(
                    line_id=2,
                    start_ms=step,
                    end_ms=step * 2,
                    text="你是谁？",
                    source="ocr",
                ),
                SubtitleLine(
                    line_id=3,
                    start_ms=step * 2,
                    end_ms=duration_ms,
                    text="我是池沐雪。",
                    source="ocr",
                ),
            ],
        )

    async def analyze_speakers(
        self, request: JobRequest, transcript: Transcript
    ) -> list[LineEvidence]:
        await self._sleep(45)
        return [
            LineEvidence(
                line_id=1,
                audio=[CandidateScore(character_id="character_001", score=0.70)],
                visual=[CandidateScore(character_id="character_001", score=0.95)],
            ),
            LineEvidence(
                line_id=2,
                audio=[CandidateScore(character_id="character_002", score=0.80)],
                visual=[CandidateScore(character_id="character_002", score=0.90)],
            ),
            LineEvidence(
                line_id=3,
                audio=[CandidateScore(character_id="character_001", score=0.91)],
                text=[CandidateScore(character_id="character_001", score=0.88)],
                offscreen=True,
            ),
        ]

    async def translate(self, request: JobRequest, transcript: Transcript) -> Transcript:
        await self._sleep(8)
        return Transcript(
            language=request.target_language,
            lines=[
                line.model_copy(update={"text": f"[{request.target_language}] {line.text}"})
                for line in transcript.lines
            ],
        )

    async def synthesize(
        self,
        request: JobRequest,
        translated: Transcript,
        decisions: list[SpeakerDecision],
    ) -> DubbingArtifact:
        await self._sleep(35)
        by_line = {item.line_id: item for item in decisions}
        return DubbingArtifact(
            clips=[
                DubbingClip(
                    line_id=line.line_id,
                    character_id=by_line[line.line_id].character_id,
                    audio_url=f"memory://line-{line.line_id}.mp3",
                )
                for line in translated.lines
            ]
        )

    async def assemble(
        self,
        request: JobRequest,
        media: MediaArtifacts,
        translated: Transcript,
        dubbing: DubbingArtifact,
    ) -> OutputArtifact:
        await self._sleep(30)
        return OutputArtifact(
            video_url="memory://result.mp4",
            subtitle_url="memory://result.srt" if request.subtitle_mode != "none" else None,
        )
