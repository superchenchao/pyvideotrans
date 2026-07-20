import numpy as np
import pytest
from pathlib import Path
from types import SimpleNamespace

from videotrans.subtitle_removal.blending import (
    composite_repaired_frames,
    repair_is_suspiciously_black,
    soft_alpha_mask,
)
from videotrans.subtitle_removal.strategy import (
    GPUProfile,
    PROPAINTER_BACKEND,
    STTN_BACKEND,
    propainter_required_files,
    select_inpaint_backend,
)


def test_auto_selects_propainter_for_4090d_class_gpu():
    selection = select_inpaint_backend(
        requested="auto",
        gpu=GPUProfile(
            name="NVIDIA GeForce RTX 4090 D",
            total_vram_mb=24564,
            free_vram_mb=22500,
            cuda_available=True,
        ),
        propainter_ready=True,
    )

    assert selection.backend == PROPAINTER_BACKEND
    assert selection.propainter_batch_size == 60


def test_auto_selects_propainter_for_nominal_16gb_rtx5080():
    selection = select_inpaint_backend(
        requested="auto",
        gpu=GPUProfile(
            name="NVIDIA GeForce RTX 5080",
            total_vram_mb=16303,
            free_vram_mb=14884,
            cuda_available=True,
        ),
        propainter_ready=True,
    )

    assert selection.backend == PROPAINTER_BACKEND
    assert selection.propainter_batch_size == 30


@pytest.mark.parametrize(
    ("gpu", "models_ready"),
    [
        (GPUProfile(name="GTX 1050 Ti", total_vram_mb=4096, free_vram_mb=3500, cuda_available=True), True),
        (GPUProfile(name="RTX 4090 D", total_vram_mb=24564, free_vram_mb=8000, cuda_available=True), True),
        (GPUProfile(name="RTX 4090 D", total_vram_mb=24564, free_vram_mb=22000, cuda_available=True), False),
        (GPUProfile(), True),
    ],
)
def test_auto_falls_back_to_sttn_when_high_quality_requirements_are_not_met(
        gpu, models_ready):
    selection = select_inpaint_backend(
        requested="auto",
        gpu=gpu,
        propainter_ready=models_ready,
    )

    assert selection.backend == STTN_BACKEND


def test_forced_propainter_requires_models():
    with pytest.raises(RuntimeError, match="model files"):
        select_inpaint_backend(
            requested="propainter",
            gpu=GPUProfile(
                name="RTX 4090 D",
                total_vram_mb=24564,
                free_vram_mb=22000,
                cuda_available=True,
            ),
            propainter_ready=False,
        )


def test_propainter_requires_big_lama_for_single_frame_fallback(tmp_path):
    required = propainter_required_files(tmp_path)

    assert tmp_path / "backend/models/big-lama/big-lama.pt" in required


def test_soft_composite_preserves_original_outside_mask():
    original = np.full((32, 32, 3), 40, dtype=np.uint8)
    repaired = np.full((32, 32, 3), 200, dtype=np.uint8)
    mask = np.zeros((32, 32), dtype=np.uint8)
    mask[10:22, 10:22] = 255

    alpha = soft_alpha_mask(mask, feather_pixels=6)
    output = composite_repaired_frames(
        [original], [repaired], mask, feather_pixels=6,
    )[0]

    assert np.array_equal(output[0, 0], original[0, 0])
    assert np.array_equal(output[15, 15], repaired[15, 15])
    assert 40 < int(output[8, 15, 0]) < 200
    assert alpha[15, 15, 0] == 1.0


def test_roi_composite_matches_full_frame_reference_exactly():
    rng = np.random.default_rng(20260717)
    original = rng.integers(0, 256, size=(96, 160, 3), dtype=np.uint8)
    repaired = rng.integers(0, 256, size=(96, 160, 3), dtype=np.uint8)
    mask = np.zeros((96, 160), dtype=np.uint8)
    mask[68:82, 24:136] = 255
    alpha = soft_alpha_mask(mask, feather_pixels=10)
    expected = np.clip(
        repaired.astype(np.float32) * alpha
        + original.astype(np.float32) * (1.0 - alpha),
        0,
        255,
    ).astype(np.uint8)

    actual = composite_repaired_frames(
        [original], [repaired], mask, feather_pixels=10,
    )[0]

    assert np.array_equal(actual, expected)


def test_empty_mask_returns_an_independent_original_frame():
    original = np.full((12, 16, 3), 80, dtype=np.uint8)
    repaired = np.full((12, 16, 3), 180, dtype=np.uint8)

    output = composite_repaired_frames(
        [original], [repaired], np.zeros((12, 16), dtype=np.uint8),
    )[0]

    assert np.array_equal(output, original)
    assert output is not original


def test_black_output_guard_detects_known_propainter_failure():
    mask = np.full((16, 16), 255, dtype=np.uint8)
    original = np.full((16, 16, 3), 80, dtype=np.uint8)
    invalid = np.zeros((16, 16, 3), dtype=np.uint8)
    valid_dark = np.full((16, 16, 3), 12, dtype=np.uint8)

    assert repair_is_suspiciously_black([original], [invalid], mask)
    assert not repair_is_suspiciously_black([original], [valid_dark], mask)


def test_auto_worker_failure_retries_with_sttn(tmp_path, monkeypatch):
    from videotrans.subtitle_removal import automation

    engine = SimpleNamespace(
        root=tmp_path,
        python=tmp_path / "python.exe",
        missing_files=lambda *args, **kwargs: [],
    )
    commands = []
    output_file = tmp_path / "cleaned.mp4"

    class FakeProcess:
        def __init__(self, command):
            self.command = command
            self.return_code = None
            backend = command[command.index("--inpaint-engine") + 1]
            if backend == "auto":
                self.stdout = iter([
                    'PYVT_EVENT {"type":"backend_selected","backend":"propainter","gpu":"RTX 4090 D","reason":"test"}\n',
                    'PYVT_EVENT {"type":"error","message":"out of memory"}\n',
                ])
                self.planned_return_code = 1
            else:
                output_file.write_bytes(b"video")
                self.stdout = iter([
                    'PYVT_EVENT {"type":"backend_selected","backend":"sttn","gpu":"RTX 4090 D","reason":"fallback"}\n',
                ])
                self.planned_return_code = 0

        def wait(self, timeout=None):
            self.return_code = self.planned_return_code
            return self.return_code

        def poll(self):
            return self.return_code

        def terminate(self):
            self.return_code = -1

    def fake_popen(command, **kwargs):
        commands.append(command)
        return FakeProcess(command)

    monkeypatch.setattr(automation, "find_subtitle_remover_engine", lambda root: engine)
    monkeypatch.setattr(automation, "requested_backend", lambda: "auto")
    monkeypatch.setattr(automation.subprocess, "Popen", fake_popen)

    result = automation.remove_burned_subtitles(
        input_file=(tmp_path / "input.mp4").as_posix(),
        output_file=output_file.as_posix(),
        rect=(10, 20, 100, 40),
        duration_ms=1000,
    )

    assert Path(result) == output_file.resolve()
    assert len(commands) == 2
    assert commands[1][commands[1].index("--inpaint-engine") + 1] == "sttn"
    assert '"backend": "sttn"' in Path(
        f"{output_file}.inpaint.json"
    ).read_text(encoding="utf-8")
