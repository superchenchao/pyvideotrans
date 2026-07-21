import json
import re
from typing import List

import requests
from urllib.parse import urlsplit, urlunsplit


from videotrans.configure.config import ROOT_DIR, tr, settings, params, logger
from pathlib import Path
from functools import lru_cache

from videotrans.configure import contants



def get_camb_role(force=False, raise_exception=False):
    from . import help_misc
    jsonfile = f'{ROOT_DIR}/videotrans/voicejson/camb.json'
    namelist = ["No", "clone"]
    if help_misc.vail_file(jsonfile):
        with open(jsonfile, 'r', encoding='utf-8-sig') as f:
            cache = json.loads(f.read())
            for it in cache.values():
                name = it.get('voice_name', it.get('name', ''))
                if name:
                    namelist.append(name)
    if not force and len(namelist) > 2:
        params['camb_role'] = namelist
        return namelist
    try:
        from camb.client import CambAI
        import os
        client = CambAI(api_key=params.get("camb_api_key", '') or os.environ.get('CAMB_API_KEY', ''))
        voiceslist = client.voice_cloning.list_voices()

        result = {}
        for it in voiceslist:
            voice_id = it.id if hasattr(it, 'id') else it.get('id')
            voice_name = it.voice_name if hasattr(it, 'voice_name') else it.get('voice_name', '')
            n = re.sub(r'[^a-zA-Z0-9_ -]+', '', voice_name, flags=re.I | re.S).strip()
            if n:
                result[n] = {"name": n, "voice_name": n, "id": voice_id}
        namelist = ['No', 'clone'] + list(result.keys())
        with open(jsonfile, 'w', encoding="utf-8") as f:
            f.write(json.dumps(result))
        params['camb_role'] = namelist
        return namelist
    except Exception as e:
        logger.exception(f'Failed to get CAMB AI voices: {e}', exc_info=True)
        if raise_exception:
            raise
    return []


def get_elevenlabs_role(force=False, raise_exception=False):
    from . import help_misc
    jsonfile = f'{ROOT_DIR}/videotrans/voicejson/elevenlabs.json'
    namelist = ["No"]
    if help_misc.vail_file(jsonfile):
        with open(jsonfile, 'r', encoding='utf-8-sig') as f:
            cache = json.loads(f.read())
            for it in cache.values():
                namelist.append(it['name'])
    if not force and len(namelist) > 0:
        params['elevenlabstts_role'] = namelist
        return namelist
    try:
        from elevenlabs import ElevenLabs
        client = ElevenLabs(api_key=params.get("elevenlabstts_key", ''))
        voiceslist = client.voices.get_all()

        namelist = ['No']
        result = {}
        for it in voiceslist.voices:
            n = re.sub(r'[^a-zA-Z0-9_ -]+', '', it.name, flags=re.I | re.S).strip()
            result[n] = {"name": n, "voice_id": it.voice_id}
            namelist.append(n)

        with open(jsonfile, 'w', encoding="utf-8") as f:
            f.write(json.dumps(result))
        params['elevenlabstts_role'] = namelist
        return namelist
    except Exception as e:
        logger.exception(f'获取 elevenlabs 角色失败:{e}', exc_info=True)
        if raise_exception:
            raise
    return []


def get_vits_role():
    zh = ['No', "zh_female"]
    en = ['No', "en_female"]
    for i in range(109):
        en.append(f'en_{i}')
    for i in range(174):
        zh.append(f'zh_{i}')

    return {"zh": {k: k for k in zh}, "en": {k: k for k in en}}


