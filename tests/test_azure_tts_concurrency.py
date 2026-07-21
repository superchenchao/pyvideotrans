from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor

import pytest

from videotrans.tts import _azuretts
from videotrans.tts._azuretts import (
    AzureTTS,
    is_transient_azure_error,
    normalize_azure_runtime_settings,
    reduce_azure_concurrency,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ((None, None, None), (5, 0.0, True)),
        (("7", "0.3", "false"), (7, 0.3, False)),
        ((0, -2, True), (1, 0.0, True)),
        ((99, 99, False), (10, 5.0, False)),
        (("bad", "bad", "yes"), (5, 0.0, True)),
    ],
)
def test_normalize_azure_runtime_settings(raw, expected):
    assert normalize_azure_runtime_settings(*raw) == expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("ServiceTimeout", True),
        ("TooManyRequests: status code: 429", True),
        ("request was throttled", True),
        ("AuthenticationFailure", False),
        (None, False),
    ],
)
def test_is_transient_azure_error(message, expected):
    assert is_transient_azure_error(message) is expected


def test_reduce_azure_concurrency_uses_safe_steps():
    assert reduce_azure_concurrency(5) == 3
    assert reduce_azure_concurrency(3) == 1
    assert reduce_azure_concurrency(1) == 1


def test_scheduler_reduces_future_submissions_after_timeout(tmp_path, monkeypatch, caplog):
    created_workers = []

    class RecordingPool(RealThreadPoolExecutor):
        def __init__(self, max_workers, *args, **kwargs):
            created_workers.append(max_workers)
            super().__init__(max_workers=max_workers, *args, **kwargs)

    monkeypatch.setattr(_azuretts, "ThreadPoolExecutor", RecordingPool)

    runner = AzureTTS.__new__(AzureTTS)
    runner.queue_tts = [
        {"text": f"line {idx}", "filename": str(tmp_path / f"{idx}.wav")}
        for idx in range(9)
    ]
    runner.len = len(runner.queue_tts)
    runner.dub_nums = 5
    runner.wait_sec = 0
    runner.adaptive_concurrency = True
    runner.error = None
    runner._exit = lambda: False
    messages = []
    runner.signal = lambda **kwargs: messages.append(kwargs.get("text"))
    attempts = {}

    def item_task(item, idx):
        attempts[idx] = attempts.get(idx, 0) + 1
        if idx == 0 and attempts[idx] == 1:
            return RuntimeError("ServiceTimeout")
        return None

    runner._item_task = item_task

    runner._exec()

    assert created_workers == [5]
    assert attempts[0] == 2
    assert "reducing concurrency 5 -> 3" in caplog.text
    assert messages[-1] == "TTS ended ..."
    assert any(message == "TTS: [9/9] ..." for message in messages)


def test_scheduler_keeps_configured_limit_when_adaptive_is_off(tmp_path, monkeypatch):
    created_workers = []

    class RecordingPool(RealThreadPoolExecutor):
        def __init__(self, max_workers, *args, **kwargs):
            created_workers.append(max_workers)
            super().__init__(max_workers=max_workers, *args, **kwargs)

    monkeypatch.setattr(_azuretts, "ThreadPoolExecutor", RecordingPool)

    runner = AzureTTS.__new__(AzureTTS)
    runner.queue_tts = [
        {"text": f"line {idx}", "filename": str(tmp_path / f"{idx}.wav")}
        for idx in range(9)
    ]
    runner.len = len(runner.queue_tts)
    runner.dub_nums = 5
    runner.wait_sec = 0
    runner.adaptive_concurrency = False
    runner.error = None
    runner._exit = lambda: False
    runner.signal = lambda **kwargs: None
    runner._item_task = lambda item, idx: RuntimeError("ServiceTimeout") if idx == 0 else None

    runner._exec()

    assert created_workers == [5]
