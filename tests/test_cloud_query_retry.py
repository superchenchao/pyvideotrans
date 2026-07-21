import json

import pytest

from videotrans.subtitle_removal import cloud


class _FlakyQueryClient:
    def __init__(self, failures, result=None):
        self.failures = failures
        self.result = result or {"Status": "Analysing"}
        self.calls = 0

    def query(self, task_id):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("remote disconnected")
        return self.result


def test_query_retries_without_changing_task_id(tmp_path, monkeypatch):
    client = _FlakyQueryClient(failures=2)
    state_path = tmp_path / "cloud.json"
    state = {"status": "processing", "job_id": "job-123"}
    sleeps = []
    logs = []
    monkeypatch.setattr(cloud, "_poll_sleep", lambda seconds, _cancel: sleeps.append(seconds))

    result = cloud._query_with_retry(
        client=client,
        task_id="job-123",
        provider_name="阿里云 IMS",
        state=state,
        state_path=state_path,
        log_callback=logs.append,
        cancel_callback=None,
    )

    assert result == {"Status": "Analysing"}
    assert client.calls == 3
    assert sleeps == [2, 4]
    assert state == {"status": "processing", "job_id": "job-123"}
    assert "连接已恢复" in logs[-1]
    assert json.loads(state_path.read_text(encoding="utf-8"))["job_id"] == "job-123"


def test_query_exhaustion_preserves_task_for_later_resume(tmp_path, monkeypatch):
    client = _FlakyQueryClient(failures=99)
    state_path = tmp_path / "cloud.json"
    state = {"status": "processing", "job_id": "job-456"}
    monkeypatch.setattr(cloud, "_poll_sleep", lambda *_args: None)

    with pytest.raises(RuntimeError, match="查询连续失败 3 次"):
        cloud._query_with_retry(
            client=client,
            task_id="job-456",
            provider_name="阿里云 IMS",
            state=state,
            state_path=state_path,
            log_callback=None,
            cancel_callback=None,
            max_attempts=3,
        )

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert client.calls == 3
    assert saved["job_id"] == "job-456"
    assert saved["status"] == "query_retry"
