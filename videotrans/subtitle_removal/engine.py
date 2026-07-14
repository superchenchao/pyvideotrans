from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EngineSpec:
    root: Path
    python: Path

    def missing_files(self, mode: str) -> list[Path]:
        required = [self.root / "backend" / "main.py"]
        if mode == "force":
            required.append(self.root / "backend" / "models" / "sttn-auto" / "infer_model.pth")
        else:
            required.extend(
                [
                    self.root / "backend" / "models" / "sttn-det" / "sttn.pth",
                    self.root / "backend" / "models" / "V5" / "ch_det_fast" / "inference.json",
                ]
            )
        return [path for path in required if not path.exists()]


def _engine_roots(project_root: Path) -> list[Path]:
    configured = os.environ.get("PYVIDEOTRANS_SUBTITLE_REMOVER_HOME")
    candidates = [
        Path(configured).expanduser() if configured else None,
        project_root / "video-subtitle-remover",
        project_root / "third_party" / "video-subtitle-remover",
        project_root / ".codex_subtitle_removal_test" / "video-subtitle-remover",
    ]
    return [path.resolve() for path in candidates if path is not None]


def _python_candidates(project_root: Path, engine_root: Path) -> list[Path]:
    configured = os.environ.get("PYVIDEOTRANS_SUBTITLE_REMOVER_PYTHON")
    executable = "python.exe" if os.name == "nt" else "python"
    scripts_dir = "Scripts" if os.name == "nt" else "bin"
    candidates = [
        Path(configured).expanduser() if configured else None,
        engine_root / ".venv" / scripts_dir / executable,
        engine_root.parent / "vsr-venv310" / scripts_dir / executable,
        engine_root.parent / "vsr-venv" / scripts_dir / executable,
        project_root / ".venv" / scripts_dir / executable,
        Path(sys.executable),
    ]
    return [path.resolve() for path in candidates if path is not None]


def find_subtitle_remover_engine(project_root: str | Path) -> EngineSpec | None:
    project_root = Path(project_root).resolve()
    for engine_root in _engine_roots(project_root):
        if not (engine_root / "backend" / "main.py").is_file():
            continue
        python = next(
            (candidate for candidate in _python_candidates(project_root, engine_root) if candidate.is_file()),
            None,
        )
        if python:
            return EngineSpec(root=engine_root, python=python)
    return None
