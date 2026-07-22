from __future__ import annotations

import asyncio
import base64
import time

import httpx

from ..config import Settings
from ..fusion import fuse_speakers, merge_evidence
from ..models import (
    DubbingArtifact,
    DubbingClip,
    JobRequest,
    LineEvidence,
    MediaArtifacts,
    OutputArtifact,
    ProviderHealth,
    SpeakerDecision,
    Transcript,
)
from .azure_tts import AzureSpeechEndpoint, AzureTTSClient
from .deepseek import DeepSeekTranslator


def resolve_character_voice(request: JobRequest, character_id: str) -> str:
    return request.character_voices.get(character_id, "").strip() or request.target_voice.strip()


class HttpWorkerClient:
    def __init__(self, base_url: str, bearer_token: str = "") -> None:
        if not base_url:
            raise ValueError("worker URL is required in production mode")
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}

    async def get(self, path: str, timeout: float = 2.0) -> dict:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{self.base_url}{path}", headers=self.headers)
            response.raise_for_status()
            return response.json()

    async def health(self, name: str) -> ProviderHealth:
        started = time.perf_counter()
        try:
            data = await self.get("/healthz", 2.0)
            healthy = bool(data.get("ok", data.get("healthy", True)))
            warm = bool(data.get("warm", data.get("ready", healthy)))
            detail = str(data.get("detail", ""))
        except Exception as exc:
            healthy, warm, detail = False, False, str(exc)
        return ProviderHealth(
            name=name,
            healthy=healthy,
            warm=warm,
            detail=detail,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    async def post(self, path: str, payload: dict, timeout: float) -> dict:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{self.base_url}{path}", headers=self.headers, json=payload
            )
            response.raise_for_status()
            return response.json()


class FailoverWorkerClient:
    """Try the preferred worker first, then its optional fallback."""

    def __init__(
        self,
        primary: HttpWorkerClient,
        secondary: HttpWorkerClient | None = None,
    ) -> None:
        self.clients = [primary] + ([secondary] if secondary else [])

    async def health(self, name: str) -> ProviderHealth:
        results = await asyncio.gather(
            *(client.health(f"{name}_{index + 1}") for index, client in enumerate(self.clients))
        )
        healthy = [item for item in results if item.healthy]
        healthy_latencies = [item.latency_ms for item in healthy]
        all_latencies = [item.latency_ms for item in results]
        return ProviderHealth(
            name=name,
            healthy=bool(healthy),
            warm=any(item.warm for item in healthy),
            latency_ms=(
                min(healthy_latencies) if healthy_latencies else max(all_latencies, default=0.0)
            ),
            detail=" | ".join(
                f"{item.name}:{'ok' if item.healthy else item.detail}" for item in results
            ),
        )

    async def post(self, path: str, payload: dict, timeout: float) -> dict:
        errors: list[str] = []
        for client in self.clients:
            try:
                return await client.post(path, payload, timeout)
            except Exception as exc:
                errors.append(str(exc))
        raise RuntimeError("all worker endpoints failed: " + " | ".join(errors))


