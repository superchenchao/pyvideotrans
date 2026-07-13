import argparse
import ast
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.optimize import linear_sum_assignment
from sklearn.metrics import adjusted_rand_score, normalized_mutual_info_score


RAW_RESULT_PATTERN = re.compile(
    r"说话人分离原始返回结果:result=(\{'text': \[.*?\]\})"
)


def time_to_ms(value):
    hours, minutes, seconds_ms = value.split(":")
    seconds, milliseconds = seconds_ms.split(",")
    return (
        (int(hours) * 3600 + int(minutes) * 60 + int(seconds)) * 1000
        + int(milliseconds)
    )


def read_srt(path):
    content = path.read_text(encoding="utf-8-sig").strip()
    subtitles = []
    for block in re.split(r"\r?\n\r?\n", content):
        lines = block.splitlines()
        if len(lines) < 3 or " --> " not in lines[1]:
            continue
        start_text, end_text = lines[1].split(" --> ", 1)
        subtitles.append({
            "line": int(lines[0].strip()),
            "time": lines[1].strip(),
            "start": time_to_ms(start_text),
            "end": time_to_ms(end_text),
            "text": " ".join(line.strip() for line in lines[2:]),
        })
    return subtitles


def normalize_raw_segments(payload):
    if isinstance(payload, dict):
        payload = payload.get("text", payload.get("segments", []))
    segments = []
    for item in payload:
        if isinstance(item, dict):
            segments.append({
                "start": int(item["start"]),
                "end": int(item["end"]),
                "speaker": str(item["speaker"]),
            })
            continue
        if len(item) == 2 and isinstance(item[0], (list, tuple)):
            (start, end), speaker = item
        else:
            start, end, speaker = item
        if isinstance(speaker, str) and speaker.startswith("spk"):
            label = speaker
        else:
            label = f"spk{int(speaker)}"
        # ModelScope returns seconds; saved diagnostics use milliseconds.
        scale = 1000 if isinstance(start, float) or isinstance(end, float) else 1
        segments.append({
            "start": round(start * scale),
            "end": round(end * scale),
            "speaker": label,
        })
    return segments


def load_raw_segments(path):
    if path.suffix.lower() == ".json":
        return normalize_raw_segments(json.loads(path.read_text(encoding="utf-8")))
    matches = RAW_RESULT_PATTERN.findall(path.read_text(encoding="utf-8"))
    if not matches:
        raise ValueError(f"No ali_CAM raw result found in {path}")
    return normalize_raw_segments(ast.literal_eval(matches[-1]))


def build_line_diagnostics(subtitles, segments):
    diagnostics = []
    for subtitle in subtitles:
        duration = subtitle["end"] - subtitle["start"]
        overlaps = defaultdict(int)
        for segment in segments:
            overlap = max(
                0,
                min(subtitle["end"], segment["end"])
                - max(subtitle["start"], segment["start"]),
            )
            if overlap:
                overlaps[segment["speaker"]] += overlap
        ranked = sorted(overlaps.items(), key=lambda item: (-item[1], item[0]))
        baseline = ranked[0][0] if ranked else "spk0"
        best_overlap = ranked[0][1] if ranked else 0
        second_overlap = ranked[1][1] if len(ranked) > 1 else 0
        diagnostics.append({
            **subtitle,
            "duration_ms": duration,
            "baseline": baseline,
            "overlaps": dict(ranked),
            "coverage": best_overlap / duration if duration > 0 else 0.0,
            "overlap_margin": (
                (best_overlap - second_overlap) / duration if duration > 0 else 0.0
            ),
        })
    return diagnostics


def load_embeddings(audio_path, diagnostics, model_path, min_audio_ms):
    audio, sample_rate = sf.read(audio_path, dtype="float32")
    if audio.ndim > 1:
        audio = audio[:, 0]
    arrays = []
    min_samples = round(min_audio_ms * sample_rate / 1000)
    for item in diagnostics:
        start = max(0, round(item["start"] * sample_rate / 1000))
        end = min(len(audio), round(item["end"] * sample_rate / 1000))
        samples = audio[start:end]
        if len(samples) < min_samples:
            samples = np.pad(samples, (0, min_samples - len(samples)))
        arrays.append(samples)

    from modelscope.pipelines import pipeline

    verifier = pipeline(
        "speaker-verification",
        str(model_path),
        device="cpu",
        disable_update=True,
        disable_progress_bar=True,
        disable_log=True,
    )
    result = verifier(arrays, output_emb=True)
    embeddings = np.asarray(result["embs"], dtype=np.float32)
    return embeddings / (np.linalg.norm(embeddings, axis=1, keepdims=True) + 1e-9)


