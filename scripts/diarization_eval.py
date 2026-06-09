import argparse
import csv
import json
import shutil
import subprocess
import time
from collections import Counter
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]

import sys

sys.path.insert(0, str(ROOT_DIR))

from videotrans.configure import config
from videotrans.configure.config import ROOT_DIR as VT_ROOT_DIR, settings
from videotrans.util import tools


LOCAL_MODELS = {"built", "ali_CAM", "pyannote", "reverb"}


def norm_spk(value):
    if value is None:
        return ""
    text = str(value).strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return text


def run_ffmpeg(cmd):
    subprocess.run(["ffmpeg", "-hide_banner", "-y", *cmd], check=True)


def ensure_audio(video, output_dir):
    audio = output_dir / "audio_16k.wav"
    if audio.exists() and audio.stat().st_size > 0:
        return audio
    run_ffmpeg([
        "-i",
        video.as_posix(),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        audio.as_posix(),
    ])
    return audio


def write_subtitle_windows(srt_path, output_dir):
    subs = tools.get_subtitle_from_srt(srt_path.as_posix(), is_file=True)
    windows = [[it["start_time"], it["end_time"]] for it in subs]
    subtitles_file = output_dir / "subtitle_windows.json"
    subtitles_file.write_text(json.dumps(windows, ensure_ascii=False), encoding="utf-8")
    return subs, subtitles_file


def download_backend_assets(model):
    if model == "built":
        tools.down_file_from_ms(
            f"{VT_ROOT_DIR}/models/onnx",
            [
                "https://www.modelscope.cn/models/himyworld/videotrans/resolve/master/onnx/seg_model.onnx",
                "https://www.modelscope.cn/models/himyworld/videotrans/resolve/master/onnx/nemo_en_titanet_small.onnx",
                "https://www.modelscope.cn/models/himyworld/videotrans/resolve/master/onnx/3dspeaker_speech_eres2net_large_sv_zh-cn_3dspeaker_16k.onnx",
            ],
        )
    elif model == "ali_CAM":
        tools.check_and_down_ms(model_id="iic/speech_campplus_speaker-diarization_common")


def run_local_backend(model, audio, subtitles_file, output_dir, num_speakers, cuda):
    download_backend_assets(model)
    if model == "built":
        from videotrans.process.prepare_audio import built_speakers as runner

        kwargs = {
            "input_file": audio.as_posix(),
            "subtitles_file": subtitles_file.as_posix(),
            "speak_file": (output_dir / f"{model}.speaker.json").as_posix(),
            "num_speakers": num_speakers,
            "language": "zh",
        }
    elif model == "ali_CAM":
        from videotrans.process.prepare_audio import cam_speakers as runner

        kwargs = {
            "input_file": audio.as_posix(),
            "subtitles_file": subtitles_file.as_posix(),
            "speak_file": (output_dir / f"{model}.speaker.json").as_posix(),
            "num_speakers": num_speakers,
            "is_cuda": cuda,
        }
    elif model == "pyannote":
        from videotrans.process.prepare_audio import pyannote_speakers as runner

        if not settings.get("hf_token"):
            raise RuntimeError("Missing hf_token for pyannote")
        kwargs = {
            "input_file": audio.as_posix(),
            "subtitles_file": subtitles_file.as_posix(),
            "speak_file": (output_dir / f"{model}.speaker.json").as_posix(),
            "num_speakers": num_speakers,
            "is_cuda": cuda,
        }
    elif model == "reverb":
        from videotrans.process.prepare_audio import reverb_speakers as runner

        if not settings.get("hf_token"):
            raise RuntimeError("Missing hf_token for reverb")
        kwargs = {
            "input_file": audio.as_posix(),
            "subtitles_file": subtitles_file.as_posix(),
            "speak_file": (output_dir / f"{model}.speaker.json").as_posix(),
            "num_speakers": num_speakers,
            "is_cuda": cuda,
        }
    else:
        raise ValueError(f"Unsupported local model: {model}")

    start = time.time()
    ok, err = runner(**kwargs)
    elapsed = time.time() - start
    if not ok:
        raise RuntimeError(err or f"{model} failed")
    labels = json.loads(Path(kwargs["speak_file"]).read_text(encoding="utf-8"))
    return [norm_spk(it) for it in labels], elapsed


def load_existing(path):
    labels = json.loads(path.read_text(encoding="utf-8"))
    return [norm_spk(it) for it in labels]


