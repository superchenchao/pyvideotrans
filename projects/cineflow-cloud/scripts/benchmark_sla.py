from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

TERMINAL = {"succeeded", "degraded", "failed", "timed_out", "rejected"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify CineFlow five-minute SLA")
    parser.add_argument("--api", default="http://127.0.0.1:8080")
    parser.add_argument("--input-url", required=True)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--input-bytes", type=int, required=True)
    parser.add_argument("--target-language", required=True)
    parser.add_argument("--voice", required=True)
    parser.add_argument("--source-language", default="zh-CN")
    parser.add_argument("--deadline", type=float, default=300.0)
    parser.add_argument("--max-cost", type=float, default=5.0)
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
        "target_voice": args.voice,
        "max_cost_cny": args.max_cost,
        "strict_sla": True,
    }

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

        while True:
            elapsed = time.monotonic() - started
            if elapsed > args.deadline + 2:
                print(f"CLIENT_DEADLINE_EXCEEDED elapsed={elapsed:.3f}", file=sys.stderr)
                return 4
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
                return 0 if elapsed <= args.deadline else 6
            time.sleep(0.5)


if __name__ == "__main__":
    raise SystemExit(main())