def get_piper_role():
    file_path = f"{ROOT_DIR}/videotrans/voicejson/piper.json"
    if Path(file_path).exists():
        rolelist = json.loads(Path(file_path).read_text(encoding='utf-8-sig'))
    else:
        rolelist = {}
        from videotrans.translator import LANGNAME_DICT
        langkeys = [it.split('-')[0] for it in LANGNAME_DICT.keys()]
        for it in Path(f'{ROOT_DIR}/models/piper').rglob('*.onnx'):
            rolename = Path(it).stem
            tmp = rolename.split('_')  # tmp[0] 语言代码
            if tmp[0] not in langkeys:
                continue
            if tmp[0] not in rolelist:
                rolelist[tmp[0]] = {"No": "No"}
            rolelist[tmp[0]][rolename] = rolename
        Path(file_path).write_text(json.dumps(rolelist, indent=4), encoding='utf-8')
    return rolelist


def get_302ai():
    role_dict = get_azure_rolelist()

    with open(ROOT_DIR + "/videotrans/voicejson/302.json", 'r', encoding='utf-8-sig') as f:
        ai302_voice_roles = json.loads(f.read())
        _doubao = ai302_voice_roles.get("AI302_doubao", {})
        _minimaxi = ai302_voice_roles.get("AI302_minimaxi", {})
        _dubbingx = ai302_voice_roles.get("AI302_dubbingx", {})
        _doubao_ja = ai302_voice_roles.get("AI302_doubao_ja", {})
    _openai = contants.OPENAITTS_ROLES.split(",")
    role_dict['zh'] = role_dict['zh'] | _doubao | _minimaxi | _dubbingx | {k: k for k in _openai}
    role_dict['ja'] = role_dict['ja'] | _doubao_ja
    return role_dict


def get_doubao2_rolelist(role_name=None, langcode="zh"):
    roledata = json.loads(Path(f'{ROOT_DIR}/videotrans/voicejson/doubao2.json').read_text(encoding='utf-8-sig'))

    if role_name:
        current_d = roledata.get(langcode[:2])
        if not current_d:
            return 'No'
        return current_d.get(role_name)

    return {key: ['No'] + list(item.keys()) for key, item in roledata.items()}


#  get role by edge tts
@lru_cache(maxsize=None)
def get_edge_rolelist(role_name=None, locale=None):
    from . import help_misc
    voice_list = {}
    voice_file = ROOT_DIR + "/videotrans/voicejson/edge_tts.json"
    if help_misc.vail_file(voice_file):
        try:
            with open(voice_file, 'r', encoding='utf-8-sig') as f:
                voice_list = json.loads(f.read())
            for i, it in voice_list.items():
                voice_list[i] = {"No": "No"} | it
        except (OSError, json.JSONDecodeError):
            pass
    if role_name and locale:
        return voice_list.get(locale.split('-')[0], {}).get(role_name)
    return voice_list


