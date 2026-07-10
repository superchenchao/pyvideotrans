import argparse
import csv
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from urllib import request

import requests

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from dashscope.utils.oss_utils import OssUtils

from videotrans.util import tools


def load_api_key():
    key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if key:
        return key

    for path in (ROOT_DIR / "videotrans/params.json", ROOT_DIR / "videotrans/cfg.json"):
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        key = str(data.get("qwenmt_key", "")).strip()
        if key:
            return key

    raise RuntimeError(
        "Missing Alibaba Cloud Model Studio API key. Set DASHSCOPE_API_KEY "
        "or configure qwenmt_key in pyVideoTrans."
    )


def upload_audio(audio_path, api_key, model):
    oss_url, _ = OssUtils.upload(
        model=model,
        file_path=audio_path.as_posix(),
        api_key=api_key,
    )
    if not oss_url:
        raise RuntimeError("Alibaba Cloud temporary audio upload failed")
    return oss_url


def run_transcription(file_url, api_key, model, workspace):
    base_url = (
        f"https://{workspace}.cn-beijing.maas.aliyuncs.com/api/v1"
        if workspace
        else "https://dashscope.aliyuncs.com/api/v1"
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
        "X-DashScope-OssResourceResolve": "enable",
    }
    payload = {
        "model": model,
        "input": {"file_urls": [file_url]},
        "parameters": {
            "channel_id": [0],
            "language_hints": ["zh"],
            "diarization_enabled": True,
            "special_word_filter": {"system_reserved_filter": False},
        },
    }
    response = requests.post(
        f"{base_url}/services/audio/asr/transcription",
        headers=headers,
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    submitted = response.json()
    task_id = submitted.get("output", {}).get("task_id")
    if not task_id:
        raise RuntimeError(f"Task submission returned no task_id: {submitted}")

    poll_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    deadline = time.time() + 900
    output = None
    while time.time() < deadline:
        result_response = requests.get(
            f"{base_url}/tasks/{task_id}", headers=poll_headers, timeout=60
        )
        result_response.raise_for_status()
        result = result_response.json()
        output = result.get("output", {})
        status = output.get("task_status")
        if status == "SUCCEEDED":
            break
        if status in {"FAILED", "CANCELED", "UNKNOWN"}:
            raise RuntimeError(
                f"Task {status}: code={output.get('code')}, message={output.get('message')}, "
                f"results={output.get('results')}"
            )
        time.sleep(3)
    else:
        raise TimeoutError(f"Task did not finish within 900 seconds: {task_id}")

    for item in output.get("results", []):
        if item.get("subtask_status") == "SUCCEEDED" and item.get("transcription_url"):
            return json.loads(request.urlopen(item["transcription_url"], timeout=60).read().decode("utf-8"))

    raise RuntimeError(f"Task returned no successful transcription: {output}")


def get_sentences(raw_result):
    sentences = []
    for transcript in raw_result.get("transcripts", []):
        for item in transcript.get("sentences", []):
            begin = int(item.get("begin_time", 0))
            end = int(item.get("end_time", 0))
            if end <= begin:
                continue
            speaker_id = item.get("speaker_id")
            sentences.append({
                "begin_time": begin,
                "end_time": end,
                "text": str(item.get("text", "")).strip(),
                "speaker": f"spk{speaker_id}" if speaker_id is not None else "",
            })
    return sentences


def map_to_subtitle_lines(subtitles, sentences):
    labels = []
    for sub in subtitles:
        overlaps = Counter()
        for sentence in sentences:
            overlap = min(sub["end_time"], sentence["end_time"]) - max(
                sub["start_time"], sentence["begin_time"]
            )
            if overlap > 0 and sentence["speaker"]:
                overlaps[sentence["speaker"]] += overlap
        labels.append(overlaps.most_common(1)[0][0] if overlaps else "")
    return labels


def count_transitions(labels):
    clean = [label for label in labels if label]
    return sum(clean[i] != clean[i - 1] for i in range(1, len(clean)))


def write_outputs(output_dir, raw_result, sentences, subtitles, labels):
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "raw_result.json").write_text(
        json.dumps(raw_result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "sentences.json").write_text(
        json.dumps(sentences, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "speaker.json").write_text(
        json.dumps(labels, ensure_ascii=False), encoding="utf-8"
    )

    with (output_dir / "line_speaker_compare.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as file:
        writer = csv.writer(file)
        writer.writerow(["line", "time", "text", "aliyun_fun_asr"])
        for sub, label in zip(subtitles, labels):
            writer.writerow([sub["line"], sub["time"], sub["text"].replace("\n", " "), label])

    distribution = Counter(label for label in labels if label)
    report = [
        "# Alibaba Cloud Fun-ASR Diarization Eval",
        "",
        f"- Cloud sentences: {len(sentences)}",
        f"- Subtitle lines: {len(subtitles)}",
        f"- Mapped lines: {sum(bool(label) for label in labels)}",
        f"- Auto-detected speakers: {len(distribution)}",
        f"- Speaker transitions: {count_transitions(labels)}",
        "- Distribution: " + ", ".join(f"{key}:{value}" for key, value in sorted(distribution.items())),
        "",
        "The speaker IDs are anonymous labels. Accuracy still requires manual comparison with the video.",
    ]
    (output_dir / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Evaluate Alibaba Cloud Fun-ASR automatic diarization")
    parser.add_argument("--audio", required=True)
    parser.add_argument("--srt", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--model", default="fun-asr")
    parser.add_argument("--workspace", default=None)
    args = parser.parse_args()

    audio_path = Path(args.audio).resolve()
    srt_path = Path(args.srt).resolve()
    output_dir = Path(args.output_dir).resolve()
    if not audio_path.is_file():
        raise FileNotFoundError(audio_path)
    if not srt_path.is_file():
        raise FileNotFoundError(srt_path)

    api_key = load_api_key()
    file_url = upload_audio(audio_path, api_key, args.model)
    raw_result = run_transcription(file_url, api_key, args.model, args.workspace)
    sentences = get_sentences(raw_result)
    if not sentences:
        raise RuntimeError("Fun-ASR returned no timestamped sentences")

    subtitles = tools.get_subtitle_from_srt(srt_path.as_posix(), is_file=True)
    labels = map_to_subtitle_lines(subtitles, sentences)
    write_outputs(output_dir, raw_result, sentences, subtitles, labels)
    print(f"report={output_dir / 'report.md'}")
    print(f"csv={output_dir / 'line_speaker_compare.csv'}")


if __name__ == "__main__":
    main()