class ProductionProviders:
    """Standalone composition root.

    Media, ASR and speaker workers use a preferred endpoint plus an optional
    fallback. Deployment policy should point preferred endpoints at Alibaba Cloud
    implementations first and fallback endpoints at Volcengine when needed.
    DeepSeek is the only/default translation engine in this release; Azure TTS is
    retained for multilingual role dubbing.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        primary_media = HttpWorkerClient(settings.media_worker_url, settings.worker_bearer_token)
        secondary_media = (
            HttpWorkerClient(settings.secondary_media_worker_url, settings.worker_bearer_token)
            if settings.secondary_media_worker_url
            else None
        )
        self.media = FailoverWorkerClient(primary_media, secondary_media)

        primary_asr = HttpWorkerClient(settings.asr_worker_url, settings.worker_bearer_token)
        secondary_asr = (
            HttpWorkerClient(settings.secondary_asr_worker_url, settings.worker_bearer_token)
            if settings.secondary_asr_worker_url
            else None
        )
        self.asr = FailoverWorkerClient(primary_asr, secondary_asr)

        primary_speaker = HttpWorkerClient(
            settings.speaker_worker_url, settings.worker_bearer_token
        )
        secondary_speaker = (
            HttpWorkerClient(settings.secondary_speaker_worker_url, settings.worker_bearer_token)
            if settings.secondary_speaker_worker_url
            else None
        )
        self.speaker = FailoverWorkerClient(primary_speaker, secondary_speaker)

        self.translator = DeepSeekTranslator(
            settings.deepseek_api_key,
            settings.deepseek_base_url,
            settings.deepseek_model,
            max_tokens=settings.deepseek_max_tokens,
            thinking=settings.deepseek_thinking,
        )
        self.azure = AzureTTSClient(
            AzureSpeechEndpoint(settings.azure_speech_key, settings.azure_speech_region),
            AzureSpeechEndpoint(
                settings.azure_speech_secondary_key,
                settings.azure_speech_secondary_region,
            ),
            concurrency=settings.azure_tts_concurrency,
        )

    def _worker_timeout(self, field: str, default: float = 300.0) -> float:
        settings = getattr(self, "settings", None)
        return float(getattr(settings, field, default))

    async def health(self) -> list[ProviderHealth]:
        media, asr, speaker = await asyncio.gather(
            self.media.health("media"),
            self.asr.health("asr"),
            self.speaker.health("speaker"),
        )
        return [
            media,
            asr,
            speaker,
            ProviderHealth(
                name="deepseek",
                healthy=bool(self.translator.api_key),
                warm=True,
                detail="configured" if self.translator.api_key else "missing API key",
            ),
            ProviderHealth(
                name="azure_tts",
                healthy=bool(self.azure.endpoints),
                warm=True,
                detail=f"{len(self.azure.endpoints)} region(s) configured",
            ),
        ]

    async def prepare_media(self, request: JobRequest) -> MediaArtifacts:
        data = await self.media.post(
            "/v1/prepare",
            request.model_dump(mode="json"),
            self._worker_timeout("media_worker_timeout_seconds"),
        )
        return MediaArtifacts.model_validate(data)

    async def transcribe(self, request: JobRequest) -> Transcript:
        data = await self.asr.post(
            "/v1/transcribe",
            request.model_dump(mode="json"),
            self._worker_timeout("asr_worker_timeout_seconds"),
        )
        return Transcript.model_validate(data)

    async def analyze_speakers(
        self, request: JobRequest, transcript: Transcript
    ) -> list[LineEvidence]:
        data = await self.speaker.post(
            "/v1/analyze",
            {"job": request.model_dump(mode="json"), "transcript": transcript.model_dump()},
            self._worker_timeout("speaker_worker_timeout_seconds"),
        )
        evidence = [LineEvidence.model_validate(item) for item in data["evidence"]]
        preliminary = fuse_speakers(transcript.lines, evidence, review_threshold=0.82)
        ambiguous = {item.line_id for item in preliminary if item.needs_review}
        if not ambiguous:
            return evidence
        try:
            text_evidence = await self.translator.reason_speakers(
                request, transcript, evidence, ambiguous
            )
        except Exception:
            # Text reasoning is weak evidence and must never block delivery.
            return evidence
        return merge_evidence(evidence, text_evidence)

    async def translate(self, request: JobRequest, transcript: Transcript) -> Transcript:
        return await self.translator.translate(request, transcript)

    async def synthesize(
        self,
        request: JobRequest,
        translated: Transcript,
        decisions: list[SpeakerDecision],
    ) -> DubbingArtifact:
        decision_by_line = {item.line_id: item for item in decisions}

        async def one(line):
            decision = decision_by_line[line.line_id]
            voice = resolve_character_voice(request, decision.character_id)
            if not voice:
                raise ValueError(
                    "target_voice is required until the character voice registry is connected"
                )
            audio = await self.azure.synthesize(request.target_language, voice, line.text)
            artifact = await self.media.post(
                "/v1/artifacts/base64",
                {
                    "name": f"line-{line.line_id}.mp3",
                    "content_base64": base64.b64encode(audio).decode("ascii"),
                },
                20.0,
            )
            return DubbingClip(
                line_id=line.line_id,
                character_id=decision.character_id,
                audio_url=artifact["url"],
            )

        clips = await asyncio.gather(*(one(line) for line in translated.lines))
        return DubbingArtifact(clips=list(clips))

    async def assemble(
        self,
        request: JobRequest,
        media: MediaArtifacts,
        translated: Transcript,
        dubbing: DubbingArtifact,
    ) -> OutputArtifact:
        data = await self.media.post(
            "/v1/assemble",
            {
                "job": request.model_dump(mode="json"),
                "media": media.model_dump(),
                "translated": translated.model_dump(),
                "dubbing": dubbing.model_dump(),
            },
            self._worker_timeout("media_worker_timeout_seconds"),
        )
        return OutputArtifact.model_validate(data)
