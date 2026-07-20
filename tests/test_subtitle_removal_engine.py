import os
from pathlib import Path

import pytest

from videotrans.subtitle_removal.engine import _python_candidates


def test_python_candidates_preserve_virtualenv_launcher_symlink(
        tmp_path, monkeypatch):
    target = tmp_path / ("python-base.exe" if os.name == "nt" else "python-base")
    target.write_bytes(b"python")
    launcher = (
        tmp_path
        / "configured-venv"
        / ("Scripts" if os.name == "nt" else "bin")
        / ("python.exe" if os.name == "nt" else "python")
    )
    launcher.parent.mkdir(parents=True)
    try:
        launcher.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlinks are unavailable: {error}")

    monkeypatch.setenv(
        "PYVIDEOTRANS_SUBTITLE_REMOVER_PYTHON",
        str(launcher),
    )

    candidates = _python_candidates(tmp_path, tmp_path / "engine")

    assert candidates[0] == Path(os.path.abspath(launcher))
    assert candidates[0] != target.resolve()
