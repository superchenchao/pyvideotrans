from __future__ import annotations

import asyncio
from xml.sax.saxutils import escape

import httpx


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


class AzureTTSClient:
    """Low-latency Azure REST client with regional failover and bounded concurrency."""

    def __init__(
        self,
        primary: AzureSpeechEndpoint,
        secondary: AzureSpeechEndpoint | None = None,
        concurrency: int = 8,
    ) -> None:
        if not primary.configured:
            raise ValueError("Azure Speech primary endpoint is required")
        self.endpoints = [primary]
        if secondary and secondary.configured:
            self.endpoints.append(secondary)
        self.semaphore = asyncio.Semaphore(concurrency)

    @staticmethod
    def build_ssml(language: str, voice: str, text: str, rate: str = "+0%") -> str:
        safe_text = escape(text)
        safe_voice = escape(voice, {"\"": "&quot;"})
        safe_language = escape(language, {"\"": "&quot;"})
        safe_rate = escape(rate, {"\"": "&quot;"})
        return (
            f'<speak version="1.0" xml:lang="{safe_language}" '
            'xmlns="http://www.w3.org/2001/10/synthesis">'
            f'<voice name="{safe_voice}"><prosody rate="{safe_rate}">'
            f"{safe_text}</prosody></voice></speak>"
        )

    async def synthesize(self, language: str, voice: str, text: str) -> bytes:
        ssml = self.build_ssml(language, voice, text)
        errors: list[str] = []
        async with self.semaphore:
            for endpoint in self.endpoints:
                try:
                    async with httpx.AsyncClient(timeout=20.0) as client:
                        for attempt in range(2):
                            response = await client.post(
                                endpoint.url,
                                headers={
                                    "Ocp-Apim-Subscription-Key": endpoint.key,
                                    "Content-Type": "application/ssml+xml",
                                    "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
                                    "User-Agent": "cineflow-cloud",
                                },
                                content=ssml.encode("utf-8"),
                            )
                            if response.status_code not in {429, 500, 502, 503, 504}:
                                response.raise_for_status()
                                return response.content
                            if attempt == 0:
                                retry_after = response.headers.get("Retry-After", "0.5")
                                try:
                                    delay = min(2.0, max(0.1, float(retry_after)))
                                except ValueError:
                                    delay = 0.5
                                await asyncio.sleep(delay)
                        response.raise_for_status()
                except Exception as exc:  # region failover intentionally catches transport errors
                    errors.append(f"{endpoint.region}: {exc}")
        raise RuntimeError("Azure TTS failed in all configured regions: " + " | ".join(errors))
