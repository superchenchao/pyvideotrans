from __future__ import annotations

import argparse
import html
import json
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEXT = "This is an Azure speech network speed comparison test."
DEFAULT_VOICE = "en-AU-WilliamMultilingualNeural"


@dataclass
class Attempt:
    round: int
    mode: str
    elapsed_seconds: float
    success: bool
    output_bytes: int = 0
    error: str = ""


def load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_project_config() -> tuple[dict, dict]:
    params = load_json(PROJECT_ROOT / "videotrans" / "params.json")
    settings = load_json(PROJECT_ROOT / "videotrans" / "cfg.json")
    return params, settings


def parse_proxy(proxy_url: str) -> tuple[str, int, str, str]:
    value = proxy_url.strip()
    if not value:
        raise ValueError("代理地址为空")
    if "://" not in value:
        value = f"http://{value}"
    parsed = urlparse(value)
    if not parsed.hostname or not parsed.port:
        raise ValueError("代理格式应为 http://主机:端口")
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("Azure Speech SDK 这里只测试 HTTP/HTTPS 代理")
    return (
        parsed.hostname,
        parsed.port,
        unquote(parsed.username or ""),
        unquote(parsed.password or ""),
    )


def masked_proxy(proxy_url: str) -> str:
    host, port, _, _ = parse_proxy(proxy_url)
    return f"{host}:{port}"


def create_speech_config(speechsdk, key: str, region_or_endpoint: str):
    if region_or_endpoint.lower().startswith(("http://", "https://")):
        return speechsdk.SpeechConfig(
            subscription=key,
            endpoint=region_or_endpoint,
        )
    return speechsdk.SpeechConfig(subscription=key, region=region_or_endpoint)


def run_attempt(
    speechsdk,
    *,
    key: str,
    region_or_endpoint: str,
    voice: str,
    text: str,
    proxy_url: str,
    use_proxy: bool,
    output_file: Path,
    round_number: int,
) -> Attempt:
    mode = "proxy" if use_proxy else "direct"
    output_file.unlink(missing_ok=True)
    speech_config = create_speech_config(speechsdk, key, region_or_endpoint)
    if use_proxy:
        host, port, username, password = parse_proxy(proxy_url)
        speech_config.set_proxy(host, port, username, password)
    speech_config.set_speech_synthesis_output_format(
        speechsdk.SpeechSynthesisOutputFormat.Riff48Khz16BitMonoPcm
    )
    audio_config = speechsdk.audio.AudioOutputConfig(filename=str(output_file))
    synthesizer = speechsdk.SpeechSynthesizer(
        speech_config=speech_config,
        audio_config=audio_config,
    )
    ssml = (
        "<speak version='1.0' xml:lang='en-US' "
        "xmlns='http://www.w3.org/2001/10/synthesis'>"
        f"<voice name='{html.escape(voice, quote=True)}'>"
        f"{html.escape(text)}"
        "</voice></speak>"
    )
    started = time.perf_counter()
    try:
        result = synthesizer.speak_ssml_async(ssml).get()
        elapsed = time.perf_counter() - started
        if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
            size = output_file.stat().st_size if output_file.is_file() else 0
            if size <= 44:
                raise RuntimeError("Azure 返回成功，但输出 WAV 为空")
            return Attempt(round_number, mode, elapsed, True, size)
        if result.reason == speechsdk.ResultReason.Canceled:
            details = result.cancellation_details
            code = getattr(getattr(details, "error_code", None), "name", "Unknown")
            detail = str(getattr(details, "error_details", "") or "").strip()
            raise RuntimeError(f"{code}: {detail}".strip())
        raise RuntimeError(f"Unexpected result: {result.reason}")
    except Exception as exc:
        elapsed = time.perf_counter() - started
        return Attempt(
            round_number,
            mode,
            elapsed,
            False,
            error=" ".join(str(exc).split())[:500],
        )


def summarize(attempts: list[Attempt]) -> dict:
    summary = {}
    for mode in ("direct", "proxy"):
        selected = [item for item in attempts if item.mode == mode]
        successful = [item.elapsed_seconds for item in selected if item.success]
        summary[mode] = {
            "success": len(successful),
            "total": len(selected),
            "average_seconds": round(statistics.mean(successful), 3) if successful else None,
            "median_seconds": round(statistics.median(successful), 3) if successful else None,
            "min_seconds": round(min(successful), 3) if successful else None,
            "max_seconds": round(max(successful), 3) if successful else None,
        }
    direct = summary["direct"]
    proxy = summary["proxy"]
    if direct["median_seconds"] and proxy["median_seconds"]:
        summary["proxy_median_improvement_percent"] = round(
            (direct["median_seconds"] - proxy["median_seconds"])
            / direct["median_seconds"]
            * 100,
            1,
        )
    else:
        summary["proxy_median_improvement_percent"] = None
    return summary


