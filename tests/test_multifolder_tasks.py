import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

from videotrans.component.multifolder_tasks import (
    LanguageSpec,
    MultiFolderScheduler,
    ProjectSpec,
    ReviewCenter,
    ReviewRequest,
    _discover_videos,
)
from videotrans.configure.base import BaseCon


def test_manual_review_gate_waits_until_explicit_approval():
    scheduler = MultiFolderScheduler([], {})
    request = object()
    with scheduler._condition:
        scheduler._pending["review-1"] = request

    finished = threading.Event()

    def wait_for_gate():
        scheduler._wait_for_reviews()
        finished.set()

    thread = threading.Thread(target=wait_for_gate)
    thread.start()
    time.sleep(0.05)
    assert finished.is_set() is False

    scheduler.approve("review-1")
    thread.join(timeout=1)
    assert finished.is_set() is True


def test_task_signal_handler_does_not_use_global_queue():
    received = []
    task = BaseCon(signal_handler=received.append)
    task.signal(text="isolated", type="logs")
    assert received == [{"text": "isolated", "type": "logs", "uuid": None}]


def test_video_discovery_excludes_generated_output(tmp_path):
    source = tmp_path / "剧集"
    output = source / "_video_out" / "剧集"
    source.mkdir()
    output.mkdir(parents=True)
    (source / "01.mp4").write_bytes(b"video")
    (source / "readme.txt").write_text("ignore", encoding="utf-8")
    (output / "01.mp4").write_bytes(b"generated")

    assert _discover_videos(source.as_posix()) == [(source / "01.mp4").as_posix()]


def test_output_isolated_by_project_and_language(tmp_path):
    project_folder = tmp_path / "长夜微星"
    project = ProjectSpec("p1", project_folder.as_posix(), [])
    root = MultiFolderScheduler._output_root(project)
    assert root == tmp_path / "_video_out" / "长夜微星"
    assert root / "en" != root / "fr"


def test_scheduler_runs_all_languages_episode_major(tmp_path, monkeypatch):
    folder = tmp_path / "series"
    folder.mkdir()
    videos = []
    for name in ("01.mp4", "02.mp4"):
        path = folder / name
        path.write_bytes(b"video")
        videos.append(path.as_posix())
    project = ProjectSpec(
        project_id="p1",
        folder=folder.as_posix(),
        videos=videos,
        manual_review=False,
        languages=[
            LanguageSpec("English", "en"),
            LanguageSpec("French", "fr"),
        ],
    )
    scheduler = MultiFolderScheduler([project], {})
    order = []

    class FakeTask:
        should_dubbing = False
        should_recogn2 = False
        hasend = False

        def align(self):
            pass

        def recogn2pass(self):
            pass

        def assembling(self):
            pass

        def task_done(self):
            pass

    def source_task(_project, video, _work_root):
        return SimpleNamespace(hasend=False, cfg=SimpleNamespace())

    def language_task(_project, language, video, *_args):
        order.append((Path(video).name, language.code))
        return FakeTask()

    monkeypatch.setattr(scheduler, "_source_task", source_task)
    monkeypatch.setattr(scheduler, "_language_task", language_task)
    scheduler.run()

    assert order == [
        ("01.mp4", "en"),
        ("01.mp4", "fr"),
        ("02.mp4", "en"),
        ("02.mp4", "fr"),
    ]


def test_completion_cache_is_invalidated_when_language_config_changes(tmp_path):
    folder = tmp_path / "series"
    folder.mkdir()
    video = folder / "01.mp4"
    video.write_bytes(b"video")
    language = LanguageSpec("English", "en", "No")
    project = ProjectSpec("p1", folder.as_posix(), [video.as_posix()], languages=[language])
    scheduler = MultiFolderScheduler([project], {"subtitle_type": 0, "clear_cache": False})
    marker = scheduler._completion_marker(project, video.as_posix(), language)
    marker.parent.mkdir(parents=True)
    (marker.parent / "en.srt").write_text("subtitle", encoding="utf-8")
    scheduler._mark_completed(project, video.as_posix(), language)

    assert scheduler._is_completed(project, video.as_posix(), language) is True
    language.voice = "Different Voice"
    assert scheduler._is_completed(project, video.as_posix(), language) is False


def test_review_request_does_not_open_window_automatically():
    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    center = ReviewCenter(parent, lambda _request_id: None)
    request = ReviewRequest(
        request_id="r1",
        project_id="p1",
        project_name="剧集",
        episode_path="C:/videos/01.mp4",
        language_code="_source",
        language_name="",
        stage="source",
        task=None,
    )
    center.add_request(request)
    app.processEvents()
    assert center.isVisible() is False