def summarize(labels, total):
    counts = Counter(labels)
    transitions = sum(1 for i in range(1, len(labels)) if labels[i] != labels[i - 1])
    return {
        "lines": len(labels),
        "expected_lines": total,
        "speakers": len([k for k in counts if k]),
        "transitions": transitions,
        "distribution": dict(sorted(counts.items())),
    }


def disagreement(a, b):
    n = min(len(a), len(b))
    if n == 0:
        return None
    diff = sum(1 for i in range(n) if a[i] != b[i])
    return diff, n, diff / n


def write_report(output_dir, subs, results, timings, failures):
    summary = {
        name: summarize(labels, len(subs))
        for name, labels in results.items()
    }
    comparisons = {}
    names = list(results)
    for i, left in enumerate(names):
        for right in names[i + 1:]:
            comparisons[f"{left} vs {right}"] = disagreement(results[left], results[right])

    csv_path = output_dir / "line_speaker_compare.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["line", "time", "text", *names])
        for idx, sub in enumerate(subs):
            writer.writerow([
                sub["line"],
                sub["time"],
                sub["text"].replace("\n", " "),
                *[results[name][idx] if idx < len(results[name]) else "" for name in names],
            ])

    report_path = output_dir / "report.md"
    lines = [
        "# Diarization Eval Report",
        "",
        f"- Subtitle lines: {len(subs)}",
        f"- CSV: `{csv_path.as_posix()}`",
        "",
        "## Summary",
        "",
        "| Model | Lines | Speakers | Transitions | Seconds | Distribution |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for name, item in summary.items():
        seconds = timings.get(name)
        seconds_text = "" if seconds is None else f"{seconds:.2f}"
        dist = ", ".join(f"{k}:{v}" for k, v in item["distribution"].items())
        lines.append(
            f"| {name} | {item['lines']}/{item['expected_lines']} | {item['speakers']} | {item['transitions']} | {seconds_text} | {dist} |"
        )

    if comparisons:
        lines.extend(["", "## Pairwise Disagreement", "", "| Pair | Different / Compared | Ratio |", "|---|---:|---:|"])
        for pair, item in comparisons.items():
            if item is None:
                lines.append(f"| {pair} | - | - |")
            else:
                diff, n, ratio = item
                lines.append(f"| {pair} | {diff}/{n} | {ratio:.2%} |")

    if failures:
        lines.extend(["", "## Failures", ""])
        for name, err in failures.items():
            lines.append(f"- `{name}`: {err}")

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (output_dir / "summary.json").write_text(
        json.dumps({"summary": summary, "timings": timings, "failures": failures}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report_path, csv_path


def main():
    parser = argparse.ArgumentParser(description="Compare speaker diarization backends on one fixed subtitle timeline.")
    parser.add_argument("--video", required=True)
    parser.add_argument("--srt", required=True)
    parser.add_argument("--output-dir", default="local_runs/diarization_eval")
    parser.add_argument("--models", default="built", help="Comma-separated: built,ali_CAM,pyannote,reverb")
    parser.add_argument("--existing-speaker-json", action="append", default=[])
    parser.add_argument("--num-speakers", type=int, default=-1, help="-1 auto, otherwise pass exact speaker count to backend")
    parser.add_argument("--cuda", action="store_true")
    args = parser.parse_args()

    config.init_run()

    video = Path(args.video).resolve()
    srt = Path(args.srt).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    audio = ensure_audio(video, output_dir)
    subs, subtitles_file = write_subtitle_windows(srt, output_dir)

    results = {}
    timings = {}
    failures = {}

    for existing in args.existing_speaker_json:
        path = Path(existing).resolve()
        name = f"existing:{path.parent.name}"
        try:
            results[name] = load_existing(path)
            timings[name] = None
            shutil.copy2(path, output_dir / f"{name.replace(':', '_')}.speaker.json")
        except Exception as exc:
            failures[name] = str(exc)

    for model in [it.strip() for it in args.models.split(",") if it.strip()]:
        if model not in LOCAL_MODELS:
            failures[model] = "Unsupported by this local-backend script"
            continue
        try:
            labels, elapsed = run_local_backend(model, audio, subtitles_file, output_dir, args.num_speakers, args.cuda)
            results[model] = labels
            timings[model] = elapsed
        except Exception as exc:
            failures[model] = str(exc)

    report_path, csv_path = write_report(output_dir, subs, results, timings, failures)
    print(f"report={report_path}")
    print(f"csv={csv_path}")
    if failures:
        print("failures=" + json.dumps(failures, ensure_ascii=False))


if __name__ == "__main__":
    main()