def print_summary(summary: dict) -> None:
    print("\n===== 测试结果 =====")
    for mode, label in (("direct", "SDK 未显式代理"), ("proxy", "SDK 显式代理")):
        item = summary[mode]
        print(
            f"{label}: 成功 {item['success']}/{item['total']}，"
            f"平均 {item['average_seconds'] if item['average_seconds'] is not None else '-'} 秒，"
            f"中位数 {item['median_seconds'] if item['median_seconds'] is not None else '-'} 秒"
        )
    improvement = summary["proxy_median_improvement_percent"]
    if improvement is None:
        print("结论：至少一组没有成功结果，先检查代理或 Azure 配置。")
    elif improvement >= 10:
        print(f"结论：显式代理中位数快 {improvement}%，建议 Azure TTS 使用代理。")
    elif improvement <= -10:
        print(f"结论：显式代理中位数慢 {abs(improvement)}%，建议 Azure TTS 直连。")
    else:
        print(f"结论：两者差异约 {abs(improvement)}%，暂时没有明显速度差异。")


def parse_args() -> argparse.Namespace:
    _, settings = load_project_config()
    parser = argparse.ArgumentParser(description="对比 Azure TTS 直连和显式代理速度")
    parser.add_argument(
        "--proxy",
        default=str(settings.get("proxy", "") or "http://127.0.0.1:7897"),
        help="HTTP 代理，例如 http://127.0.0.1:7897",
    )
    parser.add_argument("--rounds", type=int, default=3, help="每种模式测试次数")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help="Azure voice ID")
    parser.add_argument("--text", default=DEFAULT_TEXT, help="每次合成的相同测试文本")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.rounds < 1 or args.rounds > 10:
        print("错误：--rounds 必须在 1 到 10 之间。", file=sys.stderr)
        return 2
    params, _ = load_project_config()
    key = str(os.environ.get("AZURE_SPEECH_KEY") or params.get("azure_speech_key") or "").strip()
    region = str(
        os.environ.get("AZURE_SPEECH_REGION")
        or params.get("azure_speech_region")
        or ""
    ).strip()
    if not key or not region:
        print(
            "错误：没有找到 Azure Speech Key/Region。请先在 pyVideoTrans 中保存 Azure TTS 配置。",
            file=sys.stderr,
        )
        return 2
    try:
        proxy_display = masked_proxy(args.proxy)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    try:
        import azure.cognitiveservices.speech as speechsdk
    except ImportError:
        print("错误：当前 Python 环境没有安装 azure-cognitiveservices-speech。", file=sys.stderr)
        return 2

    run_dir = PROJECT_ROOT / "azure-tts-proxy-test" / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    print("===== Azure TTS 直连/代理速度测试 =====")
    print(f"区域或端点：{region}")
    print(f"代理：{proxy_display}")
    print(f"音色：{args.voice}")
    print(f"每种模式：{args.rounds} 次，共 {args.rounds * 2} 次请求")
    print("说明：direct 表示 SDK 未显式设置代理；若系统启用了 TUN/全局代理，direct 仍可能被系统接管。\n")

    attempts: list[Attempt] = []
    for round_number in range(1, args.rounds + 1):
        order = (False, True) if round_number % 2 else (True, False)
        for use_proxy in order:
            mode = "proxy" if use_proxy else "direct"
            print(f"第 {round_number}/{args.rounds} 轮，{mode}：测试中...", end="", flush=True)
            attempt = run_attempt(
                speechsdk,
                key=key,
                region_or_endpoint=region,
                voice=args.voice,
                text=args.text,
                proxy_url=args.proxy,
                use_proxy=use_proxy,
                output_file=run_dir / f"round-{round_number}-{mode}.wav",
                round_number=round_number,
            )
            attempts.append(attempt)
            if attempt.success:
                print(f" 成功，{attempt.elapsed_seconds:.3f} 秒，{attempt.output_bytes} 字节")
            else:
                print(f" 失败，{attempt.elapsed_seconds:.3f} 秒，{attempt.error}")

    summary = summarize(attempts)
    print_summary(summary)
    report = {
        "created_at": datetime.now().astimezone().isoformat(),
        "region_or_endpoint": region,
        "proxy": proxy_display,
        "voice": args.voice,
        "text": args.text,
        "attempts": [asdict(item) for item in attempts],
        "summary": summary,
        "note": "direct means no explicit SpeechConfig proxy; system-wide/TUN routing can still affect it.",
    }
    report_file = run_dir / "report.json"
    report_file.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"报告：{report_file}")
    return 0 if summary["direct"]["success"] or summary["proxy"]["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