def build_centroids(
    diagnostics,
    embeddings,
    anchor_ms,
    min_anchor_coverage,
    min_anchor_margin,
    min_anchors,
):
    candidates = defaultdict(list)
    anchor_lines = defaultdict(list)
    for index, item in enumerate(diagnostics):
        if (
            item["duration_ms"] >= anchor_ms
            and item["coverage"] >= min_anchor_coverage
            and item["overlap_margin"] >= min_anchor_margin
        ):
            candidates[item["baseline"]].append(embeddings[index])
            anchor_lines[item["baseline"]].append(item["line"])

    centroids = {}
    accepted_lines = {}
    for speaker, values in candidates.items():
        if len(values) < min_anchors:
            continue
        centroid = np.mean(values, axis=0)
        centroids[speaker] = centroid / (np.linalg.norm(centroid) + 1e-9)
        accepted_lines[speaker] = anchor_lines[speaker]
    return centroids, accepted_lines


def refine_speakers(
    diagnostics,
    embeddings,
    centroids,
    short_ms,
    low_overlap_margin,
    rare_max_lines,
    min_similarity,
    similarity_margin,
):
    counts = Counter(item["baseline"] for item in diagnostics)
    refined = []
    for index, item in enumerate(diagnostics):
        current = item["baseline"]
        similarities = sorted(
            (
                (speaker, float(embeddings[index] @ centroid))
                for speaker, centroid in centroids.items()
            ),
            key=lambda pair: (-pair[1], pair[0]),
        )
        top_speaker, top_similarity = similarities[0]
        current_similarity = next(
            (score for speaker, score in similarities if speaker == current), None
        )
        eligible_reasons = []
        if item["duration_ms"] <= short_ms:
            eligible_reasons.append("short")
        if item["overlap_margin"] <= low_overlap_margin:
            eligible_reasons.append("low_overlap_margin")
        if counts[current] <= rare_max_lines:
            eligible_reasons.append("rare_speaker")

        changed = False
        continues_current_turn = (
            index + 1 < len(diagnostics)
            and diagnostics[index + 1]["baseline"] == current
        )
        if (
            eligible_reasons
            and not continues_current_turn
            and top_speaker != current
            and top_similarity >= min_similarity
        ):
            if current_similarity is None or top_similarity - current_similarity >= similarity_margin:
                current = top_speaker
                changed = True
        refined.append({
            **item,
            "refined": current,
            "top_embedding_speaker": top_speaker,
            "top_embedding_similarity": top_similarity,
            "baseline_embedding_similarity": current_similarity,
            "changed": changed,
            "reason": "+".join(eligible_reasons) if changed else "",
        })
    return refined


def load_ground_truth(path):
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return {int(row["line"]): row for row in csv.DictReader(f)}


def score_labels(labels, ground_truth):
    pairs = []
    for line, predicted in enumerate(labels, start=1):
        truth = ground_truth.get(line)
        if not truth or truth.get("exclude_from_single_speaker_eval", "").lower() == "true":
            continue
        if not predicted or not truth.get("speaker_id"):
            continue
        pairs.append((truth["speaker_id"], predicted))
    truth_labels = sorted({truth for truth, _ in pairs})
    predicted_labels = sorted({predicted for _, predicted in pairs})
    matrix = np.zeros((len(truth_labels), len(predicted_labels)), dtype=int)
    truth_index = {label: index for index, label in enumerate(truth_labels)}
    predicted_index = {label: index for index, label in enumerate(predicted_labels)}
    for truth, predicted in pairs:
        matrix[truth_index[truth], predicted_index[predicted]] += 1
    rows, columns = linear_sum_assignment(-matrix)
    mapping = {
        predicted_labels[column]: truth_labels[row]
        for row, column in zip(rows, columns)
    }
    correct = sum(mapping.get(predicted) == truth for truth, predicted in pairs)
    truth_values, predicted_values = zip(*pairs)
    return {
        "compared": len(pairs),
        "correct": correct,
        "accuracy": correct / len(pairs),
        "ari": adjusted_rand_score(truth_values, predicted_values),
        "nmi": normalized_mutual_info_score(truth_values, predicted_values),
        "mapping": mapping,
    }


