import base64
import json
import time
import uuid
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import requests

from videotrans.configure.config import logger, params
from videotrans.configure.excepts import SpeechToTextError
from videotrans.util import tools


SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
RESOURCE_ID = "volc.bigasr.auc_turbo"
UPLOAD_TIMEOUT_SECONDS = 60
RESPONSE_TIMEOUT_SECONDS = 120
MAX_ATTEMPTS = 3
WAV_COMPRESSION_THRESHOLD_BYTES = 1024 * 1024

_ERROR_MESSAGES = {
    "20000003": "静音音频",
    "45000001": "请求参数缺失或字段值无效",
    "45000002": "音频为空",
    "45000151": "音频格式不正确",
    "55000031": "火山极速识别服务繁忙",
}


def credentials_configured() -> bool:
    api_key = str(params.get("zijierecognmodel_apikey", "") or "").strip()
    app_id = str(params.get("zijierecognmodel_appid", "") or "").strip()
    access_token = str(params.get("zijierecognmodel_token", "") or "").strip()
    return bool(api_key or (app_id and access_token))


def build_headers(request_id: str) -> Dict[str, str]:
    headers = {
        "X-Api-Resource-Id": RESOURCE_ID,
        "X-Api-Request-Id": request_id,
        "X-Api-Sequence": "-1",
    }
    api_key = str(params.get("zijierecognmodel_apikey", "") or "").strip()
    if api_key:
        headers["X-Api-Key"] = api_key
        return headers

    app_id = str(params.get("zijierecognmodel_appid", "") or "").strip()
    access_token = str(params.get("zijierecognmodel_token", "") or "").strip()
    if not app_id or not access_token:
        raise SpeechToTextError(
            "火山极速角色识别尚未配置。请打开“语音识别-字节语音大模型极速版”，"
            "填写新版 API Key，或旧版 AppID 和 Access Token。"
        )
    headers["X-Api-App-Key"] = app_id
    headers["X-Api-Access-Key"] = access_token
    return headers


