import logging
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union, List, Dict
import azure.cognitiveservices.speech as speechsdk
from azure.core.exceptions import ResourceExistsError, ClientAuthenticationError, ResourceNotFoundError
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_not_exception_type, before_log, after_log
from videotrans.configure.config import params, logger, settings
from videotrans.configure.excepts import NO_RETRY_EXCEPT, StopRetry, StopTask
from videotrans.tts._base import BaseTTS
from videotrans.util import tools


def create_speech_config(subscription: str, region_or_endpoint: str):
    """Create an Azure Speech config from either a region name or endpoint URL."""
    subscription = subscription.strip()
    region_or_endpoint = region_or_endpoint.strip()
    if region_or_endpoint.lower().startswith(("http://", "https://")):
        return speechsdk.SpeechConfig(
            subscription=subscription,
            endpoint=region_or_endpoint,
        )
    return speechsdk.SpeechConfig(
        subscription=subscription,
        region=region_or_endpoint,
    )


def resolve_voice_name(language: str, role_name: str) -> str:
    """Resolve a display name while preserving an already valid Azure voice ID."""
    resolved_name = tools.get_azure_rolelist(language.split('-')[0], role_name)
    if not resolved_name:
        resolved_name = tools.get_edge_rolelist(
            role_name=role_name,
            locale=language,
        )
    return resolved_name or role_name


def azure_retry_attempts() -> int:
    """Treat retry_nums=1 as one retry instead of one total attempt."""
    try:
        configured = int(settings.get("retry_nums") or 1)
    except (TypeError, ValueError):
        configured = 1
    return max(2, configured + 1)


def normalize_azure_runtime_settings(
        concurrency=None, wait_seconds=None, adaptive=None) -> tuple[int, float, bool]:
    """Validate persisted Azure-only scheduling settings."""
    try:
        concurrency = int(float(concurrency))
    except (TypeError, ValueError):
        concurrency = 5
    try:
        wait_seconds = float(wait_seconds)
    except (TypeError, ValueError):
        wait_seconds = 0.0
    if isinstance(adaptive, str):
        adaptive = adaptive.strip().lower() not in {"0", "false", "no", "off"}
    elif adaptive is None:
        adaptive = True
    return min(10, max(1, concurrency)), min(5.0, max(0.0, wait_seconds)), bool(adaptive)


def is_transient_azure_error(error) -> bool:
    """Return whether Azure suggests reducing request pressure for later work."""
    if not error:
        return False
    message = str(error).lower()
    return any(marker in message for marker in (
        "servicetimeout", "timeout", "too many requests", "toomanyrequests",
        "status code: 429", "statuscode=429", "throttl", "quota",
    ))


def reduce_azure_concurrency(current: int) -> int:
    if current > 3:
        return 3
    if current > 1:
        return 1
    return 1


def describe_cancellation(details) -> tuple[str, bool]:
    error_code = getattr(details, "error_code", None)
    code_name = getattr(error_code, "name", None) or str(error_code or "Unknown")
    error_details = str(getattr(details, "error_details", "") or "").strip()
    reason = str(getattr(details, "reason", "") or "").strip()
    message = f"Azure speech synthesis canceled: code={code_name}"
    if error_details:
        message += f", details={error_details}"
    elif reason:
        message += f", reason={reason}"
    permanent = code_name in {"AuthenticationFailure", "BadRequest", "Forbidden"}
    return message, permanent