def write_outputs(output_dir, segments, refined, anchor_lines, ground_truth):
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "raw_segments.json").write_text(
        json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "baseline.speaker.json").write_text(
        json.dumps([item["baseline"] for item in refined], ensure_ascii=False),
        encoding="utf-8",
    )
    (output_dir / "refined.speaker.json").write_text(
        json.dumps([item["refined"] for item in refined], ensure_ascii=False),
        encoding="utf-8",
    )

    report = {
        "anchors": anchor_lines,
        "changed_lines": [item["line"] for item in refined if item["changed"]],
    }
    baseline_score = None
    refined_score = None
    if ground_truth:
        baseline_score = score_labels(
            [item["baseline"] for item in refined], ground_truth
        )
        refined_score = score_labels(
            [item["refined"] for item in refined], ground_truth
        )
        report["baseline"] = baseline_score
        report["refined"] = refined_score

    fieldnames = [
        "line", "time", "text", "duration_ms", "baseline", "refined",
        "coverage", "overlap_margin", "overlaps", "top_embedding_speaker",
        "top_embedding_similarity", "baseline_embedding_similarity", "changed",
        "reason", "truth", "baseline_correct", "refined_correct",
    ]
    with (output_dir / "line_diagnostics.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in refined:
            truth = ground_truth.get(item["line"], {}) if ground_truth else {}
            truth_label = truth.get("speaker_id", "")
            excluded = truth.get("exclude_from_single_speaker_eval", "").lower() == "true"
            baseline_role = (
                baseline_score["mapping"].get(item["baseline"])
                if baseline_score else ""
            )
            refined_role = (
                refined_score["mapping"].get(item["refined"])
                if refined_score else ""
            )
            writer.writerow({
                "line": item["line"],
                "time": item["time"],
                "text": item["text"],
                "duration_ms": item["duration_ms"],
                "baseline": item["baseline"],
                "refined": item["refined"],
                "coverage": f'{item["coverage"]:.4f}',
                "overlap_margin": f'{item["overlap_margin"]:.4f}',
                "overlaps": json.dumps(item["overlaps"], ensure_ascii=False),
                "top_embedding_speaker": item["top_embedding_speaker"],
                "top_embedding_similarity": f'{item["top_embedding_similarity"]:.4f}',
                "baseline_embedding_similarity": (
                    "" if item["baseline_embedding_similarity"] is None
                    else f'{item["baseline_embedding_similarity"]:.4f}'
                ),
                "changed": item["changed"],
                "reason": item["reason"],
                "truth": "" if excluded else truth_label,
                "baseline_correct": "" if excluded else truth_label == baseline_role,
                "refined_correct": "" if excluded else truth_label == refined_role,
            })

    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate generic ali_CAM short-line speaker refinement."
    )
    parser.add_argument("--audio", required=True)
    parser.add_argument("--srt", required=True)
    parser.add_argument("--raw-source", required=True, help="ali_CAM raw JSON or log file")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--ground-truth-csv")
    parser.add_argument(
        "--sv-model",
        default="models/models/damo/speech_campplus_sv_zh-cn_16k-common",
    )
    parser.add_argument("--short-ms", type=int, default=1500)
    parser.add_argument("--anchor-ms", type=int, default=1500)
    parser.add_argument("--min-audio-ms", type=int, default=500)
    parser.add_argument("--min-anchor-coverage", type=float, default=0.65)
    parser.add_argument("--min-anchor-margin", type=float, default=0.35)
    parser.add_argument("--min-anchors", type=int, default=2)
    parser.add_argument("--low-overlap-margin", type=float, default=0.15)
    parser.add_argument("--rare-max-lines", type=int, default=2)
    parser.add_argument("--min-similarity", type=float, default=0.25)
    parser.add_argument("--similarity-margin", type=float, default=0.12)
    args = parser.parse_args()

    subtitles = read_srt(Path(args.srt))
    segments = load_raw_segments(Path(args.raw_source))
    diagnostics = build_line_diagnostics(subtitles, segments)
    embeddings = load_embeddings(
        Path(args.audio), diagnostics, Path(args.sv_model), args.min_audio_ms
    )
    centroids, anchor_lines = build_centroids(
        diagnostics,
        embeddings,
        args.anchor_ms,
        args.min_anchor_coverage,
        args.min_anchor_margin,
        args.min_anchors,
    )
    refined = refine_speakers(
        diagnostics,
        embeddings,
        centroids,
        args.short_ms,
        args.low_overlap_margin,
        args.rare_max_lines,
        args.min_similarity,
        args.similarity_margin,
    )
    ground_truth = (
        load_ground_truth(Path(args.ground_truth_csv))
        if args.ground_truth_csv else {}
    )
    report = write_outputs(
        Path(args.output_dir), segments, refined, anchor_lines, ground_truth
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
