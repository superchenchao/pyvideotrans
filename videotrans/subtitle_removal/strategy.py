from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


AUTO_BACKEND = "auto"
STTN_BACKEND = "sttn"
PROPAINTER_BACKEND = "propainter"
SUPPORTED_BACKENDS = (AUTO_BACKEND, STTN_BACKEND, PROPAINTER_BACKEND)

# ProPainter processes only the subtitle crop, not the full source frame. A
# A nominal 16 GB consumer card reports slightly less than 16384 MiB to
# nvidia-smi (for example, the RTX 5080 reports about 16303 MiB). Keep the
# threshold below that reported capacity while retaining the 12 GB free-memory
# guard, otherwise these cards incorrectly fall back to STTN.
PROPAINTER_MIN_TOTAL_VRAM_MB = 15 * 1024
PROPAINTER_MIN_FREE_VRAM_MB = 12 * 1024
STRATEGY_VERSION = 3


@dataclass(frozen=True)
class GPUProfile:
    name: str = ""
    total_vram_mb: int = 0
    free_vram_mb: int = 0
    cuda_available: bool = False


@dataclass(frozen=True)
class BackendSelection:
    backend: str
    reason: str
    propainter_batch_size: int = 0


def requested_backend() -> str:
    value = os.environ.get("PYVIDEOTRANS_SUBTITLE_INPAINT", AUTO_BACKEND).strip().lower()
    return value if value in SUPPORTED_BACKENDS else AUTO_BACKEND


def propainter_required_files(engine_root: str | Path) -> tuple[Path, ...]:
    models_dir = Path(engine_root) / "backend" / "models"
    model_dir = models_dir / "propainter"
    return (
        model_dir / "ProPainter.pth",
        model_dir / "raft-things.pth",
        model_dir / "recurrent_flow_completion.pth",
        # ProPainter delegates isolated one-frame intervals to Big-Lama.
        # Without this file a long run fails after OCR and then repeats the
        # whole removal pass with STTN.
        models_dir / "big-lama" / "big-lama.pt",
    )


def propainter_models_ready(engine_root: str | Path) -> bool:
    return all(path.is_file() and path.stat().st_size > 0 for path in propainter_required_files(engine_root))


def _propainter_batch_size(free_vram_mb: int) -> int:
    if free_vram_mb >= 20 * 1024:
        return 60
    if free_vram_mb >= 16 * 1024:
        return 45
    return 30


def select_inpaint_backend(
        *, requested: str, gpu: GPUProfile,
        propainter_ready: bool) -> BackendSelection:
    requested = requested.strip().lower()
    if requested not in SUPPORTED_BACKENDS:
        requested = AUTO_BACKEND

    if requested == STTN_BACKEND:
        return BackendSelection(STTN_BACKEND, "STTN was explicitly requested")

    if requested == PROPAINTER_BACKEND:
        if not propainter_ready:
            raise RuntimeError("ProPainter was requested but its model files are incomplete")
        if not gpu.cuda_available:
            raise RuntimeError("ProPainter was requested but CUDA is unavailable")
        return BackendSelection(
            PROPAINTER_BACKEND,
            "ProPainter was explicitly requested",
            _propainter_batch_size(gpu.free_vram_mb),
        )

    if not propainter_ready:
        return BackendSelection(STTN_BACKEND, "ProPainter model files are unavailable")
    if not gpu.cuda_available:
        return BackendSelection(STTN_BACKEND, "CUDA is unavailable")
    if gpu.total_vram_mb < PROPAINTER_MIN_TOTAL_VRAM_MB:
        return BackendSelection(
            STTN_BACKEND,
            f"GPU VRAM is below {PROPAINTER_MIN_TOTAL_VRAM_MB // 1024} GB",
        )
    if gpu.free_vram_mb < PROPAINTER_MIN_FREE_VRAM_MB:
        return BackendSelection(
            STTN_BACKEND,
            f"free GPU VRAM is below {PROPAINTER_MIN_FREE_VRAM_MB // 1024} GB",
        )
    return BackendSelection(
        PROPAINTER_BACKEND,
        "CUDA GPU and model files satisfy the high-quality threshold",
        _propainter_batch_size(gpu.free_vram_mb),
    )


def detect_nvidia_gpu_profile() -> GPUProfile:
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=creationflags,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return GPUProfile()
    if result.returncode != 0 or not result.stdout.strip():
        return GPUProfile()
    try:
        name, total, free = (part.strip() for part in result.stdout.splitlines()[0].split(",", 2))
        return GPUProfile(
            name=name,
            total_vram_mb=int(total),
            free_vram_mb=int(free),
            cuda_available=True,
        )
    except (TypeError, ValueError):
        return GPUProfile()


def strategy_cache_key(engine_root: str | Path | None) -> str:
    requested = requested_backend()
    gpu = detect_nvidia_gpu_profile()
    ready = bool(engine_root) and propainter_models_ready(engine_root)
    try:
        selection = select_inpaint_backend(
            requested=requested,
            gpu=gpu,
            propainter_ready=ready,
        )
        selected = selection.backend
    except RuntimeError:
        selected = "unavailable"
    gpu_name = gpu.name or "no-cuda"
    return f"v{STRATEGY_VERSION}:{requested}:{selected}:{gpu_name}:{gpu.total_vram_mb}"
