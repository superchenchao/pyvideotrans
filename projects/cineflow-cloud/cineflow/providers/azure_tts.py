from __future__ import annotations

import asyncio
import math
import wave
from dataclasses import dataclass, replace
from io import BytesIO
from xml.sax.saxutils import escape

import httpx

AZURE_PCM_OUTPUT_FORMAT = "riff-24khz-16bit-mono-pcm"


class AzureSpeechEndpoint:
    def __init__(self, key: str, region: str) -> None:
        self.key = key.strip()
        self.region = region.strip()

    @property
    def configured(self) -> bool:
        return bool(self.key and self.region)

    @property
    def url(self) -> str:
        return f"https://{self.region}.tts.speech.microsoft.com/cognitiveservices/v1"


@dataclass(frozen=True)
class AzureSynthesisResult:
    audio: bytes
    duration_ms: int
    target_duration_ms: int | None
    rate_percent: int
    attempts: int
    overflow_ms: int
    within_target: bool
    output_format: str = AZURE_PCM_OUTPUT_FORMAT
    extension: str = ".wav"


def wav_duration_ms(audio: bytes) -> int:
    if not audio:
        raise ValueError("Azure TTS returned empty audio")
    try:
        with wave.open(BytesIO(audio), "rb") as source:
            sample_rate = source.getframerate()
            frames = source.getnframes()
    except (EOFError, wave.Error) as exc:
        raise ValueError("Azure TTS response is not a valid RIFF PCM WAV") from exc
    if sample_rate <= 0 or frames <= 0:
        raise ValueError("Azure TTS WAV has no usable samples")
    return max(1, round(frames * 1000 / sample_rate))


class AzureTTSClient:
    """Azure REST TTS with failover, concurrency, duration measurement, and slot fitting."""

    def __init__(
        self,
        primary: AzureSpeechEndpoint,
        secondary: AzureSpeechEndpoint | None = None,
        concurrency: int = 8,
        *,
        max_fit_rate_percent: int = 55,
        duration_tolerance_ratio: float = 1.08,
        max_fit_attempts: int = 2,
        request_timeout_seconds: float = 25.0,
        output_format: str = AZURE_PCM_OUTPUT_FORMAT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not primary.configured:
            raise ValueError("Azure Speech primary endpoint is required")
        self.endpoints = [primary]
        if secondary and secondary.configured:
            self.endpoints.append(secondary)
        self.semaphore = asyncio.Semaphore(max(1, concurrency))
        self.max_fit_rate_percent = min(100, max(0, int(max_fit_rate_percent)))
        self.duration_tolerance_ratio = max(1.0, float(duration_tolerance_ratio))
        self.max_fit_attempts = max(1, int(max_fit_attempts))
        self.request_timeout_seconds = max(1.0, float(request_timeout_seconds))
        self.output_format = output_format
        self.transport = transport

    @staticmethod
    def build_ssml(
        language: str,
        voice: str,
        text: str,
        rate: str = "+0%",
    ) -> str:
        safe_text = escape(text)
        safe_voice = escape(voice, {'"': "&quot;"})
        safe_language = escape(language, {'"': "&quot;"})
        safe_rate = escape(rate, {'"': "&quot;"})
        return (
            f'<speak version="1.0" xml:lang="{safe_language}" '
            'xmlns="http://www.w3.org/2001/10/synthesis">'
            f'<voice name="{safe_voice}"><prosody rate="{safe_rate}">'
            f"{safe_text}</prosody></voice></speak>"
        )

    async def synthesize(
        self,
        language: str,
        voice: str,
        text: str,
        *,
        rate_percent: int = 0,
    ) -> bytes:
        return await self._synthesize_audio(
            language,
            voice,
            text,
            rate_percent=rate_percent,
        )

    async def synthesize_for_slot(
        self,
        language: str,
        voice: str,
        text: str,
        target_duration_ms: int,
    ) -> AzureSynthesisResult:
        target = max(1, int(target_duration_ms))
        tolerance_limit = max(target, round(target * self.duration_tolerance_ratio))
        rate_percent = 0
        attempted_rates: set[int] = set()
        best: AzureSynthesisResult | None = None
        attempts = 0

        for _index in range(self.max_fit_attempts):
            if rate_percent in attempted_rates:
                break
            attempted_rates.add(rate_percent)
            attempts += 1
            audio = await self._synthesize_audio(
                language,
                voice,
                text,
                rate_percent=rate_percent,
            )
            duration_ms = wav_duration_ms(audio)
            candidate = AzureSynthesisResult(
                audio=audio,
                duration_ms=duration_ms,
                target_duration_ms=target,
                rate_percent=rate_percent,
                attempts=attempts,
                overflow_ms=max(0, duration_ms - target),
                within_target=duration_ms <= tolerance_limit,
                output_format=self.output_format,
            )
            if best is None or candidate.duration_ms < best.duration_ms:
                best = candidate
            if candidate.within_target:
                return candidate

            required_rate = math.ceil((duration_ms / target - 1.0) * 100)
            next_rate = min(
                self.max_fit_rate_percent,
                max(rate_percent + 5, required_rate),
            )
            if next_rate <= rate_percent:
                break
            rate_percent = next_rate

        if best is None:
            raise RuntimeError("Azure TTS did not produce an audio candidate")
        return replace(best, attempts=attempts)

    async def _synthesize_audio(
        self,
        language: str,
        voice: str,
        text: str,
        *,
        rate_percent: int,
    ) -> bytes:
        normalized_rate = min(100, max(-50, int(rate_percent)))
        ssml = self.build_ssml(
            language,
            voice,
            text,
            rate=f"{normalized_rate:+d}%",
        )
        errors: list[str] = []
        async with self.semaphore, httpx.AsyncClient(
            timeout=self.request_timeout_seconds,
            transport=self.transport,
        ) as client:
            for endpoint in self.endpoints:
                try:
                    response: httpx.Response | None = None
                    for attempt in range(2):
                        response = await client.post(
                            endpoint.url,
                            headers={
                                "Ocp-Apim-Subscription-Key": endpoint.key,
                                "Content-Type": "application/ssml+xml",
                                "X-Microsoft-OutputFormat": self.output_format,
                                "User-Agent": "cineflow-cloud",
                            },
                            content=ssml.encode("utf-8"),
                        )
                        if response.status_code not in {
                            429,
                            500,
                            502,
                            503,
                            504,
                        }:
                            response.raise_for_status()
                            return response.content
                        if attempt == 0:
                            retry_after = response.headers.get(
                                "Retry-After",
                                "0.5",
                            )
                            try:
                                delay = min(
                                    2.0,
                                    max(0.1, float(retry_after)),
                                )
                            except ValueError:
                                delay = 0.5
                            await asyncio.sleep(delay)
                    if response is not None:
                        response.raise_for_status()
                except Exception as exc:
                    errors.append(f"{endpoint.region}: {exc}")
        raise RuntimeError("Azure TTS failed in all configured regions: " + " | ".join(errors))
