import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable, List

from videotrans.configure.config import ROOT_DIR, params


@dataclass(frozen=True)
class VoiceEntry:
    role: str
    name: str
    provider: str
    voice_id: str = ""
    gender: str = "未知"
    age: str = "未知"
    locale: str = ""
    region: str = ""
    description: str = ""
    tags: List[str] = field(default_factory=list)
    previewable: bool = True

    @property
    def search_text(self) -> str:
        return " ".join(
            [
                self.role,
                self.name,
                self.provider,
                self.voice_id,
                self.gender,
                self.age,
                self.locale,
                self.region,
                self.description,
                *self.tags,
            ]
        ).lower()


@lru_cache(maxsize=1)
def _edge_tags() -> dict:
    path = Path(ROOT_DIR) / "videotrans" / "voicejson" / "edge_voice_tags.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


@lru_cache(maxsize=1)
def _edge_tags_by_voice_id() -> dict:
    return {
        str(metadata["voice_id"]): metadata
        for metadata in _edge_tags().values()
        if metadata.get("voice_id")
    }


def _provider_name(tts_type: int) -> str:
    from videotrans import tts

    if 0 <= int(tts_type) < len(tts.TTS_NAME_LIST):
        return tts.TTS_NAME_LIST[int(tts_type)]
    return f"TTS-{tts_type}"


def _infer_gender(role: str) -> str:
    lowered = role.lower()
    if "female" in lowered or "女声" in role:
        return "女声"
    if "male" in lowered or "男声" in role:
        return "男声"
    if "neutral" in lowered or "中性" in role:
        return "中性"
    return "未知"


def _display_name(role: str) -> str:
    name = re.split(r"[（(]", role, maxsplit=1)[0].strip()
    return name or role


def _voice_id_and_locale(tts_type: int, role: str, language: str) -> tuple[str, str]:
    from videotrans import tts
    from videotrans.util import tools

    voice_id = ""
    lang = (language or "").split("-")[0]
    if tts_type == tts.EDGE_TTS:
        voice_id = tools.get_edge_rolelist(role_name=role, locale=language) or ""
    elif tts_type == tts.AZURE_TTS:
        voice_id = tools.get_azure_rolelist(lang, role) or ""
    if not voice_id and re.match(r"^[a-z]{2,3}-[A-Z]{2}-", role):
        voice_id = role
    locale_match = re.match(r"^([a-z]{2,3}-[A-Z]{2})-", voice_id)
    locale = locale_match.group(1) if locale_match else (language or "")
    return voice_id, locale


def build_voice_entries(
    tts_type: int,
    roles: Iterable[str],
    language: str = "",
) -> List[VoiceEntry]:
    from videotrans import tts

    provider = _provider_name(tts_type)
    result = []
    seen = set()
    for raw_role in roles:
        role = str(raw_role or "").strip()
        if not role or role in seen:
            continue
        seen.add(role)
        if role in {"No", "-"}:
            result.append(
                VoiceEntry(
                    role=role,
                    name="不使用配音",
                    provider=provider,
                    description="保留当前内容，不生成配音",
                    tags=["关闭配音"],
                    previewable=False,
                )
            )
            continue

        voice_id, locale = _voice_id_and_locale(tts_type, role, language)
        metadata = _edge_tags().get(role, {})
        if not metadata and voice_id:
            metadata = _edge_tags_by_voice_id().get(voice_id, {})
        name = metadata.get("name") or _display_name(role)
        gender = metadata.get("gender") or _infer_gender(role)
        age = metadata.get("age") or "未知"
        region = metadata.get("region") or ""
        tags = [tag for tag in (gender, age, locale, region) if tag and tag != "未知"]
        lowered = f"{role} {voice_id}".lower()
        if "multilingual" in lowered:
            tags.append("多语言")
        if "dragon" in lowered or "hd" in lowered:
            tags.append("HD")
        if role.lower() == "clone":
            tags.append("克隆音色")

        description_bits = [provider]
        if locale:
            description_bits.append(locale)
        if region:
            description_bits.append(region)
        result.append(
            VoiceEntry(
                role=role,
                name=name,
                provider=provider,
                voice_id=metadata.get("voice_id") or voice_id,
                gender=gender,
                age=age,
                locale=locale,
                region=region,
                description=" · ".join(description_bits),
                tags=list(dict.fromkeys(tags)),
                previewable=role.lower() != "clone",
            )
        )
    return result


def get_voice_favorites(tts_type: int) -> set[str]:
    data = getattr(params, "voice_favorites", {})
    if not isinstance(data, dict):
        return set()
    values = data.get(str(int(tts_type)), [])
    return {str(value) for value in values} if isinstance(values, list) else set()


def set_voice_favorite(tts_type: int, role: str, favorite: bool) -> set[str]:
    data = getattr(params, "voice_favorites", {})
    if not isinstance(data, dict):
        data = {}
    else:
        data = {key: list(value) for key, value in data.items() if isinstance(value, list)}
    key = str(int(tts_type))
    values = {str(value) for value in data.get(key, [])}
    if favorite:
        values.add(role)
    else:
        values.discard(role)
    data[key] = sorted(values)
    params["voice_favorites"] = data
    params.save()
    return values