@dataclass
class AzureTTS(BaseTTS):

    adaptive_concurrency: bool = field(default=True, init=False)

    def __post_init__(self):
        super().__post_init__()
        self.dub_nums, self.wait_sec, self.adaptive_concurrency = normalize_azure_runtime_settings(
            params.get("azure_tts_concurrency", 5),
            params.get("azure_tts_wait", 0),
            params.get("azure_tts_adaptive", True),
        )

    def _exec(self) -> None:
        """Keep Azure busy while allowing later submissions to reduce their load."""
        if self._exit():
            return

        pending = deque()
        completed_tasks = 0
        for idx, item in enumerate(self.queue_tts):
            if not item.get("text", "").strip() or tools.vail_file(item.get("filename", "")):
                completed_tasks += 1
            else:
                pending.append((idx, item))

        max_concurrency = min(self.dub_nums, max(1, len(pending)))
        current_concurrency = max_concurrency
        stable_successes = 0
        adaptive_retries = {}
        last_submit_at = 0.0
        logger.info(
            "Azure TTS scheduling: max_concurrency=%s, request_interval=%.1fs, adaptive=%s",
            max_concurrency, self.wait_sec, self.adaptive_concurrency,
        )

        active = {}
        with ThreadPoolExecutor(max_workers=max_concurrency) as pool:
            while pending or active:
                if self._exit():
                    return

                while pending and len(active) < current_concurrency:
                    if self.wait_sec > 0 and last_submit_at:
                        remaining = self.wait_sec - (time.monotonic() - last_submit_at)
                        if remaining > 0:
                            time.sleep(remaining)
                    idx, item = pending.popleft()
                    future = pool.submit(self._item_task, item, idx)
                    active[future] = (idx, item)
                    last_submit_at = time.monotonic()

                if not active:
                    continue
                completed, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in completed:
                    idx, item = active.pop(future)
                    error = future.result()
                    self.error = error
                    if isinstance(error, StopTask):
                        raise error

                    transient = is_transient_azure_error(error)
                    if error and transient and self.adaptive_concurrency:
                        reduced = reduce_azure_concurrency(current_concurrency)
                        if reduced < current_concurrency:
                            logger.warning(
                                "Azure TTS transient failure, reducing concurrency %s -> %s",
                                current_concurrency, reduced,
                            )
                            current_concurrency = reduced
                        stable_successes = 0
                        if adaptive_retries.get(idx, 0) < 1:
                            adaptive_retries[idx] = adaptive_retries.get(idx, 0) + 1
                            pending.appendleft((idx, item))
                            logger.warning(
                                "Azure TTS will retry line %s at lower concurrency", idx + 1,
                            )
                            continue

                    if not error and self.adaptive_concurrency and current_concurrency < max_concurrency:
                        stable_successes += 1
                        if stable_successes >= 10:
                            recovered = min(max_concurrency, current_concurrency + 1)
                            logger.info(
                                "Azure TTS requests are stable, recovering concurrency %s -> %s",
                                current_concurrency, recovered,
                            )
                            current_concurrency = recovered
                            stable_successes = 0

                    completed_tasks += 1
                    self.signal(text=f"TTS: [{completed_tasks}/{self.len}] ...")

        self.signal(text="TTS ended ...")

    @retry(retry=retry_if_not_exception_type(NO_RETRY_EXCEPT), stop=stop_after_attempt(azure_retry_attempts()), wait=wait_fixed(2), before=before_log(logger, logging.INFO), after=after_log(logger, logging.INFO))
    def _run(self, data_item: Union[Dict, List, None], idx: int = -1) -> Union[str, None]:
        try:
            filename = data_item['filename'] + f"-generate.wav"
            Path(filename).unlink(missing_ok=True)
            speech_config = create_speech_config(
                params.get('azure_speech_key', ''),
                params.get('azure_speech_region', ''),
            )
            speech_config.set_speech_synthesis_output_format(
                speechsdk.SpeechSynthesisOutputFormat.Riff48Khz16BitMonoPcm)

            audio_config = speechsdk.audio.AudioOutputConfig(use_default_speaker=True, filename=filename)
            speech_synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=audio_config)
            text_xml = f"<prosody rate='{self.rate}' pitch='{self.pitch}' volume='{self.volume}'>{data_item['text']}</prosody>"
            voice_name = resolve_voice_name(self.language, data_item['role'])
            ssml = """<speak version='1.0' xml:lang='{}' xmlns='http://www.w3.org/2001/10/synthesis' xmlns:mstts='http://www.w3.org/2001/mstts'>
                                    <voice name='{}'>
                                        <prosody rate="{}" pitch='{}'  volume='{}'>
                                        {}
                                        </prosody>
                                    </voice>
                                    </speak>""".format(self.language, voice_name, self.rate, self.pitch,
                                                       self.volume,
                                                       text_xml)
            logger.debug(f'{ssml=}')
            speech_synthesis_result = speech_synthesizer.speak_ssml_async(ssml).get()
            if speech_synthesis_result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                if tools.vail_file(filename):
                    self.convert_to_wav(filename, data_item['filename'])
                    if not tools.vail_file(data_item['filename']):
                        raise RuntimeError("Azure TTS converted output is missing or empty")
                else:
                    raise RuntimeError("Azure TTS generated output is missing or empty")
                return
            if speech_synthesis_result.reason == speechsdk.ResultReason.Canceled:
                cancellation_details = speech_synthesis_result.cancellation_details
                message, permanent = describe_cancellation(cancellation_details)
                logger.warning(
                    f"Azure TTS 单条合成失败: index={idx + 1}, "
                    f"line={data_item.get('line', '')}, {message}"
                )
                if permanent:
                    raise StopRetry(message)
                raise RuntimeError(message)
            raise RuntimeError(
                f"Azure TTS returned unexpected result: {speech_synthesis_result.reason}"
            )

        except (ResourceExistsError,ResourceNotFoundError,ClientAuthenticationError) as e:
            raise StopTask(str(e)) from e

