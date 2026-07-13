import numpy as np

from videotrans.process import speaker_refine


def test_build_line_diagnostics_uses_overlap_and_fallback():
    subtitles = [[0, 1000], [1000, 2000], [3000, 3500]]
    diarizations = [
        [[0, 400], 0],
        [[400, 1000], 1],
        [[1000, 2000], "spk1"],
    ]

    result = speaker_refine.build_line_diagnostics(subtitles, diarizations)

    assert [item["baseline"] for item in result] == ["spk1", "spk1", "spk0"]
    assert result[0]["coverage"] == 0.6
    assert result[0]["overlap_margin"] == 0.2
    assert result[2]["coverage"] == 0.0


def test_refine_reassigns_short_line_to_stable_voice_centroid(monkeypatch):
    subtitles = [
        [0, 2000],
        [2100, 4100],
        [4200, 4700],
        [4800, 6800],
        [6900, 8900],
    ]
    diarizations = [
        [[0, 2000], "spk0"],
        [[2100, 4100], "spk0"],
        [[4200, 4700], "spk0"],
        [[4800, 6800], "spk1"],
        [[6900, 8900], "spk1"],
    ]

    def fake_embeddings(audio_file, diagnostics, indices, model_path, min_audio_ms):
        values = {
            0: np.array([1.0, 0.0]),
            1: np.array([1.0, 0.0]),
            2: np.array([0.0, 1.0]),
            3: np.array([0.0, 1.0]),
            4: np.array([0.0, 1.0]),
        }
        return {index: values[index] for index in indices}

    monkeypatch.setattr(speaker_refine, "_load_embeddings", fake_embeddings)

    labels, report = speaker_refine.refine_cam_speaker_labels(
        audio_file="unused.wav",
        subtitles=subtitles,
        diarizations=diarizations,
        model_path="unused-model",
    )

    assert labels == ["spk0", "spk0", "spk1", "spk1", "spk1"]
    assert report["changed_lines"] == [3]


def test_refine_protects_continuing_turn(monkeypatch):
    subtitles = [
        [0, 2000],
        [2100, 4100],
        [4200, 4700],
        [4800, 6800],
        [6900, 8900],
        [9000, 11000],
        [11100, 13100],
    ]
    diarizations = [
        [[0, 2000], "spk0"],
        [[2100, 4100], "spk0"],
        [[4200, 4700], "spk0"],
        [[4800, 6800], "spk0"],
        [[6900, 8900], "spk1"],
        [[9000, 11000], "spk1"],
        [[11100, 13100], "spk1"],
    ]

    def fake_embeddings(audio_file, diagnostics, indices, model_path, min_audio_ms):
        values = {
            0: np.array([1.0, 0.0]),
            1: np.array([1.0, 0.0]),
            2: np.array([0.0, 1.0]),
            3: np.array([1.0, 0.0]),
            4: np.array([0.0, 1.0]),
            5: np.array([0.0, 1.0]),
            6: np.array([0.0, 1.0]),
        }
        return {index: values[index] for index in indices}

    monkeypatch.setattr(speaker_refine, "_load_embeddings", fake_embeddings)

    labels, report = speaker_refine.refine_cam_speaker_labels(
        audio_file="unused.wav",
        subtitles=subtitles,
        diarizations=diarizations,
        model_path="unused-model",
    )

    assert labels[2] == "spk0"
    assert 3 not in report["changed_lines"]