def _normalize_speaker(raw_value) -> str:
    value = str(raw_value if raw_value is not None else "0").strip().lower()
    for prefix in ("speaker_", "speaker", "spk_"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    return f"spk{value or '0'}"


def extract_utterances(result: Dict) -> List[Dict]:
    utterances = result.get("result", {}).get("utterances") or []
    output = []
    for item in utterances:
        try:
            start = int(item["start_time"])
            end = int(item["end_time"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        additions = item.get("additions") or {}
        output.append({
            "start_time": start,
            "end_time": end,
            "speaker": _normalize_speaker(additions.get("speaker", 0)),
            "text": str(item.get("text", "") or "").strip(),
        })
    return output


def prepare_audio_for_upload(audio_path: Path) -> Path:
    """Compress sizeable WAV input so slow uplinks do not stall JSON uploads."""
    if (
            audio_path.suffix.lower() != ".wav"
            or audio_path.stat().st_size < WAV_COMPRESSION_THRESHOLD_BYTES
    ):
        return audio_path

    compressed_path = audio_path.with_name(
        f"{audio_path.stem}-volcengine-16k-64k.mp3"
    )
    if (
            compressed_path.is_file()
            and compressed_path.stat().st_size > 0
            and compressed_path.stat().st_mtime_ns >= audio_path.stat().st_mtime_ns
    ):
        return compressed_path

    temporary_path = audio_path.with_name(
        f"{audio_path.stem}-{uuid.uuid4().hex}.partial.mp3"
    )
    try:
        tools.runffmpeg([
            "-y",
            "-i", audio_path.as_posix(),
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            "-c:a", "libmp3lame",
            "-b:a", "64k",
            "-map_metadata", "-1",
            temporary_path.as_posix(),
        ], force_cpu=True)
        if not temporary_path.is_file() or temporary_path.stat().st_size <= 0:
            raise SpeechToTextError("火山极速识别上传音频压缩后为空")
        temporary_path.replace(compressed_path)
    except Exception as error:
        temporary_path.unlink(missing_ok=True)
        raise SpeechToTextError(f"火山极速识别上传音频压缩失败：{error}") from error

    logger.info(
        "火山极速识别上传前压缩音频：WAV %.2f MB -> MP3 %.2f MB",
        audio_path.stat().st_size / 1024 / 1024,
        compressed_path.stat().st_size / 1024 / 1024,
    )
    return compressed_path


def request_transcription(
        audio_file: str,
        *,
        upload_timeout_seconds: int = UPLOAD_TIMEOUT_SECONDS,
        response_timeout_seconds: int = RESPONSE_TIMEOUT_SECONDS,
        attempts: int = MAX_ATTEMPTS,
) -> Tuple[Dict, str]:
    audio_path = Path(audio_file)
    if not audio_path.is_file() or audio_path.stat().st_size <= 0:
        raise SpeechToTextError(f"火山极速识别音频不存在或为空：{audio_path.name}")
    upload_path = prepare_audio_for_upload(audio_path)

    app_id = str(params.get("zijierecognmodel_appid", "") or "").strip()
    payload = {
        "user": {"uid": app_id or "pyvideotrans"},
        "audio": {
            "data": base64.b64encode(upload_path.read_bytes()).decode("ascii"),
            "format": upload_path.suffix.lstrip(".").lower(),
        },
        "request": {
            "model_name": "bigmodel",
            "model_version": "400",
            "enable_itn": True,
            "enable_punc": True,
            "enable_ddc": True,
            "show_utterances": True,
            "enable_speaker_info": True,
        },
    }

    last_error = None
    attempt_count = max(1, int(attempts))
    session = requests.Session()
    # 火山接口在国内可直连。禁用 requests 对系统代理环境变量的自动继承，
    # 避免远程电脑残留的 HTTP(S)_PROXY 干扰大文件上传。
    session.trust_env = False
    try:
        for attempt in range(attempt_count):
            request_id = str(uuid.uuid4())
            try:
                response = session.post(
                    SUBMIT_URL,
                    json=payload,
                    headers=build_headers(request_id),
                    timeout=(
                        max(10, int(upload_timeout_seconds)),
                        max(10, int(response_timeout_seconds)),
                    ),
                )
                response.raise_for_status()
                code = str(response.headers.get("X-Api-Status-Code", ""))
                trace_id = str(response.headers.get("X-Tt-Logid", "") or "")
                if code == "20000000":
                    return response.json(), trace_id
                message = response.headers.get("X-Api-Message") or _ERROR_MESSAGES.get(
                    code, f"火山极速识别错误码 {code or 'unknown'}"
                )
                last_error = SpeechToTextError(str(message))
                if not code.startswith("55"):
                    break
            except (requests.Timeout, requests.ConnectionError) as error:
                last_error = error
            except requests.HTTPError as error:
                last_error = error
                status_code = getattr(error.response, "status_code", 0) or 0
                if status_code < 500:
                    break
            if attempt + 1 < attempt_count:
                delay_seconds = 1.5 * (attempt + 1)
                logger.warning(
                    "火山极速识别上传或请求暂时失败，第 %s/%s 次，%.1f 秒后重试：%s",
                    attempt + 1, attempt_count, delay_seconds, last_error,
                )
                time.sleep(delay_seconds)
    finally:
        session.close()

    logger.error(
        "火山极速识别失败，已尝试 %s 次；鉴权信息未写入日志",
        attempt_count,
    )
    raise SpeechToTextError(f"火山极速识别失败：{last_error}") from last_error


def map_speakers_to_subtitles(
        subtitles: Iterable,
        utterances: Iterable[Dict],
) -> List[str]:
    subtitle_list = list(subtitles)
    utterance_list = list(utterances)
    raw_speakers = []
    for item in utterance_list:
        speaker = item["speaker"]
        if speaker not in raw_speakers:
            raw_speakers.append(speaker)
    speaker_map = {speaker: f"spk{index}" for index, speaker in enumerate(raw_speakers)}

    labels = []
    for subtitle in subtitle_list:
        if isinstance(subtitle, dict):
            start = int(subtitle.get("start_time", 0))
            end = int(subtitle.get("end_time", 0))
        else:
            start, end = int(subtitle[0]), int(subtitle[1])
        overlaps: Dict[str, int] = {}
        for utterance in utterance_list:
            overlap = max(
                0,
                min(end, utterance["end_time"]) - max(start, utterance["start_time"]),
            )
            if overlap:
                speaker = utterance["speaker"]
                overlaps[speaker] = overlaps.get(speaker, 0) + overlap
        if overlaps:
            raw_speaker = max(overlaps, key=overlaps.get)
            labels.append(speaker_map.get(raw_speaker, "spk0"))
        else:
            labels.append("spk0")
    return labels


def volcengine_flash_speakers(
        *,
        input_file: str,
        subtitles_file: str,
        speak_file: str,
        num_speakers: int = -1,
        is_cuda: bool = False,
        logs_file: str = None,
        device_index: int = 0,
):
    del num_speakers, is_cuda, logs_file, device_index
    try:
        subtitles = json.loads(Path(subtitles_file).read_text(encoding="utf-8"))
        response, trace_id = request_transcription(input_file)
        utterances = extract_utterances(response)
        if not utterances:
            return False, "火山极速识别未返回说话人分段"
        labels = map_speakers_to_subtitles(subtitles, utterances)
        if not labels:
            return False, "火山极速识别无法映射到现有字幕"

        speaker_path = Path(speak_file)
        speaker_path.write_text(json.dumps(labels, ensure_ascii=False), encoding="utf-8")
        diagnostic = {
            "provider": "volcengine_flash",
            "resource_id": RESOURCE_ID,
            "trace_id": trace_id,
            "speaker_count": len(set(labels)),
            "utterances": utterances,
        }
        speaker_path.with_name(f"{speaker_path.stem}.volcengine.json").write_text(
            json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info(
            "火山极速说话人识别完成：%s 个角色，%s 条字幕",
            len(set(labels)), len(labels),
        )
        return True, None
    except Exception as error:
        logger.exception("火山极速说话人识别失败", exc_info=True)
        return False, str(error)
