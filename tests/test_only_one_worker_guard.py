from uuid import uuid4

from videotrans.configure.config import app_cfg
from videotrans.task.only_one import (
    Worker,
    _claim_active_worker,
    _release_active_worker,
)


def test_same_video_worker_cannot_be_claimed_twice():
    worker_uuid = str(uuid4())
    try:
        assert _claim_active_worker(worker_uuid) is True
        assert _claim_active_worker(worker_uuid) is False
    finally:
        _release_active_worker(worker_uuid)

    assert _claim_active_worker(worker_uuid) is True
    _release_active_worker(worker_uuid)


def test_different_video_workers_can_run_together():
    first_uuid = str(uuid4())
    second_uuid = str(uuid4())
    try:
        assert _claim_active_worker(first_uuid) is True
        assert _claim_active_worker(second_uuid) is True
    finally:
        _release_active_worker(first_uuid)
        _release_active_worker(second_uuid)


def test_stopped_video_worker_exits_even_after_global_status_returns_to_running(
        monkeypatch):
    worker_uuid = str(uuid4())
    worker = Worker(file=None, cfg=None)
    worker.uuid = worker_uuid
    monkeypatch.setattr(app_cfg, "current_status", "ing")
    app_cfg.stoped_uuid_set.add(worker_uuid)
    try:
        assert worker._exit() is True
    finally:
        app_cfg.stoped_uuid_set.discard(worker_uuid)
