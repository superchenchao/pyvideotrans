from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

TERMINAL = {"succeeded", "degraded", "failed", "rejected"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure the CineFlow five-minute target")
    parser.add_argument("--api", default="http://127.0.0.1:8080")
    parser.add_argument("--input-url", required=True)
    parser.add_argument("--clean-video-url", default="")
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--input-bytes", type=int, required=True)
    parser.add_argument("--target-language", required=True)
    parser.add_argument("--voice", required=True)
    parser.add_argument("--source-language", default="zh-CN")
    parser.add_argument("--target-seconds", type=float, default=300.0)
    parser.add_argument("--max-wait-seconds", type=float, default=1800.0)
    parser.add_argument("--max-cost", type=float, default=5.0)
    parser.add_argument("--fail-on-target-miss", action="store_true")
    args = parser.parse_args()

    payload = {
        "input_url": args.input_url,
        "probe": {
            "duration_seconds": args.duration,
            "input_bytes": args.input_bytes,
            "codec": "h264",
            "width": 1920,
            "height": 1080,
            "fps": 25,
        },
        "source_language": args.source_language,
        "target_language": args.target_language,
        "translation_engine": "deepseek",
        "target_voice": args.voice,
        "max_cost_cny": args.max_cost,
        "optimize_for_target": True,
    }
    if args.clean_video_url:
        payload["clean_video_url"] = args.clean_video_url

    with httpx.Client(timeout=15.0) as client:
        admission = client.post(f"{args.api.rstrip('/')}/v1/admission", json=payload)
        admission.raise_for_status()
        quote = admission.json()
        print("ADMISSION", json.dumps(quote, ensure_ascii=False))
        if not quote.get("accepted"):
            return 2

        started = time.monotonic()
        response = client.post(f"{args.api.rstrip('/')}/v1/jobs", json=payload)
        if response.status_code != 202:
            print("SUBMIT_FAILED", response.status_code, response.text, file=sys.stderr)
            return 3
        job = response.json()
        job_id = job["job_id"]
        print("JOB", job_id)

        target_notice_printed = False
        while True:
            elapsed = time.monotonic() - started
            if elapsed > args.max_wait_seconds:
                print(
                    f"CLIENT_MAX_WAIT_EXCEEDED elapsed={elapsed:.3f}; server job is not cancelled",
                    file=sys.stderr,
                )
                return 4
            if elapsed > args.target_seconds and not target_notice_printed:
                print(f"TARGET_MISSED elapsed={elapsed:.3f}; continuing to wait for completion")
                target_notice_printed = True

            status_response = client.get(f"{args.api.rstrip('/')}/v1/jobs/{job_id}")
            status_response.raise_for_status()
            record = status_response.json()
            state = record["state"]
            print(
                f"STATUS elapsed={elapsed:.3f}s state={state} "
                f"stage={record['current_stage']} progress={record['progress']}"
            )
            if state in TERMINAL:
                print("RESULT", json.dumps(record, ensure_ascii=False))
                if state not in {"succeeded", "degraded"}:
                    return 5
                if args.fail_on_target_miss and elapsed > args.target_seconds:
                    return 6
                return 0
            time.sleep(0.5)


if __name__ == "__main__":
    raise SystemExit(main())
