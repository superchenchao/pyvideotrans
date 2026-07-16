from pathlib import Path

import pytest

from videotrans.subtitle_ocr.runner import _roi_command_args, _worker_environment


def test_worker_environment_removes_parent_python_overrides(monkeypatch):
    monkeypatch.setenv("PYTHONHOME", "parent-home")
    monkeypatch.setenv("PYTHONPATH", "parent-packages")
    monkeypatch.setenv("PATH", "system-path")
    python = Path("E:/ocr-env/Scripts/python.exe")

    env = _worker_environment(python)

    assert "PYTHONHOME" not in env
    assert "PYTHONPATH" not in env
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["PYTHONUTF8"] == "1"
    assert env["VIRTUAL_ENV"] == str(python.parent.parent)
    assert env["PATH"].startswith(str(python.parent))


def test_roi_command_args_converts_saved_xywh_to_worker_edges():
    args = _roi_command_args([0.05, 0.68, 0.9, 0.08])

    assert args == [
        "--roi-left", "0.05",
        "--roi-top", "0.68",
        "--roi-right", "0.95",
        "--roi-bottom", "0.76",
    ]


def test_roi_command_args_rejects_region_outside_video():
    with pytest.raises(ValueError, match="inside the video frame"):
        _roi_command_args([0.9, 0.7, 0.2, 0.1])


def test_roi_command_args_rejects_non_finite_values():
    with pytest.raises(ValueError, match="finite numbers"):
        _roi_command_args([0.1, float("nan"), 0.8, 0.1])
