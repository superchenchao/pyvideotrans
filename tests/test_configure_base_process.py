from videotrans.configure.base import BaseCon
from videotrans.process.signelobj import GlobalProcessManager


class _CompletedFuture:
    def done(self):
        return True

    def result(self):
        return "ok", None


class _ThreadWithoutWorker:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


def test_new_process_creates_nested_log_directory(tmp_path, monkeypatch):
    logs_file = tmp_path / "task" / "episode" / "recognition.log"

    def submit_task_cpu(callback, **kwargs):
        assert logs_file.is_file()
        return _CompletedFuture()

    monkeypatch.setattr(GlobalProcessManager, "submit_task_cpu", submit_task_cpu)
    monkeypatch.setattr(
        "videotrans.configure.base.threading.Thread", _ThreadWithoutWorker
    )

    task = BaseCon(signal_handler=lambda message: None)
    result = task._new_process(
        callback=lambda: None,
        title="nested process log",
        kwargs={"logs_file": str(logs_file)},
    )

    assert result == "ok"
    assert logs_file.parent.is_dir()
