from collections import Counter, defaultdict
import threading

import numpy as np
import soundfile as sf


_VERIFIER_CACHE = {}
_VERIFIER_LOCK = threading.RLock()


def _normalize_segments(diarizations):
    segments = []
    for item in diarizations:
        if isinstance(item, dict):
            start = item["start"]
            end = item["end"]
            speaker = item["speaker"]
        elif len(item) == 2 and isinstance(item[0], (list, tuple)):
            (start, end), speaker = item
        else:
            start, end, speaker = item
        speaker_text = str(speaker)
        if not speaker_text.startswith("spk"):
            speaker_text = f"spk{int(speaker)}"
        segments.append({
            "start": int(start),
            "end": int(end),
            "speaker": speaker_text,
        })
    return segments


def build_line_diagnostics(subtitles, diarizations):
    segments = _normalize_segments(diarizations)
    diagnostics = []
    for index, subtitle in enumerate(subtitles):
        start, end = int(subtitle[0]), int(subtitle[1])
        duration = end - start
        overlaps = defaultdict(int)
        for segment in segments:
            overlap = max(
                0,
                min(end, segment["end"]) - max(start, segment["start"]),
            )
            if overlap:
                overlaps[segment["speaker"]] += overlap
        ranked = sorted(overlaps.items(), key=lambda item: (-item[1], item[0]))
        baseline = ranked[0][0] if ranked else "spk0"
        best_overlap = ranked[0][1] if ranked else 0
        second_overlap = ranked[1][1] if len(ranked) > 1 else 0
        diagnostics.append({
            "line": index + 1,
            "start": start,
            "end": end,
            "duration_ms": duration,
            "baseline": baseline,
            "refined": baseline,
            "overlaps": dict(ranked),
            "coverage": best_overlap / duration if duration > 0 else 0.0,
            "overlap_margin": (
                (best_overlap - second_overlap) / duration if duration > 0 else 0.0
            ),
            "changed": False,
            "reason": "",
        })
    return diagnostics


def _load_embeddings(audio_file, diagnostics, indices, model_path, min_audio_ms):
    audio, sample_rate = sf.read(audio_file, dtype="float32")
    if audio.ndim > 1:
        audio = audio[:, 0]
    min_samples = round(min_audio_ms * sample_rate / 1000)
    ordered_indices = sorted(indices)
    with _VERIFIER_LOCK:
        cache_key = str(model_path)
        verifier = _VERIFIER_CACHE.get(cache_key)
        if verifier is None:
            from modelscope.pipelines import pipeline
            verifier = pipeline(
                "speaker-verification",
                cache_key,
                device="cpu",
                disable_update=True,
                disable_progress_bar=True,
                disable_log=True,
            )
            _VERIFIER_CACHE[cache_key] = verifier
        embedding_batches = []
        for offset in range(0, len(ordered_indices), 64):
            batch_indices = ordered_indices[offset:offset + 64]
            arrays = []
            for index in batch_indices:
                item = diagnostics[index]
                start = max(0, round(item["start"] * sample_rate / 1000))
                end = min(len(audio), round(item["end"] * sample_rate / 1000))
                samples = audio[start:end]
                if len(samples) < min_samples:
                    samples = np.pad(samples, (0, min_samples - len(samples)))
                arrays.append(samples)
            result = verifier(arrays, output_emb=True)
            embedding_batches.append(np.asarray(result["embs"], dtype=np.float32))
    embeddings = np.concatenate(embedding_batches, axis=0)
    embeddings /= np.linalg.norm(embeddings, axis=1, keepdims=True) + 1e-9
    return dict(zip(ordered_indices, embeddings))


def refine_cam_speaker_labels(
    *,
    audio_file,
    subtitles,
    diarizations,
    model_path,
    short_ms=1500,
    anchor_ms=1500,
    min_audio_ms=500,
    min_anchor_coverage=0.65,
    min_anchor_margin=0.35,
    min_anchors=2,
    low_overlap_margin=0.15,
    rare_max_lines=2,
    min_similarity=0.25,
    similarity_margin=0.12,
):
    diagnostics = build_line_diagnostics(subtitles, diarizations)
    counts = Counter(item["baseline"] for item in diagnostics)

    anchor_indices = [
        index for index, item in enumerate(diagnostics)
        if item["duration_ms"] >= anchor_ms
        and item["coverage"] >= min_anchor_coverage
        and item["overlap_margin"] >= min_anchor_margin
    ]
    eligible_indices = []
    for index, item in enumerate(diagnostics):
        continues_current_turn = (
            index + 1 < len(diagnostics)
            and diagnostics[index + 1]["baseline"] == item["baseline"]
        )
        eligible = (
            item["duration_ms"] <= short_ms
            or item["overlap_margin"] <= low_overlap_margin
            or counts[item["baseline"]] <= rare_max_lines
        )
        if eligible and not continues_current_turn:
            eligible_indices.append(index)

    required_indices = set(anchor_indices) | set(eligible_indices)
    if not required_indices:
        return [item["baseline"] for item in diagnostics], {
            "changed_lines": [],
            "anchors": {},
            "lines": diagnostics,
        }
    embeddings = _load_embeddings(
        audio_file, diagnostics, required_indices, model_path, min_audio_ms
    )

    anchors = defaultdict(list)
    anchor_lines = defaultdict(list)
    for index in anchor_indices:
        speaker = diagnostics[index]["baseline"]
        anchors[speaker].append(embeddings[index])
        anchor_lines[speaker].append(index + 1)
    centroids = {}
    accepted_anchor_lines = {}
    for speaker, values in anchors.items():
        if len(values) < min_anchors:
            continue
        centroid = np.mean(values, axis=0)
        centroids[speaker] = centroid / (np.linalg.norm(centroid) + 1e-9)
        accepted_anchor_lines[speaker] = anchor_lines[speaker]

    if len(centroids) < 2:
        return [item["baseline"] for item in diagnostics], {
            "changed_lines": [],
            "anchors": accepted_anchor_lines,
            "lines": diagnostics,
        }

    for index in eligible_indices:
        item = diagnostics[index]
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
        reasons = []
        if item["duration_ms"] <= short_ms:
            reasons.append("short")
        if item["overlap_margin"] <= low_overlap_margin:
            reasons.append("low_overlap_margin")
        if counts[current] <= rare_max_lines:
            reasons.append("rare_speaker")

        item["top_embedding_speaker"] = top_speaker
        item["top_embedding_similarity"] = top_similarity
        item["baseline_embedding_similarity"] = current_similarity
        if (
            top_speaker != current
            and top_similarity >= min_similarity
            and (
                current_similarity is None
                or top_similarity - current_similarity >= similarity_margin
            )
        ):
            item["refined"] = top_speaker
            item["changed"] = True
            item["reason"] = "+".join(reasons)

    labels = [item["refined"] for item in diagnostics]
    return labels, {
        "changed_lines": [item["line"] for item in diagnostics if item["changed"]],
        "anchors": accepted_anchor_lines,
        "lines": diagnostics,
    }
