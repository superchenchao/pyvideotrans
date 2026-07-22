from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import CandidateScore, JobRequest, LineEvidence, Transcript


class AudioTurn(BaseModel):
    start_ms: int
    end_ms: int
    speaker_id: str
    confidence: float = Field(default=0.85, ge=0, le=1)


class VisualTrack(BaseModel):
    start_ms: int
    end_ms: int
    face_id: str
    score: float = Field(ge=0, le=1)
    av_sync_confidence: float = Field(default=0.8, ge=0, le=1)


class AnalyzeRequest(BaseModel):
    job: JobRequest
    transcript: Transcript


class SpeakerWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CINEFLOW_SPEAKER_",
        env_file=".env",
        extra="ignore",
    )

    mode: str = "demo"
    audio_backend: Literal["auto", "asr", "pyannote"] = "auto"
    asr_turn_confidence: float = Field(default=0.9, ge=0, le=1)
    hf_token: str = ""
    pyannote_model: str = "pyannote/speaker-diarization-community-1"
    device: str = "cuda"
    active_speaker_command: str = ""
    host: str = "0.0.0.0"
    port: int = 8090


def overlap_ms(
    left_start: int,
    left_end: int,
    right_start: int,
    right_end: int,
) -> int:
    return max(0, min(left_end, right_end) - max(left_start, right_start))


def audio_turns_from_transcript(
    transcript: Transcript,
    *,
    confidence: float = 0.9,
) -> list[AudioTurn]:
    """Use cloud ASR diarization labels as the first audio-speaker evidence."""

    return [
        AudioTurn(
            start_ms=line.start_ms,
            end_ms=line.end_ms,
            speaker_id=line.speaker_id,
            confidence=confidence,
        )
        for line in transcript.lines
        if line.speaker_id
    ]


def associate_audio_speakers_with_faces(
    audio_turns: list[AudioTurn],
    visual_tracks: list[VisualTrack],
) -> dict[str, str]:
    """One-to-one audio speaker to face association from confident co-speech."""

    scores: dict[tuple[str, str], float] = defaultdict(float)
    for audio in audio_turns:
        for visual in visual_tracks:
            overlap = overlap_ms(
                audio.start_ms,
                audio.end_ms,
                visual.start_ms,
                visual.end_ms,
            )
            if overlap:
                scores[(audio.speaker_id, visual.face_id)] += (
                    overlap * audio.confidence * visual.score * visual.av_sync_confidence
                )

    mapping: dict[str, str] = {}
    used_faces: set[str] = set()
    for (speaker_id, face_id), _score in sorted(
        scores.items(),
        key=lambda item: (-item[1], item[0][0], item[0][1]),
    ):
        if speaker_id in mapping or face_id in used_faces:
            continue
        mapping[speaker_id] = face_id
        used_faces.add(face_id)
    return mapping


def _candidate_scores(
    values: dict[str, float],
    duration_ms: int,
) -> list[CandidateScore]:
    if not values:
        return []
    return [
        CandidateScore(
            character_id=character_id,
            score=min(1.0, max(0.001, value / duration_ms)),
        )
        for character_id, value in sorted(
            values.items(),
            key=lambda pair: (-pair[1], pair[0]),
        )
    ]


def build_line_evidence(
    transcript: Transcript,
    audio_turns: list[AudioTurn],
    visual_tracks: list[VisualTrack],
    speaker_to_face: dict[str, str] | None = None,
) -> list[LineEvidence]:
    mapping = speaker_to_face or {}
    rows: list[LineEvidence] = []
    for line in transcript.lines:
        duration = max(1, line.end_ms - line.start_ms)
        audio_scores: dict[str, float] = defaultdict(float)
        visual_scores: dict[str, float] = defaultdict(float)
        total_audio_overlap = 0
        active_audio_speakers: set[str] = set()
        av_sync = 0.0

        for turn in audio_turns:
            overlap = overlap_ms(
                line.start_ms,
                line.end_ms,
                turn.start_ms,
                turn.end_ms,
            )
            if not overlap:
                continue
            character_id = mapping.get(
                turn.speaker_id,
                f"audio:{turn.speaker_id}",
            )
            audio_scores[character_id] += overlap * turn.confidence
            total_audio_overlap += overlap
            active_audio_speakers.add(turn.speaker_id)

        for track in visual_tracks:
            overlap = overlap_ms(
                line.start_ms,
                line.end_ms,
                track.start_ms,
                track.end_ms,
            )
            if not overlap:
                continue
            visual_scores[track.face_id] += overlap * track.score
            av_sync = max(av_sync, track.av_sync_confidence)

        visual_candidates = _candidate_scores(visual_scores, duration)
        rows.append(
            LineEvidence(
                line_id=line.line_id,
                audio=_candidate_scores(audio_scores, duration),
                visual=visual_candidates,
                offscreen=(
                    not visual_candidates or max(item.score for item in visual_candidates) < 0.2
                ),
                overlap_speech=(
                    len(active_audio_speakers) > 1 and total_audio_overlap > duration * 1.05
                ),
                av_sync_confidence=av_sync if visual_candidates else 0.0,
            )
        )
    return rows


