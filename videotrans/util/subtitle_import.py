import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List


_TIMECODE_RE = re.compile(
    r"\d{1,3}:\d{2}:\d{2}(?:[,.]\d+)?\s*-->\s*"
    r"\d{1,3}:\d{2}:\d{2}(?:[,.]\d+)?"
)
_TRAILING_SUBTITLE_TAG_RE = re.compile(
    r"(?:[\s._-]+(?:"
    r"zh(?:[-_](?:cn|hans|hant|tw))?|chs|cht|sc|tc|cn|"
    r"subtitle|subtitles|translated|sub|srt|字幕|中文字幕|简体|繁体|"
    r"[a-z]{2,3}(?:[-_][a-z]{2,4})?"
    r"))+$",
    re.IGNORECASE,
)
_GENERATED_VIDEO_SUFFIX_RE = re.compile(
    r"(?:[\W_]+trimmed(?:[\W_]+\d+)?)$",
    re.IGNORECASE | re.UNICODE,
)
_LANGUAGE_EPISODE_RE = re.compile(
    r"(?:^|[\W_])([a-z]{2,12})(?:[\W_]+)(\d+)$",
    re.IGNORECASE | re.UNICODE,
)


def normalized_path_key(path) -> str:
    """Return a stable key for matching a task path to an imported subtitle."""
    return Path(path).resolve(strict=False).as_posix().casefold()


def read_timed_subtitle(path) -> str:
    """Read an SRT-like file and require real timestamps for speaker alignment."""
    subtitle_path = Path(path)
    content = None
    last_error = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            content = subtitle_path.read_text(encoding=encoding).strip()
            break
        except UnicodeDecodeError as exc:
            last_error = exc
    if content is None:
        raise ValueError(str(last_error or "Unable to decode subtitle file"))
    if not content or not _TIMECODE_RE.search(content):
        raise ValueError("Subtitle file has no valid SRT timestamps")
    return content


def _normalized_stem(path) -> str:
    stem = _strip_generated_video_suffixes(
        unicodedata.normalize("NFKC", Path(path).stem).casefold().strip()
    )
    previous = None
    while stem != previous:
        previous = stem
        stem = _TRAILING_SUBTITLE_TAG_RE.sub("", stem).strip()
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", stem)


def _strip_generated_video_suffixes(stem: str) -> str:
    previous = None
    while stem != previous:
        previous = stem
        stem = _GENERATED_VIDEO_SUFFIX_RE.sub("", stem)
    return stem


def _language_episode_key(path):
    stem = _strip_generated_video_suffixes(
        unicodedata.normalize("NFKC", Path(path).stem).casefold()
    )
    match = _LANGUAGE_EPISODE_RE.search(stem)
    if not match:
        return None
    return match.group(1), int(match.group(2))


def _episode_key(path) -> str:
    language_episode = _language_episode_key(path)
    if language_episode:
        return str(language_episode[1])
    stem = _strip_generated_video_suffixes(
        unicodedata.normalize("NFKC", Path(path).stem).casefold()
    )
    patterns = (
        r"第\s*0*(\d+)\s*[集话回]",
        r"(?:^|[^a-z0-9])s\d{1,3}[._ -]*e(?:p)?[._ -]*0*(\d+)(?:[^0-9]|$)",
        r"(?:^|[^a-z0-9])e(?:p)?[._ -]*0*(\d+)(?:[^0-9]|$)",
        r"(?:^|[^0-9])0*(\d+)(?:[^0-9]*)$",
    )
    for pattern in patterns:
        match = re.search(pattern, stem, re.IGNORECASE)
        if match:
            return str(int(match.group(1)))
    return ""


@dataclass
class SubtitleMatchResult:
    mapping: Dict[str, str] = field(default_factory=dict)
    unmatched_videos: List[str] = field(default_factory=list)
    unmatched_subtitles: List[str] = field(default_factory=list)
    ambiguous_videos: List[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.unmatched_videos and not self.ambiguous_videos

    @property
    def safe_to_run(self) -> bool:
        """Missing subtitles can fall back to ASR; ambiguity must be resolved."""
        return not self.ambiguous_videos


def match_subtitles_to_videos(
        videos: Iterable[str], subtitles: Iterable[str]) -> SubtitleMatchResult:
    """Match subtitles conservatively: normalized filename first, episode key second."""
    video_list = [str(path) for path in videos]
    subtitle_list = [str(path) for path in subtitles]
    result = SubtitleMatchResult()

    if len(video_list) == 1 and len(subtitle_list) == 1:
        result.mapping[normalized_path_key(video_list[0])] = subtitle_list[0]
        return result

    unused = set(range(len(subtitle_list)))
    pending = []
    subtitle_stems = [_normalized_stem(path) for path in subtitle_list]

    for video in video_list:
        video_stem = _normalized_stem(video)
        candidates = [
            index for index in unused
            if video_stem and subtitle_stems[index] == video_stem
        ]
        if len(candidates) == 1:
            index = candidates[0]
            result.mapping[normalized_path_key(video)] = subtitle_list[index]
            unused.remove(index)
        elif len(candidates) > 1:
            result.ambiguous_videos.append(video)
        else:
            pending.append(video)

    episode_pending = []
    for video in pending:
        language_episode = _language_episode_key(video)
        candidates = [
            index for index in unused
            if language_episode
            and _language_episode_key(subtitle_list[index]) == language_episode
        ]
        if len(candidates) == 1:
            index = candidates[0]
            result.mapping[normalized_path_key(video)] = subtitle_list[index]
            unused.remove(index)
        elif len(candidates) > 1:
            result.ambiguous_videos.append(video)
        else:
            episode_pending.append(video)

    for video in episode_pending:
        episode = _episode_key(video)
        video_language_episode = _language_episode_key(video)
        language_candidates_for_episode = [
            index for index in unused
            if _language_episode_key(subtitle_list[index])
            and _episode_key(subtitle_list[index]) == episode
        ]
        # If both sides carry language tags, never silently cross languages.
        if video_language_episode and language_candidates_for_episode:
            result.unmatched_videos.append(video)
            continue
        candidates = [
            index for index in unused
            if episode and _episode_key(subtitle_list[index]) == episode
        ]
        if len(candidates) == 1:
            index = candidates[0]
            result.mapping[normalized_path_key(video)] = subtitle_list[index]
            unused.remove(index)
        elif len(candidates) > 1:
            result.ambiguous_videos.append(video)
        else:
            result.unmatched_videos.append(video)

    result.unmatched_subtitles = [subtitle_list[index] for index in sorted(unused)]
    return result