@lru_cache(maxsize=1)
def _get_static_azure_rolelist():
    voice_file = Path(ROOT_DIR) / "videotrans" / "voicejson" / "azure_voice_list.json"
    try:
        return json.loads(voice_file.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def _azure_voice_list_url(region_or_endpoint):
    value = str(region_or_endpoint or "").strip().rstrip("/")
    if not value:
        return ""
    if not value.lower().startswith(("http://", "https://")):
        return (
            f"https://{value.lower()}.tts.speech.microsoft.com/"
            "cognitiveservices/voices/list"
        )

    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if host.endswith(".api.cognitive.microsoft.com"):
        region = host.split(".", 1)[0]
        return (
            f"https://{region}.tts.speech.microsoft.com/"
            "cognitiveservices/voices/list"
        )
    base_path = parsed.path.rstrip("/")
    if base_path.endswith("/cognitiveservices/voices/list"):
        path = base_path
    else:
        path = f"{base_path}/cognitiveservices/voices/list"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


@lru_cache(maxsize=4)
def _get_azure_region_voices(subscription, region_or_endpoint):
    url = _azure_voice_list_url(region_or_endpoint)
    if not subscription or not url:
        return []
    response = requests.get(
        url,
        headers={"Ocp-Apim-Subscription-Key": subscription},
        timeout=(5, 15),
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def _azure_locale_matches(candidate, language):
    candidate = str(candidate or "").strip().lower().replace("_", "-")
    language = str(language or "").strip().lower().replace("_", "-")
    if not candidate or not language:
        return False
    if "-" in language:
        return candidate == language
    return candidate.split("-", 1)[0] == language


def _format_azure_role(voice):
    display_name = str(voice.get("DisplayName") or "").strip()
    short_name = str(voice.get("ShortName") or voice.get("Name") or "").strip()
    gender = str(voice.get("Gender") or "").strip()
    if not display_name or not short_name:
        return None, None
    role = f"{display_name}({gender})" if gender else display_name
    return role, short_name


def _static_azure_roles_for_language(language):
    voice_list = _get_static_azure_rolelist()
    language = str(language or "").strip().lower().replace("_", "-")
    roles = voice_list.get(language) or voice_list.get(language.split("-", 1)[0]) or {}
    return {"No": "No"} | dict(roles)


def get_azure_supported_rolelist(language):
    """Return regional Azure voices whose primary or secondary locale matches."""
    subscription = str(params.get("azure_speech_key", "") or "").strip()
    region_or_endpoint = str(params.get("azure_speech_region", "") or "").strip()
    if not subscription or not region_or_endpoint:
        return _static_azure_roles_for_language(language)

    try:
        voices = _get_azure_region_voices(subscription, region_or_endpoint)
        roles = {}
        for voice in voices:
            if not isinstance(voice, dict):
                continue
            secondary_locales = voice.get("SecondaryLocaleList") or []
            if isinstance(secondary_locales, str):
                secondary_locales = [secondary_locales]
            locales = [voice.get("Locale"), *secondary_locales]
            if not any(_azure_locale_matches(locale, language) for locale in locales):
                continue
            role, short_name = _format_azure_role(voice)
            if role and short_name:
                roles[role] = short_name
        if roles:
            return {"No": "No"} | roles
    except (requests.RequestException, ValueError, TypeError) as exc:
        logger.warning(f"Failed to refresh Azure voice list, using local catalog: {exc}")
    return _static_azure_roles_for_language(language)


def get_azure_rolelist(language=None, role_name=None):
    voice_list = _get_static_azure_rolelist()
    # 根据角色显示名字获取真实角色
    if language and role_name:
        language_roles = voice_list.get(language, {}) or voice_list.get(
            str(language).split('-')[0], {})
        resolved = language_roles.get(role_name)
        if resolved:
            return resolved
        # Multilingual voices can have a different primary locale, so the
        # selected target language bucket is not sufficient for resolution.
        for roles in voice_list.values():
            resolved = roles.get(role_name)
            if resolved:
                return resolved
        return get_azure_supported_rolelist(language).get(role_name)
    if role_name and (not language or language == 'auto'):
        for it in voice_list.values():
            for name, ro in it.items():
                if name == role_name:
                    return ro
        return None
    try:
        voice_list = {
            key: {"No": "No"} | dict(roles)
            for key, roles in voice_list.items()
        }
    except (OSError, json.JSONDecodeError):
        pass
    return voice_list


def get_minimaxi_rolelist():
    from . import help_misc
    voice_list = {}
    voice_file = ROOT_DIR + "/videotrans/voicejson/minimaxi.json"

    if params.get("minimaxi_apiurl", '') == 'api.minimax.io':
        voice_file = ROOT_DIR + "/videotrans/voicejson/minimaxiio.json"
    if help_misc.vail_file(voice_file):
        try:
            with open(voice_file, 'r', encoding='utf-8-sig') as f:
                voice_list = json.loads(f.read())
            for i, it in voice_list.items():
                voice_list[i] = {"No": "No"} | it
        except (OSError, json.JSONDecodeError):
            pass
    return voice_list


def get_qwen3tts_rolelist():
    voices = json.loads(Path(ROOT_DIR + "/videotrans/voicejson/qwen3tts.json").read_text(encoding='utf-8-sig'))
    voices = {"No": "No"} | voices
    return voices


# 本地qwentts3
def get_qwenttslocal_rolelist():
    voices = {
        "Vivian": "Vivian",
        "Serena": "Serena",
        "Uncle_fu": "Uncle_fu",
        "Dylan": "Dylan",
        "Eric": "Eric",
        "Ryan": "Ryan",
        "Aiden": "Aiden",
        "Ono_anna": "Ono_anna",
        "Sohee": "Sohee"
    }
    return get_f5tts_role() | voices


def get_supertonic_rolelist():
    voices = json.loads(Path(ROOT_DIR + "/videotrans/voicejson/supertonic.json").read_text(encoding='utf-8-sig'))
    voices = {"No": "No"} | voices
    return voices


def get_glmtts_rolelist():
    voices = json.loads(Path(ROOT_DIR + "/videotrans/voicejson/glmtts.json").read_text(encoding='utf-8-sig'))
    voices = {"No": "No"} | voices
    return voices


def get_kokoro_rolelist():
    voice_list = {
        "en": [
            "No",
            "af_alloy",
            "af_aoede",
            "af_bella",
            "af_jessica",
            "af_kore",
            "af_nicole",
            "af_nova",
            "af_river",
            "af_sarah",
            "af_sky",
            "am_adam",
            "am_echo",
            "am_eric",
            "am_fenrir",
            "am_liam",
            "am_michael",
            "am_onyx",
            "am_puck",
            "am_santa",
            "bf_alice",
            "bf_emma",
            "bf_isabella",
            "bf_lily",
            "bm_daniel",
            "bm_fable",
            "bm_george",
            "bm_lewis"
        ],
        "zh": ["No", "zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao", "zf_xiaoyi", "zm_yunjian", "zm_yunxi", "zm_yunxia",
               "zm_yunyang"],
        "ja": ["No", "jf_alpha", "jf_gongitsune", "jf_nezumi", "jf_tebukuro", "jm_kumo"],
        "fr": ["No", "ff_siwis"],
        "it": ["No", "if_sara", "im_nicola"],
        "hi": ["No", "hf_alpha", "hf_beta", "hm_omega", "hm_psi"],
        "es": ["No", "ef_dora", "em_alex", "em_santa"],
        "pt": ["No", "pf_dora", "pm_alex", "pm_santa"]
    }

    return voice_list


# 根据 gptsovits params['gptsovits_role'] 返回以参考音频为key的dict
def get_gptsovits_role():
    if not params.get('gptsovits_role', '').strip():
        return None
    rolelist = {"No": "No", "clone": "clone"}
    for it in params.get('gptsovits_role', '').strip().split("\n"):
        tmp = it.strip().split('#')
        if len(tmp) != 3:
            continue
        rolelist[tmp[0]] = {"refer_wav_path": tmp[0], "prompt_text": tmp[1], "prompt_language": tmp[2]}
    return rolelist


def get_chatterbox_role():

    rolelist=get_f5tts_role()
    rolelist["default"]='default'
    return rolelist

def get_f5tts_role():
    rolelist = {"No": "No", "clone": "clone"}
    if not params.get('f5tts_role', '').strip():
        return rolelist
    for it in params.get('f5tts_role', '').strip().split("\n"):
        tmp = it.strip().split('#')
        if len(tmp) != 2:
            continue
        rolelist[tmp[0]] = {"ref_wav": tmp[0], "ref_text": tmp[1]}
    return rolelist


# 获取clone-voice的角色列表
def get_clone_role(set_p=False):
    from . import help_misc
    if not params.get('clone_api', ''):
        if set_p:
            raise Exception(tr('bixutianxiecloneapi'))
        return False
    try:
        url = params.get('clone_api', '').strip().rstrip('/') + "/init"
        res = requests.get('http://' + url.replace('http://', ''), proxies={"http": "", "https": ""})
        res.raise_for_status()
        params["clone_voicelist"] = ['No', "clone"] + res.json()
        help_misc.set_process(type='refreshtts')
    except Exception:
        if set_p: raise
    return False


# 从渠道返回的角色中选择默认配音音色。`No` 只表示关闭配音，不能作为自动默认值。
def default_voice_role(roles, preferred=None) -> str:
    normalized = [str(role).strip() for role in (roles or [])]
    usable = [role for role in normalized if role not in {'', '-', 'No'}]
    preferred = str(preferred or '').strip()
    if preferred in usable:
        return preferred
    return usable[0] if usable else 'No'


# 根据渠道返回角色列表 供下拉菜单使用
def role_menu(tts_type, langcode=None) -> List:
    from videotrans import tts

    if tts_type == tts.OPENAI_TTS:
        return ['No'] + (params.get('openaitts_role') or contants.OPENAITTS_ROLES).split(',')

    if tts_type == tts.XAI_TTS:
        return ['No'] + contants.XAITTS_ROLES.split(',')

    if tts_type == tts.XIAOMI_TTS:
        return ['No'] + contants.MITTS_ROLES.split(',')

    if tts_type == tts.QWEN_TTS:
        return list(get_qwen3tts_rolelist().keys())

    if tts_type == tts.Supertonic_TTS:
        return list(get_supertonic_rolelist().keys())

    if tts_type == tts.GLM_TTS:
        return list(get_glmtts_rolelist().keys())

    if tts_type == tts.GEMINI_TTS:
        return contants.GEMINITTS_ROLES.split(',')

    if tts_type == tts.ELEVENLABS_TTS:
        return get_elevenlabs_role()

    if tts_type == tts.CAMB_TTS:
        return get_camb_role()

    if tts_type == tts.CLONE_VOICE_TTS:
        _list = params.get("clone_voicelist")
        return ['No'] if not isinstance(_list, list) else _list

    if tts_type == tts.CHATTTS:
        return ['No'] + list(settings.ChatTTS_voicelist)

    if tts_type == tts.TTS_API:
        return ['No'] + params.get('ttsapi_voice_role', '').strip().split(',')

    if tts_type == tts.GPTSOVITS_TTS:
        return list(get_gptsovits_role().keys())

    if tts_type == tts.QWEN3LOCAL_TTS:
        return list(get_qwenttslocal_rolelist().keys())

    if tts_type in [tts.F5_TTS, tts.INDEX_TTS, tts.SPARK_TTS, tts.VOXCPM_TTS, tts.DIA_TTS, tts.OMNIVOICE_TTS,
                    tts.COSYVOICE_TTS, tts.FISHTTS, tts.MOSS_TTS]:
        return list(get_f5tts_role().keys())

    if tts_type == tts.CHATTERBOX_TTS:
        return list(get_chatterbox_role().keys())
    # 语言无关角色一致的到此结束
    # 以下均根据语言代码返回对应角色
    if not langcode:
        return ['No']

    _roledict = None
    if tts_type == tts.EDGE_TTS:
        _roledict = get_edge_rolelist()
    elif tts_type == tts.KOKORO_TTS:
        _roledict = get_kokoro_rolelist()
    elif tts_type == tts.PIPER_TTS:
        _roledict = get_piper_role()
    elif tts_type == tts.VITSCNEN_TTS:
        _roledict = get_vits_role()
    elif tts_type == tts.AI302_TTS:
        _roledict = get_302ai()
    elif tts_type == tts.DOUBAO2_TTS:
        _roledict = get_doubao2_rolelist()
    elif tts_type == tts.MINIMAXI_TTS:
        _roledict = get_minimaxi_rolelist()
    elif tts_type == tts.AZURE_TTS:
        return list(get_azure_supported_rolelist(langcode).keys())

    if not _roledict:
        return ['No']
    _roles=_roledict.get(langcode) or _roledict.get(langcode.split('-')[0])
    if not _roles:
        return ['No']
    return _roles if isinstance(_roles, list) else list(_roles.keys())


def show_refaudio_win():
    from videotrans.component.set_form import RefaudioForm
    dialog = RefaudioForm()
    dialog.exec()
    return
