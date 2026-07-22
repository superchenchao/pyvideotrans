from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from ..models import (
    DubbingArtifact,
    JobRequest,
    MediaArtifacts,
    OutputArtifact,
    Transcript,
)
from .aliyun_ice import AliyunICEClient, AliyunICEError
from .aliyun_oss import AliyunOSSError, AliyunOSSStore


class AliyunMediaError(RuntimeError):
    """Normalized Alibaba Cloud media-preparation or assembly failure."""


@dataclass(frozen=True)
class AliyunMediaConfig:
    background_gain: float = 0.25
    output_bitrate_kbps: int = 3000
    hard_subtitle_font: str = "Alibaba PuHuiTi"
    hard_subtitle_font_size: int = 54
    hard_subtitle_y: float = 0.88
    hard_subtitle_text_width: float = 0.9
    hard_subtitle_outline: int = 2
    max_audio_tracks: int = 100


def _seconds(milliseconds: int) -> float:
    return round(max(0, milliseconds) / 1000.0, 3)


def _safe_name(value: str, fallback: str = "artifact") -> str:
    name = PurePosixPath(str(value or "")).name
    name = re.sub(r"[^0-9A-Za-z._-]+", "-", name).strip("-.")
    return name[:120] or fallback


def transcript_to_srt(transcript: Transcript) -> str:
    def timestamp(milliseconds: int) -> str:
        milliseconds = max(0, int(milliseconds))
        hours, remainder = divmod(milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, millis = divmod(remainder, 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"

    return "\n\n".join(
        f"{index}\n{timestamp(line.start_ms)} --> {timestamp(line.end_ms)}\n"
        f"{line.text.strip()}"
        for index, line in enumerate(transcript.lines, 1)
        if line.text.strip()
    )


def build_subtitle_track(
    transcript: Transcript,
    config: AliyunMediaConfig,
) -> dict[str, object]:
    clips = []
    for line in transcript.lines:
        text = line.text.strip()
        if not text:
            continue
        clips.append(
            {
                "Type": "Text",
                "TimelineIn": _seconds(line.start_ms),
                "TimelineOut": _seconds(line.end_ms),
                "X": 0.5,
                "Y": config.hard_subtitle_y,
                "Content": text,
                "Alignment": "BottomCenter",
                "Font": config.hard_subtitle_font,
                "FontSize": config.hard_subtitle_font_size,
                "FontColor": "#FFFFFF",
                "Outline": config.hard_subtitle_outline,
                "OutlineColour": "#000000",
                "AdaptMode": "AutoWrap",
                "TextWidth": config.hard_subtitle_text_width,
            }
        )
    return {"SubtitleTrackClips": clips}


def _pack_dialogue_audio_tracks(
    transcript: Transcript,
    dubbing: DubbingArtifact,
    *,
    max_tracks: int,
) -> list[dict[str, object]]:
    lines = {line.line_id: line for line in transcript.lines}
    scheduled = []
    for clip in dubbing.clips:
        line = lines.get(clip.line_id)
        if line is None:
            raise AliyunMediaError(
                f"dubbing clip line {clip.line_id} does not exist in translated transcript"
            )
        effective_duration = clip.duration_ms or max(1, line.end_ms - line.start_ms)
        scheduled.append(
            (
                line.start_ms,
                line.start_ms + effective_duration,
                {
                    "MediaURL": clip.audio_url,
                    "TimelineIn": _seconds(line.start_ms),
                },
            )
        )
    scheduled.sort(key=lambda item: (item[0], item[1]))

    tracks: list[list[dict[str, object]]] = []
    track_ends: list[int] = []
    for start_ms, end_ms, item in scheduled:
        target = next(
            (index for index, previous_end in enumerate(track_ends) if previous_end <= start_ms),
            None,
        )
        if target is None:
            if len(tracks) >= max_tracks:
                raise AliyunMediaError(
                    f"dubbing overlap requires more than {max_tracks} audio tracks"
                )
            tracks.append([])
            track_ends.append(0)
            target = len(tracks) - 1
        tracks[target].append(item)
        track_ends[target] = end_ms
    return [{"AudioTrackClips": clips} for clips in tracks]


def build_assembly_timeline(
    *,
    video_url: str,
    translated: Transcript,
    dubbing: DubbingArtifact,
    background_url: str | None,
    subtitle_mode: str,
    width: int,
    height: int,
    config: AliyunMediaConfig,
) -> dict[str, object]:
    timeline: dict[str, object] = {
        "FECanvas": {"Width": width, "Height": height},
        "VideoTracks": [
            {
                "VideoTrackClips": [
                    {
                        "MediaURL": video_url,
                        "Effects": [{"Type": "Volume", "Gain": 0}],
                    }
                ]
            }
        ],
    }
    audio_tracks: list[dict[str, object]] = []
    if background_url:
        audio_tracks.append(
            {
                "AudioTrackClips": [
                    {
                        "MediaURL": background_url,
                        "TimelineIn": 0,
                        "Effects": [
                            {"Type": "Volume", "Gain": config.background_gain}
                        ],
                    }
                ]
            }
        )
    dialogue_limit = max(1, config.max_audio_tracks - len(audio_tracks))
    audio_tracks.extend(
        _pack_dialogue_audio_tracks(
            translated,
            dubbing,
            max_tracks=dialogue_limit,
        )
    )
    timeline["AudioTracks"] = audio_tracks
    if subtitle_mode == "hard":
        timeline["SubtitleTracks"] = [build_subtitle_track(translated, config)]
    return timeline


def build_audio_extract_timeline(video_url: str) -> dict[str, object]:
    return {
        "AudioTracks": [
            {"AudioTrackClips": [{"MediaURL": video_url}]}
        ]
    }


def _walk_output_values(value: object, label: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk_output_values(child, f"{label}/{key}" if label else str(key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_output_values(child, f"{label}/{index}")
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith(("http://", "https://", "oss://")):
            yield label, text
        elif text.startswith(("{", "[")):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return
            yield from _walk_output_values(parsed, label)


def collect_i_production_outputs(
    result: dict[str, Any],
    store: AliyunOSSStore,
) -> list[tuple[str, str]]:
    candidates = list(_walk_output_values(result.get("OutputUrls", []), "OutputUrls"))
    candidates.extend(_walk_output_values(result.get("Result", ""), "Result"))
    for index, object_key in enumerate(result.get("OutputFiles", []) or []):
        text = str(object_key or "").strip()
        if not text:
            continue
        url = text if text.startswith(("http://", "https://", "oss://")) else store.canonical_url(text)
        candidates.append((f"OutputFiles/{index}", url))

    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, url in candidates:
        if url in seen:
            continue
        seen.add(url)
        unique.append((label, url))
    return unique


def classify_demix_outputs(
    candidates: list[tuple[str, str]],
) -> tuple[str | None, str | None]:
    vocal: str | None = None
    background: str | None = None
    vocal_words = ("vocal", "vocals", "voice", "human", "singer", "人声")
    background_words = (
        "accompaniment",
        "instrumental",
        "instrument",
        "music",
        "background",
        "bgm",
        "伴奏",
    )
    for label, url in candidates:
        haystack = f"{label} {url}".lower()
        if vocal is None and any(word in haystack for word in vocal_words):
            vocal = url
        if background is None and any(word in haystack for word in background_words):
            background = url

    if len(candidates) == 2:
        urls = [url for _label, url in candidates]
        if vocal and not background:
            background = next((url for url in urls if url != vocal), None)
        if background and not vocal:
            vocal = next((url for url in urls if url != background), None)
    return vocal, background


def output_dimensions(request: JobRequest) -> tuple[int, int]:
    width = int(request.probe.width)
    height = int(request.probe.height)
    if width <= 1920 and height <= 1920 and width * height <= 1920 * 1080:
        return width - width % 2, height - height % 2
    scale = min(1920 / width, 1080 / height) if width >= height else min(1080 / width, 1920 / height)
    return max(128, int(width * scale) // 2 * 2), max(128, int(height * scale) // 2 * 2)


class AliyunMediaService:
    def __init__(
        self,
        ice: AliyunICEClient,
        store: AliyunOSSStore,
        config: AliyunMediaConfig | None = None,
    ) -> None:
        self.ice = ice
        self.store = store
        self.config = config or AliyunMediaConfig()

    async def save_artifact(self, name: str, content: bytes) -> dict[str, str]:
        object_key = self.store.key(
            "artifacts",
            uuid.uuid4().hex,
            _safe_name(name),
        )
        try:
            await __import__("asyncio").to_thread(
                self.store.put_bytes,
                object_key,
                content,
            )
        except AliyunOSSError as exc:
            raise AliyunMediaError(str(exc)) from exc
        return {
            "url": self.store.canonical_url(object_key),
            "download_url": self.store.signed_url(object_key),
            "oss_uri": self.store.oss_uri(object_key),
            "object_key": object_key,
        }

    async def prepare(self, request: JobRequest) -> MediaArtifacts:
        video_url = self.store.canonicalize_oss_input(
            str(request.clean_video_url or request.input_url)
        )
        source_audio_url = (
            self.store.canonicalize_oss_input(str(request.source_audio_url))
            if request.source_audio_url is not None
            else None
        )
        degraded: list[str] = []
        task_ids: dict[str, str] = {}
        metadata: dict[str, object] = {}

        if not source_audio_url:
            try:
                source_audio_url, task_id = await self._extract_audio(video_url)
                task_ids["audio_extract"] = task_id
            except (AliyunICEError, AliyunOSSError, AliyunMediaError) as exc:
                degraded.append("audio_extract")
                metadata["audio_extract_error"] = str(exc)

        vocal_url = None
        background_url = None
        if request.separate_background and source_audio_url:
            try:
                vocal_url, background_url, task_id, outputs = await self._demix(
                    source_audio_url
                )
                task_ids["music_demix"] = task_id
                metadata["music_demix_outputs"] = outputs
                if not vocal_url or not background_url:
                    degraded.append("background_separation_unclassified")
            except (AliyunICEError, AliyunOSSError, AliyunMediaError) as exc:
                degraded.append("background_separation")
                metadata["background_separation_error"] = str(exc)

        return MediaArtifacts(
            video_url=video_url,
            background_url=background_url,
            vocal_url=vocal_url,
            source_audio_url=source_audio_url,
            provider="aliyun_ice",
            task_ids=task_ids,
            metadata=metadata,
            degraded_features=degraded,
        )

    async def _extract_audio(self, video_url: str) -> tuple[str, str]:
        operation_id = uuid.uuid4().hex
        object_key = self.store.key("media", operation_id, "source-audio.wav")
        output_url = self.store.canonical_url(object_key)
        job_id = await self.ice.submit_media_producing(
            timeline=build_audio_extract_timeline(video_url),
            output_media_url=output_url,
            output_config=None,
            client_token=f"audio-{operation_id}",
            user_data={"operation": "audio_extract"},
        )
        result = await self.ice.wait_media_producing(job_id)
        media_url = str(result.get("MediaURL", "") or output_url)
        return media_url, job_id

    async def _demix(
        self,
        source_audio_url: str,
    ) -> tuple[str | None, str | None, str, list[dict[str, str]]]:
        operation_id = uuid.uuid4().hex
        output_pattern = self.store.oss_uri(
            self.store.key("media", operation_id, "demix-{resultType}.wav")
        )
        job_id = await self.ice.submit_i_production(
            name=f"cineflow-music-demix-{operation_id[:12]}",
            function_name="MusicDemix",
            input_media=source_audio_url,
            output_media=output_pattern,
            client_token=f"demix-{operation_id}",
        )
        result = await self.ice.wait_i_production(job_id)
        candidates = collect_i_production_outputs(result, self.store)
        vocal, background = classify_demix_outputs(candidates)
        return (
            vocal,
            background,
            job_id,
            [{"label": label, "url": url} for label, url in candidates],
        )

    async def assemble(
        self,
        request: JobRequest,
        media: MediaArtifacts,
        translated: Transcript,
        dubbing: DubbingArtifact,
    ) -> OutputArtifact:
        width, height = output_dimensions(request)
        video_url = self.store.canonicalize_oss_input(media.video_url)
        background_url = (
            self.store.canonicalize_oss_input(media.background_url)
            if media.background_url
            else None
        )
        timeline = build_assembly_timeline(
            video_url=video_url,
            translated=translated,
            dubbing=dubbing,
            background_url=background_url,
            subtitle_mode=request.subtitle_mode,
            width=width,
            height=height,
            config=self.config,
        )

        operation_id = uuid.uuid4().hex
        output_key = self.store.key("outputs", operation_id, "result.mp4")
        output_url = self.store.canonical_url(output_key)
        output_config = {
            "Bitrate": min(5000, max(256, self.config.output_bitrate_kbps)),
            "Width": width,
            "Height": height,
        }
        try:
            job_id = await self.ice.submit_media_producing(
                timeline=timeline,
                output_media_url=output_url,
                output_config=output_config,
                client_token=f"assemble-{operation_id}",
                user_data={"operation": "assemble"},
            )
            result = await self.ice.wait_media_producing(job_id)
        except AliyunICEError as exc:
            raise AliyunMediaError(str(exc)) from exc

        final_url = str(result.get("MediaURL", "") or output_url)
        subtitle_url = None
        subtitle_object_key = None
        if request.subtitle_mode != "none":
            subtitle_object_key = self.store.key(
                "outputs",
                operation_id,
                f"{translated.language or 'translated'}.srt",
            )
            try:
                await __import__("asyncio").to_thread(
                    self.store.put_bytes,
                    subtitle_object_key,
                    transcript_to_srt(translated).encode("utf-8"),
                    content_type="application/x-subrip; charset=utf-8",
                )
                subtitle_url = self.store.signed_url(subtitle_object_key)
            except AliyunOSSError as exc:
                raise AliyunMediaError(str(exc)) from exc

        return OutputArtifact(
            video_url=self.store.sign_if_owned(final_url),
            subtitle_url=subtitle_url,
            provider="aliyun_ice",
            task_id=job_id,
            metadata={
                "canonical_video_url": final_url,
                "output_object_key": output_key,
                "subtitle_object_key": subtitle_object_key,
                "duration_seconds": result.get("Duration"),
                "width": width,
                "height": height,
                "timeline_audio_tracks": len(timeline.get("AudioTracks", [])),
                "hard_subtitles": request.subtitle_mode == "hard",
            },
        )
