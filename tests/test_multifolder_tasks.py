import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QWidget

from videotrans.component.checkable_combo import CheckableComboBox
import videotrans.component.multifolder_tasks as multifolder_tasks
from videotrans.component.multifolder_tasks import (
    LanguageSpec,
    MultiFolderScheduler,
    MultiFolderTaskWindow,
    ProjectSpec,
    ReviewCenter,
    ReviewRequest,
    _discover_videos,
)
from videotrans.configure.base import BaseCon


def test_target_language_combo_supports_multiple_checked_languages():
    app = QApplication.instance() or QApplication([])
    combo = CheckableComboBox()
    combo.addItems(["-", "English", "French", "German"])

    combo.setCheckedTexts(["English", "French", "German"])

    assert combo.checkedTexts() == ["English", "French", "German"]
    assert combo.currentText() == "English"
    assert combo.displayText() == "English、French +1"
    combo.setCurrentText("French")
    assert combo.checkedTexts() == ["French"]
    assert combo.displayText() == "French"
    app.processEvents()


def test_default_voice_enables_dubbing_when_channel_has_a_real_voice():
    assert multifolder_tasks.tools.default_voice_role(
        ["No", "Auto Voice", "Second Voice"]
    ) == "Auto Voice"
    assert multifolder_tasks.tools.default_voice_role(
        ["No", "Auto Voice", "Second Voice"], preferred="Second Voice"
    ) == "Second Voice"
    assert multifolder_tasks.tools.default_voice_role(["No", "", "-"]) == "No"


def test_imported_multilanguage_project_is_expanded_and_visible(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(multifolder_tasks, "STATE_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(
        multifolder_tasks.tools,
        "role_menu",
        lambda *_args: ["No", "Auto Voice"],
    )

    main = QWidget()
    main.review_countdown = QCheckBox()
    main.review_countdown.setChecked(True)
    main.tts_type = QComboBox()
    main.tts_type.addItem("Azure-TTS")
    main.voice_role = QComboBox()
    main.voice_role.addItem("No")
    main.target_dir = ""
    video = tmp_path / "剧集" / "01.mp4"
    video.parent.mkdir()
    video.write_bytes(b"video")

    window = MultiFolderTaskWindow(main)
    assert window.add_videos(
        [video.as_posix()],
        target_language="英语",
        target_languages=["英语", "法语"],
        manual_review=True,
        replace=True,
    ) is True

    parent = window.tree.topLevelItem(0)
    assert parent.isExpanded() is True
    assert [parent.child(index).text(0) for index in range(parent.childCount())] == [
        "原文识别",
        "英语",
        "法语",
    ]
    assert [language.voice for language in window.projects[0].languages] == [
        "Auto Voice",
        "Auto Voice",
    ]
    assert window.summary.text() == (
        "已加入 1 个视频；目标语言：英语、法语。"
        "即将自动开始处理。"
    )
    window.close()
    main.close()
    app.processEvents()


def test_each_folder_gets_its_own_subtitle_area_selection(tmp_path, monkeypatch):
    from videotrans.component import subtitle_removal

    projects = []
    for index, name in enumerate(("第一部剧", "第二部剧"), start=1):
        folder = tmp_path / name
        folder.mkdir()
        video = folder / "01.mp4"
        video.write_bytes(b"video")
        projects.append(ProjectSpec(f"p{index}", folder.as_posix(), [video.as_posix()]))

    selections = []

    def select_area(**kwargs):
        selections.append(kwargs)
        return {
            "skip_ocr": False,
            "normalized_rect": [0.1, 0.7, 0.8, 0.1],
            "reference_aspect_ratio": 9 / 16,
        }

    monkeypatch.setattr(subtitle_removal, "select_batch_subtitle_area", select_area)
    monkeypatch.setattr(multifolder_tasks.settings, "save", lambda: None)
    monkeypatch.setitem(multifolder_tasks.settings, "subtitle_removal_last_rect", "")
    monkeypatch.setitem(multifolder_tasks.settings, "subtitle_removal_last_aspect_ratio", 0.0)
    fake_window = SimpleNamespace(
        main=None,
        _save_projects=lambda: None,
    )

    assert MultiFolderTaskWindow._prepare_subtitle_removal(
        fake_window,
        projects,
        {
            "remove_burned_subtitles": False,
            "subtitle_removal_provider": "local",
            "burned_subtitle_ocr": True,
            "recogn_type": multifolder_tasks.recognition.FASTER_WHISPER,
            "source_language_code": "zh-cn",
        },
    ) is True

    assert [call["title"] for call in selections] == [
        "框选字幕区域（1/2）· 第一部剧",
        "框选字幕区域（2/2）· 第二部剧",
    ]
    assert all(project.burned_subtitle_ocr for project in projects)
    assert all(project.subtitle_removal_rect == [0.1, 0.7, 0.8, 0.1] for project in projects)


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


def test_skipping_language_releases_only_current_stage_for_all_episodes():
    scheduler = MultiFolderScheduler([], {})
    project = ProjectSpec("p1", "C:/series", [])
    language = LanguageSpec("English", "en")
    other_language = LanguageSpec("French", "fr")
    requests = [
        ReviewRequest("r1", "p1", "series", "01.mp4", "en", "English", "target", None),
        ReviewRequest("r2", "p1", "series", "02.mp4", "en", "English", "target", None),
        ReviewRequest("r3", "p1", "series", "01.mp4", "fr", "French", "target", None),
    ]
    scheduler._pending = {request.request_id: request for request in requests}

    scheduler.skip_language_stage_reviews("p1", "en", "target")

    assert set(scheduler._pending) == {"r3"}
    assert scheduler._request_review(
        project, None, "target", "03.mp4", language
    ) is False
    assert scheduler._request_review(
        project, None, "dubbing", "03.mp4", language
    ) is True
    assert scheduler._request_review(
        project, None, "target", "03.mp4", other_language
    ) is True


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
    center = ReviewCenter(
        parent,
        lambda _request_id: None,
        lambda _project_id, _language_code, _stage: None,
    )
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