class SpeakerRuntime:
    def __init__(self, settings: SpeakerWorkerSettings) -> None:
        self.settings = settings
        self.pipeline = None
        self.warm = settings.mode == "demo"
        self.detail = "demo" if self.warm else "not loaded"

    async def startup(self) -> None:
        if self.settings.mode == "demo":
            return
        if not self.settings.active_speaker_command:
            self.detail = "CINEFLOW_SPEAKER_ACTIVE_SPEAKER_COMMAND is missing"
            return

        if self.settings.audio_backend == "asr":
            self.warm = True
            self.detail = "cloud ASR diarization and active-speaker command ready"
            return

        if not self.settings.hf_token:
            if self.settings.audio_backend == "auto":
                self.warm = True
                self.detail = (
                    "cloud ASR diarization and active-speaker command ready; "
                    "pyannote fallback unavailable"
                )
                return
            self.detail = "CINEFLOW_SPEAKER_HF_TOKEN is missing"
            return

        try:
            self.pipeline = await asyncio.to_thread(self._load_pyannote)
            self.warm = True
            self.detail = (
                "cloud ASR diarization preferred; pyannote and active-speaker fallback ready"
                if self.settings.audio_backend == "auto"
                else "pyannote and active-speaker command ready"
            )
        except Exception as exc:
            self.detail = str(exc)
            self.warm = False

    def _load_pyannote(self):
        import torch
        from pyannote.audio import Pipeline

        pipeline = Pipeline.from_pretrained(
            self.settings.pyannote_model,
            token=self.settings.hf_token,
        )
        pipeline.to(torch.device(self.settings.device))
        return pipeline

    async def analyze(self, request: AnalyzeRequest) -> list[LineEvidence]:
        if self.settings.mode == "demo":
            return self._demo_evidence(request.transcript)
        if not self.warm:
            raise RuntimeError(self.detail)

        asr_turns = audio_turns_from_transcript(
            request.transcript,
            confidence=self.settings.asr_turn_confidence,
        )
        use_asr_turns = self.settings.audio_backend != "pyannote" and bool(asr_turns)
        if self.settings.audio_backend == "asr" and not use_asr_turns:
            raise RuntimeError("ASR audio backend selected but transcript has no speaker_id labels")

        with tempfile.TemporaryDirectory(prefix="cineflow-speaker-") as directory:
            work = Path(directory)
            video_url = request.job.clean_video_url or request.job.input_url
            video = await self._materialize(
                str(video_url),
                work / "input.mp4",
            )
            visual_task = asyncio.to_thread(
                self._run_active_speaker,
                video,
                work,
            )

            if use_asr_turns:
                audio_turns = asr_turns
                visual_tracks = await visual_task
            else:
                if self.pipeline is None:
                    raise RuntimeError(
                        "transcript has no cloud ASR speaker labels and "
                        "pyannote fallback is unavailable"
                    )
                audio = await self._prepare_audio(request.job, video, work)
                audio_task = asyncio.to_thread(
                    self._run_pyannote,
                    audio,
                    request.job.expected_speakers,
                )
                audio_turns, visual_tracks = await asyncio.gather(
                    audio_task,
                    visual_task,
                )

            mapping = associate_audio_speakers_with_faces(
                audio_turns,
                visual_tracks,
            )
            return build_line_evidence(
                request.transcript,
                audio_turns,
                visual_tracks,
                mapping,
            )

    async def _prepare_audio(
        self,
        job: JobRequest,
        video: Path,
        work: Path,
    ) -> Path:
        if job.source_audio_url is not None:
            suffix = Path(urlparse(str(job.source_audio_url)).path).suffix or ".audio"
            return await self._materialize(
                str(job.source_audio_url),
                work / f"source{suffix}",
            )
        audio = work / "audio.wav"
        await self._extract_audio(video, audio)
        return audio

    async def _materialize(self, url: str, destination: Path) -> Path:
        parsed = urlparse(url)
        if parsed.scheme == "file":
            source = Path(parsed.path)
            shutil.copy2(source, destination)
            return destination
        if parsed.scheme in {"http", "https"}:
            async with (
                httpx.AsyncClient(
                    timeout=60.0,
                    follow_redirects=True,
                ) as client,
                client.stream("GET", url) as response,
            ):
                response.raise_for_status()
                with destination.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        output.write(chunk)
            return destination
        source = Path(url)
        if source.is_file():
            shutil.copy2(source, destination)
            return destination
        raise ValueError(f"unsupported input URL: {url}")

    async def _extract_audio(self, video: Path, audio: Path) -> None:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(audio),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(stderr.decode("utf-8", errors="replace"))

    def _run_pyannote(
        self,
        audio: Path,
        expected_speakers: int | None,
    ) -> list[AudioTurn]:
        kwargs = (
            {
                "num_speakers": expected_speakers,
            }
            if expected_speakers
            else {}
        )
        output = self.pipeline(str(audio), **kwargs)
        annotation = getattr(output, "speaker_diarization", output)
        turns: list[AudioTurn] = []
        if hasattr(annotation, "itertracks"):
            iterator = (
                (segment, speaker)
                for segment, _track, speaker in annotation.itertracks(yield_label=True)
            )
        else:
            iterator = iter(annotation)
        for segment, speaker in iterator:
            turns.append(
                AudioTurn(
                    start_ms=round(float(segment.start) * 1000),
                    end_ms=round(float(segment.end) * 1000),
                    speaker_id=str(speaker),
                )
            )
        return turns

    def _run_active_speaker(
        self,
        video: Path,
        work: Path,
    ) -> list[VisualTrack]:
        output = work / "active-speaker.json"
        template = self.settings.active_speaker_command
        command = shlex.split(
            template.format(
                input=str(video),
                output=str(output),
                workdir=str(work),
            )
        )
        subprocess.run(
            command,
            check=True,
            timeout=90,
            env=os.environ.copy(),
        )
        data = json.loads(output.read_text(encoding="utf-8"))
        raw_tracks = data.get("tracks", data) if isinstance(data, dict) else data
        return [VisualTrack.model_validate(item) for item in raw_tracks]

    @staticmethod
    def _demo_evidence(transcript: Transcript) -> list[LineEvidence]:
        rows: list[LineEvidence] = []
        for index, line in enumerate(transcript.lines):
            character = f"character_{index % 2 + 1:03d}"
            rows.append(
                LineEvidence(
                    line_id=line.line_id,
                    audio=[
                        CandidateScore(
                            character_id=character,
                            score=0.88,
                        )
                    ],
                    visual=[
                        CandidateScore(
                            character_id=character,
                            score=0.95,
                        )
                    ],
                    av_sync_confidence=0.9,
                )
            )
        return rows


settings = SpeakerWorkerSettings()
runtime = SpeakerRuntime(settings)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await runtime.startup()
    yield


app = FastAPI(
    title="CineFlow CineFusion Worker",
    version="0.3.0",
    lifespan=lifespan,
)


@app.get("/healthz")
async def healthz() -> dict[str, object]:
    return {
        "ok": runtime.warm,
        "warm": runtime.warm,
        "detail": runtime.detail,
        "backend": f"{settings.audio_backend}+external-active-speaker",
    }


@app.post("/v1/analyze")
async def analyze(payload: AnalyzeRequest) -> dict[str, object]:
    started = time.perf_counter()
    try:
        evidence = await runtime.analyze(payload)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "evidence": [item.model_dump() for item in evidence],
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def run() -> None:
    uvicorn.run(app, host=settings.host, port=settings.port)
