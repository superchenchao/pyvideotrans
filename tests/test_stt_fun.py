import json

from videotrans.process.stt_fun import (
    select_faster_compute_type,
    write_faster_result_sidecar,
)
from videotrans.task.taskcfg import SrtItem


def test_select_faster_compute_type_uses_cuda_int8_when_float16_is_unsupported():
    assert select_faster_compute_type(
        "float16",
        is_cuda=True,
        supported_types={"int8", "int8_float32", "float32"},
    ) == "int8"


def test_select_faster_compute_type_keeps_supported_value():
    assert select_faster_compute_type(
        "float16",
        is_cuda=True,
        supported_types={"int8", "float16", "float32"},
    ) == "float16"


def test_select_faster_compute_type_keeps_automatic_modes():
    assert select_faster_compute_type(
        "auto",
        is_cuda=False,
        supported_types={"int8", "float32"},
    ) == "auto"


def test_write_faster_result_sidecar_serializes_subtitles(tmp_path):
    logs_file = tmp_path / "recognition.log"
    write_faster_result_sidecar(
        logs_file,
        [SrtItem(text="你好", start_time=100, end_time=500)],
        None,
    )

    payload = json.loads(
        (tmp_path / "recognition.log.result.json").read_text(encoding="utf-8")
    )
    assert payload["error"] is None
    assert payload["data"][0]["text"] == "你好"
