"""Multi-folder, multi-language task center with passive manual review gates."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import threading
import traceback
import uuid as uuid_lib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTreeView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from pydub import AudioSegment

from videotrans import recognition, translator, tts
from videotrans.configure import contants
from videotrans.configure.config import ROOT_DIR, app_cfg, logger, settings
from videotrans.task.taskcfg import TaskCfgVTT
from videotrans.task.trans_create import TransCreate
from videotrans.util import tools
from videotrans.util.subtitle_import import normalized_path_key


STATE_FILE = Path(ROOT_DIR, "videotrans", "multifolder_tasks.json")


@dataclass
class LanguageSpec:
    name: str
    code: str
    voice: str = "No"


@dataclass
class ProjectSpec:
    project_id: str
    folder: str
    videos: List[str]
    manual_review: bool = True
    languages: List[LanguageSpec] = field(default_factory=list)
    remove_burned_subtitles: bool = False
    subtitle_removal_rect: Optional[list] = None
    subtitle_removal_aspect_ratio: float = 0.0
    subtitle_files: Dict[str, str] = field(default_factory=dict)
    output_dir: str = ""


@dataclass
class ReviewRequest:
    request_id: str
    project_id: str
    project_name: str
    episode_path: str
    language_code: str
    language_name: str
    stage: str
    task: TransCreate

    @property
    def title(self) -> str:
        stage_names = {
            "source": "原文字幕",
            "target": "译文 / 角色",
            "dubbing": "配音",
            "recogn2": "二次识别",
        }
        language = f" · {self.language_name}" if self.language_name else ""
        return f"{Path(self.episode_path).name}{language} · {stage_names[self.stage]}"


def _atomic_write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _discover_videos(folder: str) -> List[str]:
    supported = set(contants.VIDEO_EXTS) | set(contants.AUDIO_EXITS)
    result = [
        path.resolve().as_posix()
        for path in Path(folder).rglob("*")
        if path.is_file() and path.suffix.lower().lstrip(".") in supported
        and not any(part.casefold() == "_video_out" for part in path.parts)
    ]
    return sorted(result, key=lambda value: value.casefold())


def _subtitle_for_video(project: ProjectSpec, video: str) -> Optional[str]:
    imported = project.subtitle_files.get(normalized_path_key(video))
    if imported and tools.vail_file(imported):
        return Path(imported).resolve().as_posix()
    video_path = Path(video)
    candidates = [
        video_path.with_suffix(".srt"),
        Path(project.folder, "字幕", f"{video_path.stem}.srt"),
        Path(project.folder, "subtitles", f"{video_path.stem}.srt"),
    ]
    for candidate in candidates:
        if tools.vail_file(candidate):
            return candidate.resolve().as_posix()
    return None


def _task_uuid(video: str, suffix: str) -> str:
    path = Path(video)
    stat = path.stat()
    raw = f"{path.resolve().as_posix()}-{stat.st_size}-{stat.st_mtime_ns}-{suffix}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


class MultiFolderScheduler(QThread):
    status_changed = Signal(str, str, str)
    review_ready = Signal(object)
    run_finished = Signal(bool, str)

    def __init__(self, projects: List[ProjectSpec], base_cfg: dict, parent=None):
        super().__init__(parent)
        self.projects = copy.deepcopy(projects)
        self.base_cfg = copy.deepcopy(base_cfg)
        self._condition = threading.Condition()
        self._pending: Dict[str, ReviewRequest] = {}
        self._cancelled = False
        self._active_uuids = set()

    def approve(self, request_id: str) -> None:
        with self._condition:
            self._pending.pop(request_id, None)
            self._condition.notify_all()

    def cancel(self) -> None:
        with self._condition:
            self._cancelled = True
            app_cfg.current_status = "stop"
            app_cfg.stoped_uuid_set.update(self._active_uuids)
            self._pending.clear()
            self._condition.notify_all()

    def _emit_status(self, project_id: str, key: str, text: str) -> None:
        self.status_changed.emit(project_id, key, text)

    def _task_signal(self, project_id: str, key: str, message: dict) -> None:
        if message.get("type") == "logs" and message.get("text"):
            text = str(message["text"]).splitlines()[0][:80]
            self._emit_status(project_id, key, text)

    def _request_review(
            self, project: ProjectSpec, task: TransCreate, stage: str,
            episode_path: str, language: Optional[LanguageSpec] = None,
    ) -> None:
        request = ReviewRequest(
            request_id=uuid_lib.uuid4().hex,
            project_id=project.project_id,
            project_name=Path(project.folder).name,
            episode_path=episode_path,
            language_code=language.code if language else "_source",
            language_name=language.name if language else "",
            stage=stage,
            task=task,
        )
        with self._condition:
            self._pending[request.request_id] = request
        self.review_ready.emit(request)

    def _wait_for_reviews(self) -> None:
        with self._condition:
            while self._pending and not self._cancelled:
                self._condition.wait()
        if self._cancelled:
            raise InterruptedError("任务已停止")

    def _source_task(self, project: ProjectSpec, video: str, work_root: Path) -> TransCreate:
        episode_key = _task_uuid(video, "source")
        app_cfg.rm_uuid(episode_key)
        self._active_uuids.add(episode_key)
        output_root = work_root / "shared-output"
        input_file = tools.format_video(video, output_root.as_posix())
        input_file.uuid = episode_key
        cfg = copy.deepcopy(self.base_cfg)
        cfg.update({
            "uuid": episode_key,
            "target_language": "-",
            "target_language_code": "",
            "voice_role": "No",
            "subtitle_type": 0,
            "only_out_mp4": False,
            "cache_folder": (work_root / "shared-cache" / episode_key).as_posix(),
            "series_video_paths": project.videos,
            "remove_burned_subtitles": project.remove_burned_subtitles,
            "subtitle_removal_rect": project.subtitle_removal_rect,
            "subtitle_removal_aspect_ratio": project.subtitle_removal_aspect_ratio,
        })
        subtitle = _subtitle_for_video(project, video)
        cfg["subtitle_files"] = (
            {normalized_path_key(video): subtitle} if subtitle else {}
        )
        task = TransCreate(
            cfg=TaskCfgVTT(**(cfg | asdict(input_file))),
            signal_handler=lambda msg: self._task_signal(
                project.project_id, "_source", msg
            ),
        )
        try:
            task.prepare()
            task.recogn()
            task.diariz()
        except Exception:
            self._end_failed_task(task)
            raise
        return task

    def _language_task(
            self, project: ProjectSpec, language: LanguageSpec, video: str,
            source_task: TransCreate, output_root: Path, work_root: Path,
    ) -> TransCreate:
        episode_key = _task_uuid(video, language.code)
        app_cfg.rm_uuid(episode_key)
        self._active_uuids.add(episode_key)
        language_root = output_root / language.code
        input_file = tools.format_video(video, language_root.as_posix())
        input_file.uuid = episode_key
        source_text = Path(source_task.cfg.source_sub).read_text(
            encoding="utf-8", errors="ignore"
        )
        shared_novoice = None
        if not self.base_cfg.get("video_autorate"):
            tools.is_novoice_mp4(source_task.cfg.novoice_mp4, source_task.uuid)
            if tools.vail_file(source_task.cfg.novoice_mp4):
                shared_novoice = source_task.cfg.novoice_mp4
        cfg = copy.deepcopy(self.base_cfg)
        cfg.update({
            "uuid": episode_key,
            "target_language": language.name,
            "target_language_code": language.code,
            "voice_role": language.voice,
            "subtitles": source_text,
            "subtitle_files": {},
            "cache_folder": (work_root / "language-cache" / language.code / episode_key).as_posix(),
            "series_video_paths": project.videos,
            "shared_visual_source": source_task.visual_source,
            "shared_ocr_source": source_task.ocr_source,
            "shared_source_wav": source_task.cfg.source_wav,
            "shared_vocal": source_task.cfg.vocal,
            "shared_instrument": source_task.cfg.instrument,
            "shared_speaker_file": Path(
                source_task.cfg.cache_folder, "speaker.json"
            ).as_posix(),
            "shared_novoice": shared_novoice,
            # 共享阶段已经完成字幕消除，语言任务不得重复调用。
            "remove_burned_subtitles": False,
        })
        task = TransCreate(
            cfg=TaskCfgVTT(**(cfg | asdict(input_file))),
            signal_handler=lambda msg: self._task_signal(
                project.project_id, language.code, msg
            ),
        )
        try:
            task.prepare()
            task.recogn()
            task.trans()
            if task.should_dubbing:
                task._prepare_line_roles(tools.get_subtitle_from_srt(task.cfg.source_sub))
        except Exception:
            self._end_failed_task(task)
            raise
        return task

    @staticmethod
    def _end_failed_task(task: Optional[TransCreate]) -> None:
        if not task:
            return
        task.hasend = True
        if task.uuid:
            app_cfg.stoped_uuid_set.add(task.uuid)

    def _completion_identity(
            self, project: ProjectSpec, video: str, language: LanguageSpec,
    ) -> str:
        stat = Path(video).stat()
        config = {
            key: value for key, value in self.base_cfg.items()
            if key != "clear_cache"
        }
        raw = json.dumps({
            "source": Path(video).resolve().as_posix(),
            "source_size": stat.st_size,
            "source_mtime_ns": stat.st_mtime_ns,
            "language": asdict(language),
            "config": config,
            "subtitle_file": project.subtitle_files.get(normalized_path_key(video)),
            "remove_burned_subtitles": project.remove_burned_subtitles,
            "subtitle_removal_rect": project.subtitle_removal_rect,
        }, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _completion_marker(
            self, project: ProjectSpec, video: str, language: LanguageSpec,
    ) -> Path:
        output_dir = (
            self._output_root(project) / language.code
            / f"{Path(video).stem}-{Path(video).suffix[1:].lower()}"
        )
        return output_dir / ".pyvideotrans-complete.json"

    def _is_completed(
            self, project: ProjectSpec, video: str, language: LanguageSpec,
    ) -> bool:
        if self.base_cfg.get("clear_cache"):
            return False
        marker = self._completion_marker(project, video, language)
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        if data.get("identity") != self._completion_identity(project, video, language):
            return False
        output_dir = marker.parent
        needs_video = (
            language.voice not in ("", "-", "No")
            or int(self.base_cfg.get("subtitle_type") or 0) > 0
        )
        expected = (
            output_dir / f"{Path(video).stem}.mp4"
            if needs_video else output_dir / f"{language.code}.srt"
        )
        return tools.vail_file(expected)

    def _mark_completed(
            self, project: ProjectSpec, video: str, language: LanguageSpec,
    ) -> None:
        marker = self._completion_marker(project, video, language)
        _atomic_write_json(marker, {
            "identity": self._completion_identity(project, video, language),
            "source": Path(video).resolve().as_posix(),
            "language": language.code,
        })

    def run(self) -> None:
        source_tasks: Dict[tuple, TransCreate] = {}
        language_tasks: Dict[tuple, TransCreate] = {}
        failures = 0
        try:
            # Wave 1: all language-independent work. Every episode becomes
            # reviewable before the pipeline crosses the source-subtitle gate.
            for project in self.projects:
                work_root = self._work_root(project)
                for video in project.videos:
                    if self._cancelled:
                        raise InterruptedError("任务已停止")
                    try:
                        self._emit_status(project.project_id, "_source", f"识别中 · {Path(video).name}")
                        task = self._source_task(project, video, work_root)
                        source_tasks[(project.project_id, video)] = task
                        if project.manual_review:
                            self._request_review(project, task, "source", video)
                            self._emit_status(project.project_id, "_source", "待人工校对")
                    except Exception as error:
                        failures += 1
                        self._emit_status(project.project_id, "_source", f"失败：{error}")
                        logger.exception("多文件夹原文阶段失败", exc_info=True)
            self._wait_for_reviews()

            # Wave 2: episode-major translation. This deliberately finishes
            # every language of an episode before moving to the next episode.
            for project in self.projects:
                output_root = self._output_root(project)
                work_root = self._work_root(project)
                for video in project.videos:
                    source_task = source_tasks.get((project.project_id, video))
                    if not source_task:
                        continue
                    for language in project.languages:
                        if self._cancelled:
                            raise InterruptedError("任务已停止")
                        if self._is_completed(project, video, language):
                            self._emit_status(project.project_id, language.code, "已完成（复用成品）")
                            continue
                        try:
                            self._emit_status(project.project_id, language.code, f"翻译中 · {Path(video).name}")
                            task = self._language_task(
                                project, language, video, source_task,
                                output_root, work_root,
                            )
                            language_tasks[(project.project_id, video, language.code)] = task
                            if project.manual_review:
                                self._request_review(project, task, "target", video, language)
                                self._emit_status(project.project_id, language.code, "待字幕 / 角色校对")
                        except Exception as error:
                            failures += 1
                            self._emit_status(project.project_id, language.code, f"失败：{error}")
                            logger.exception("多文件夹翻译阶段失败", exc_info=True)
            self._wait_for_reviews()

            # Wave 3: synthesize every approved language/episode pair.
            for project in self.projects:
                for video in project.videos:
                    for language in project.languages:
                        task = language_tasks.get((project.project_id, video, language.code))
                        if not task or not task.should_dubbing:
                            continue
                        try:
                            self._emit_status(project.project_id, language.code, f"配音中 · {Path(video).name}")
                            task.dubbing()
                            for item in task.queue_tts:
                                item["dubbing_s"] = (
                                    len(AudioSegment.from_file(item["filename"])) / 1000.0
                                    if tools.vail_file(item.get("filename")) else 0.0
                                )
                            Path(task.cfg.cache_folder, "queue_tts.json").write_text(
                                json.dumps(task.queue_tts, ensure_ascii=False), encoding="utf-8"
                            )
                            if project.manual_review and not task.ignore_align:
                                self._request_review(project, task, "dubbing", video, language)
                                self._emit_status(project.project_id, language.code, "待配音校对")
                        except Exception as error:
                            failures += 1
                            self._end_failed_task(task)
                            language_tasks.pop((project.project_id, video, language.code), None)
                            self._emit_status(project.project_id, language.code, f"失败：{error}")
                            logger.exception("多文件夹配音阶段失败", exc_info=True)
            self._wait_for_reviews()

            # Wave 4: alignment, optional second recognition, then final output.
            for project in self.projects:
                for video in project.videos:
                    for language in project.languages:
                        task = language_tasks.get((project.project_id, video, language.code))
                        if not task:
                            continue
                        try:
                            self._emit_status(project.project_id, language.code, f"对齐中 · {Path(video).name}")
                            task.align()
                            task.recogn2pass()
                            if project.manual_review and task.should_recogn2:
                                self._request_review(project, task, "recogn2", video, language)
                                self._emit_status(project.project_id, language.code, "待二次识别校对")
                        except Exception as error:
                            failures += 1
                            self._end_failed_task(task)
                            language_tasks.pop((project.project_id, video, language.code), None)
                            self._emit_status(project.project_id, language.code, f"失败：{error}")
                            logger.exception("多文件夹对齐阶段失败", exc_info=True)
            self._wait_for_reviews()

            for project in self.projects:
                for video in project.videos:
                    for language in project.languages:
                        task = language_tasks.get((project.project_id, video, language.code))
                        if not task:
                            continue
                        try:
                            self._emit_status(project.project_id, language.code, f"合成中 · {Path(video).name}")
                            task.assembling()
                            task.task_done()
                            self._mark_completed(project, video, language)
                            self._emit_status(project.project_id, language.code, "处理完成 ✓")
                        except Exception as error:
                            failures += 1
                            self._end_failed_task(task)
                            self._emit_status(project.project_id, language.code, f"失败：{error}")
                            logger.exception("多文件夹合成阶段失败", exc_info=True)

            message = "全部任务完成" if failures == 0 else f"执行结束，失败 {failures} 项，可点击重试未完成"
            self.run_finished.emit(failures == 0, message)
        except InterruptedError as error:
            self.run_finished.emit(False, str(error))
        except Exception as error:
            logger.exception("多文件夹任务调度器异常", exc_info=True)
            self.run_finished.emit(False, f"任务异常：{error}\n{traceback.format_exc()}")
        finally:
            for task in source_tasks.values():
                task.hasend = True
            for task in language_tasks.values():
                task.hasend = True
            if app_cfg.current_status in ("ing", "stop"):
                app_cfg.current_status = "end"

    @staticmethod
    def _output_root(project: ProjectSpec) -> Path:
        folder = Path(project.folder)
        if project.output_dir:
            return Path(project.output_dir) / folder.name
        return folder.parent / "_video_out" / folder.name

    @staticmethod
    def _work_root(project: ProjectSpec) -> Path:
        return MultiFolderScheduler._output_root(project) / ".pyvideotrans-work"


class ReviewCenter(QDialog):
    def __init__(self, parent, approve_callback):
        super().__init__(parent)
        self.main = parent
        self.approve_callback = approve_callback
        self.requests: Dict[str, ReviewRequest] = {}
        self.current_request_id: Optional[str] = None
        self.current_editor = None
        self.project_filter: Optional[str] = None
        self.language_filter: Optional[str] = None
        self.setWindowTitle("人工校对中心")
        self.resize(1250, 800)

        layout = QVBoxLayout(self)
        self.info = QLabel("选择左侧待校对视频。保存并继续即代表通过；没有倒计时。")
        layout.addWidget(self.info)
        splitter = QSplitter()
        self.list_widget = QListWidget()
        self.list_widget.setMinimumWidth(300)
        self.list_widget.currentItemChanged.connect(self._selection_changed)
        splitter.addWidget(self.list_widget)
        self.host = QWidget()
        self.host_layout = QVBoxLayout(self.host)
        self.placeholder = QLabel("当前筛选下没有待校对项目")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.host_layout.addWidget(self.placeholder)
        splitter.addWidget(self.host)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)

    def add_request(self, request: ReviewRequest) -> None:
        self.requests[request.request_id] = request
        self._refresh_list()

    def remove_request(self, request_id: str) -> None:
        self.requests.pop(request_id, None)
        if self.current_request_id == request_id:
            self._clear_editor()
        self._refresh_list()

    def open_for(self, project_id: str, language_code: Optional[str]) -> None:
        self.project_filter = project_id
        self.language_filter = language_code
        self._refresh_list()
        self.show()
        self.raise_()
        self.activateWindow()
        if self.list_widget.count() and not self.list_widget.currentItem():
            self.list_widget.setCurrentRow(0)

    def _filtered_requests(self) -> List[ReviewRequest]:
        result = [
            request for request in self.requests.values()
            if request.project_id == self.project_filter
            and (
                self.language_filter in (None, "*")
                or request.language_code == self.language_filter
            )
        ]
        return sorted(result, key=lambda request: (
            Path(request.episode_path).name.casefold(), request.language_code, request.stage
        ))

    def _refresh_list(self) -> None:
        selected_id = self.current_request_id
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        selected_row = -1
        for row, request in enumerate(self._filtered_requests()):
            item = QListWidgetItem(request.title)
            item.setData(Qt.UserRole, request.request_id)
            self.list_widget.addItem(item)
            if request.request_id == selected_id:
                selected_row = row
        self.list_widget.blockSignals(False)
        if selected_row >= 0:
            self.list_widget.setCurrentRow(selected_row)
        self.placeholder.setVisible(self.list_widget.count() == 0 and self.current_editor is None)

    def _selection_changed(self, current, previous) -> None:
        if not current:
            return
        request_id = current.data(Qt.UserRole)
        if request_id == self.current_request_id:
            return
        if self.current_editor is not None:
            reply = QMessageBox.question(
                self,
                "切换校对视频",
                "当前视频尚未点击通过，未保存的界面修改可能丢失。是否切换？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                self._refresh_list()
                return
        self._load_editor(self.requests[request_id])

    def _clear_editor(self) -> None:
        if self.current_editor is not None:
            self.host_layout.removeWidget(self.current_editor)
            self.current_editor.setParent(None)
            self.current_editor.deleteLater()
        self.current_editor = None
        self.current_request_id = None
        self.placeholder.show()

    def _load_editor(self, request: ReviewRequest) -> None:
        self._clear_editor()
        task = request.task
        if request.stage == "source":
            from videotrans.component.onlyone_set_recogn import EditRecognResultDialog
            editor = EditRecognResultDialog(
                self.main, task.cfg.source_sub, countdown_enabled=False
            )
        elif request.stage == "target":
            if task.should_dubbing:
                from videotrans.component.onlyone_set_role import SpeakerAssignmentDialog
                editor = SpeakerAssignmentDialog(
                    parent=self.main,
                    target_sub=task.cfg.target_sub,
                    all_voices=tools.role_menu(task.cfg.tts_type, task.cfg.target_language_code),
                    source_sub=task.cfg.source_sub,
                    source_audio=task.cfg.source_wav,
                    cache_folder=task.cfg.cache_folder,
                    target_language=task.cfg.target_language_code,
                    source_language=task.cfg.source_language_code,
                    tts_type=task.cfg.tts_type,
                    default_role=task.cfg.voice_role,
                    video_path=task.cfg.name,
                    series_folder=task.cfg.dirname,
                    series_video_paths=task.cfg.series_video_paths,
                    series_output_dir=Path(task.cfg.target_dir).parent.as_posix(),
                    countdown_enabled=False,
                )
            else:
                from videotrans.component.onlyone_set_recogn import EditRecognResultDialog
                editor = EditRecognResultDialog(
                    self.main, task.cfg.target_sub, countdown_enabled=False
                )
        elif request.stage == "dubbing":
            from videotrans.component.onlyone_set_editdubb import EditDubbingResultDialog
            editor = EditDubbingResultDialog(
                self.main, task.cfg.target_language_code,
                task.cfg.cache_folder, countdown_enabled=False,
            )
        else:
            from videotrans.component.onlyone_set_recogn2 import EditRecognResultDialog2
            editor = EditRecognResultDialog2(
                self.main, task.cfg.source_sub, countdown_enabled=False
            )
        self.current_request_id = request.request_id
        self.current_editor = editor
        for button in editor.findChildren(QPushButton):
            if "终止" in button.text() or button.text() == "Terminate this mission":
                button.setText("关闭校对（任务继续等待）")
        editor.accepted.connect(lambda rid=request.request_id: self._approved(rid))
        editor.rejected.connect(self._editor_closed_without_approval)
        editor.setParent(self.host)
        editor.setWindowFlags(Qt.Widget)
        self.host_layout.addWidget(editor)
        self.placeholder.hide()
        editor.show()

    def _approved(self, request_id: str) -> None:
        self.approve_callback(request_id)

    def _editor_closed_without_approval(self) -> None:
        self._clear_editor()
        self._refresh_list()

    def closeEvent(self, event) -> None:
        event.ignore()
        self.hide()


class LanguagePicker(QDialog):
    def __init__(self, parent, names: List[str], selected_codes: set):
        super().__init__(parent)
        self.setWindowTitle("选择目标语言")
        self.resize(420, 600)
        layout = QVBoxLayout(self)
        self.list_widget = QListWidget()
        for name in names:
            code = translator.get_code(show_text=name)
            if not code:
                continue
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, code)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if code in selected_codes else Qt.Unchecked)
            self.list_widget.addItem(item)
        layout.addWidget(self.list_widget)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("取消")
        confirm = QPushButton("确定")
        cancel.clicked.connect(self.reject)
        confirm.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(confirm)
        layout.addLayout(buttons)

    def selected(self) -> List[tuple]:
        result = []
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            if item.checkState() == Qt.Checked:
                result.append((item.text(), item.data(Qt.UserRole)))
        return result


class MultiFolderTaskWindow(QDialog):
    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.projects: List[ProjectSpec] = self._load_projects()
        self.scheduler: Optional[MultiFolderScheduler] = None
        self.review_counts: Dict[tuple, int] = {}
        self.items: Dict[tuple, QTreeWidgetItem] = {}
        self.review_center = ReviewCenter(main, self._approve_request)

        self.setWindowTitle("多文件夹 · 多语言任务中心")
        self.resize(1150, 720)
        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        add_folders = QPushButton("+ 添加文件夹")
        add_folders.clicked.connect(self._add_folders)
        self.start_button = QPushButton("开始处理")
        self.start_button.clicked.connect(self._start)
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        self.summary = QLabel("任务按处理阶段推进；校对开启时不会自动弹窗，也不会自动通过。")
        toolbar.addWidget(add_folders)
        toolbar.addWidget(self.start_button)
        toolbar.addWidget(self.stop_button)
        toolbar.addStretch()
        toolbar.addWidget(self.summary)
        layout.addLayout(toolbar)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["文件夹 / 语言", "视频", "配音音色", "人工校对", "状态", "操作"])
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().resizeSection(0, 320)
        self.tree.header().resizeSection(1, 90)
        self.tree.header().resizeSection(2, 250)
        self.tree.header().resizeSection(3, 100)
        self.tree.header().resizeSection(4, 220)
        self.tree.header().resizeSection(5, 150)
        layout.addWidget(self.tree)
        self._render()

    def _load_projects(self) -> List[ProjectSpec]:
        if not STATE_FILE.is_file():
            return []
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            projects = []
            for item in data:
                item["languages"] = [LanguageSpec(**lang) for lang in item.get("languages", [])]
                projects.append(ProjectSpec(**item))
            return projects
        except Exception:
            logger.exception("读取多文件夹任务配置失败", exc_info=True)
            return []

    def _save_projects(self) -> None:
        _atomic_write_json(STATE_FILE, [asdict(project) for project in self.projects])

    def _render(self) -> None:
        self.tree.clear()
        self.items.clear()
        for project in self.projects:
            parent = QTreeWidgetItem([
                Path(project.folder).name,
                f"{len(project.videos)} 集",
                "",
                "",
                "待开始",
                "",
            ])
            parent.setToolTip(
                0,
                f"输入：{project.folder}\n输出：{MultiFolderScheduler._output_root(project)}",
            )
            parent.setExpanded(True)
            self.tree.addTopLevelItem(parent)
            review_switch = QCheckBox("开启")
            review_switch.setChecked(project.manual_review)
            review_switch.toggled.connect(
                lambda checked, pid=project.project_id: self._toggle_review(pid, checked)
            )
            self.tree.setItemWidget(parent, 3, review_switch)
            action_widget = QWidget()
            action_layout = QHBoxLayout(action_widget)
            action_layout.setContentsMargins(0, 0, 0, 0)
            add_language = QPushButton("+ 语言")
            add_language.clicked.connect(
                lambda _=False, pid=project.project_id: self._choose_languages(pid)
            )
            remove_project = QPushButton("删除")
            remove_project.clicked.connect(
                lambda _=False, pid=project.project_id: self._remove_project(pid)
            )
            action_layout.addWidget(add_language)
            action_layout.addWidget(remove_project)
            self.tree.setItemWidget(parent, 5, action_widget)

            source = QTreeWidgetItem(["原文识别", f"{len(project.videos)} 集", "-", "共享", "待开始", ""])
            parent.addChild(source)
            self.items[(project.project_id, "_source")] = source
            self._set_review_button(project, source, "_source")

            for language in project.languages:
                child = QTreeWidgetItem([language.name, f"{len(project.videos)} 集", "", "逐集", "待开始", ""])
                parent.addChild(child)
                self.items[(project.project_id, language.code)] = child
                voice = QComboBox()
                roles = tools.role_menu(self.main.tts_type.currentIndex(), language.code)
                if "No" not in roles:
                    roles.insert(0, "No")
                voice.addItems(roles)
                voice.setCurrentText(language.voice if language.voice in roles else "No")
                language.voice = voice.currentText()
                voice.currentTextChanged.connect(
                    lambda value, pid=project.project_id, code=language.code: self._set_voice(pid, code, value)
                )
                self.tree.setItemWidget(child, 2, voice)
                self._set_review_button(project, child, language.code)

    def _set_review_button(self, project, item, language_code) -> None:
        count = self.review_counts.get((project.project_id, language_code), 0)
        button = QPushButton(f"校对{f' ({count})' if count else ''}")
        button.setEnabled(count > 0)
        button.clicked.connect(
            lambda _=False, pid=project.project_id, code=language_code:
            self.review_center.open_for(pid, code)
        )
        self.tree.setItemWidget(item, 5, button)

    def _project(self, project_id: str) -> ProjectSpec:
        return next(project for project in self.projects if project.project_id == project_id)

    def _toggle_review(self, project_id: str, checked: bool) -> None:
        project = self._project(project_id)
        if self.scheduler and self.scheduler.isRunning():
            widget = self.sender()
            if isinstance(widget, QCheckBox):
                widget.blockSignals(True)
                widget.setChecked(project.manual_review)
                widget.blockSignals(False)
            QMessageBox.information(self, "任务运行中", "人工校对开关只能在开始前修改。")
            return
        project.manual_review = checked
        self._save_projects()

    def _set_voice(self, project_id: str, code: str, voice: str) -> None:
        language = next(lang for lang in self._project(project_id).languages if lang.code == code)
        if self.scheduler and self.scheduler.isRunning():
            widget = self.sender()
            if isinstance(widget, QComboBox):
                widget.blockSignals(True)
                widget.setCurrentText(language.voice)
                widget.blockSignals(False)
            return
        language.voice = voice
        self._save_projects()

    def _add_folders(self) -> None:
        if self.scheduler and self.scheduler.isRunning():
            QMessageBox.information(self, "任务运行中", "请等待完成或停止后再添加文件夹。")
            return
        dialog = QFileDialog(self, "选择一个或多个文件夹")
        dialog.setFileMode(QFileDialog.Directory)
        dialog.setOption(QFileDialog.DontUseNativeDialog, True)
        dialog.setOption(QFileDialog.ShowDirsOnly, True)
        for view in dialog.findChildren(QListView) + dialog.findChildren(QTreeView):
            view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        if dialog.exec() != QDialog.Accepted:
            return
        existing = {Path(project.folder).resolve() for project in self.projects}
        added = 0
        for folder in dialog.selectedFiles():
            resolved = Path(folder).resolve()
            if resolved in existing:
                continue
            videos = _discover_videos(resolved.as_posix())
            if not videos:
                continue
            self.projects.append(ProjectSpec(
                project_id=uuid_lib.uuid4().hex[:12],
                folder=resolved.as_posix(),
                videos=videos,
                manual_review=self.main.review_countdown.isChecked(),
                output_dir=self.main.target_dir or "",
            ))
            existing.add(resolved)
            added += 1
        self._save_projects()
        self._render()
        if not added:
            QMessageBox.information(self, "没有新增", "所选文件夹重复，或没有支持的音视频文件。")

    def add_videos(
            self, videos: List[str], target_language: str = "",
            subtitle_files: Optional[Dict[str, str]] = None,
    ) -> None:
        """Import the existing main-window selection without starting it."""
        if self.scheduler and self.scheduler.isRunning():
            QMessageBox.information(self, "任务运行中", "当前选择未加入，请等待完成或停止后重试。")
            return
        grouped: Dict[str, List[str]] = {}
        for value in videos:
            path = Path(value).resolve()
            grouped.setdefault(path.parent.as_posix(), []).append(path.as_posix())
        target_code = translator.get_code(show_text=target_language)
        for folder, grouped_videos in grouped.items():
            resolved_videos = sorted(set(grouped_videos), key=str.casefold)
            project = next(
                (item for item in self.projects if Path(item.folder).resolve().as_posix() == folder),
                None,
            )
            if project is None:
                project = ProjectSpec(
                    project_id=uuid_lib.uuid4().hex[:12],
                    folder=folder,
                    videos=resolved_videos,
                    manual_review=True,
                    output_dir=self.main.target_dir or "",
                )
                self.projects.append(project)
            else:
                project.videos = sorted(
                    set(project.videos) | set(resolved_videos), key=str.casefold
                )
                project.manual_review = True
                if self.main.target_dir:
                    project.output_dir = self.main.target_dir
            for video in resolved_videos:
                key = normalized_path_key(video)
                subtitle = (subtitle_files or {}).get(key)
                if subtitle:
                    project.subtitle_files[key] = subtitle
            if target_code and target_code != "-" and not any(
                    language.code == target_code for language in project.languages
            ):
                project.languages.append(LanguageSpec(
                    name=target_language, code=target_code, voice="No"
                ))
        self._save_projects()
        self._render()

    def _choose_languages(self, project_id: str) -> None:
        if self.scheduler and self.scheduler.isRunning():
            QMessageBox.information(self, "任务运行中", "目标语言只能在开始前修改。")
            return
        project = self._project(project_id)
        picker = LanguagePicker(
            self,
            [name for name in self.main.languagename if name and name != "-"],
            {language.code for language in project.languages},
        )
        if picker.exec() != QDialog.Accepted:
            return
        existing = {language.code: language for language in project.languages}
        project.languages = [
            existing.get(code, LanguageSpec(name=name, code=code, voice="No"))
            for name, code in picker.selected()
        ]
        self._save_projects()
        self._render()

    def _remove_project(self, project_id: str) -> None:
        if self.scheduler and self.scheduler.isRunning():
            QMessageBox.information(self, "任务运行中", "请先停止当前任务，再删除文件夹。")
            return
        project = self._project(project_id)
        reply = QMessageBox.question(
            self,
            "删除任务",
            f"从列表移除“{Path(project.folder).name}”？不会删除任何视频或输出文件。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self.projects = [item for item in self.projects if item.project_id != project_id]
        self._save_projects()
        self._render()

    def _collect_base_cfg(self) -> dict:
        source_name = self.main.source_language.currentText()
        voice_rate = int(self.main.voice_rate.value())
        volume = int(self.main.volume_rate.value())
        pitch = int(self.main.pitch_rate.value())
        return {
            "translate_type": self.main.translate_type.currentIndex(),
            "source_language": source_name,
            "source_language_code": translator.get_code(show_text=source_name),
            "clear_cache": self.main.clear_cache.isChecked(),
            "only_out_mp4": False,
            "fix_punc": self.main.fix_punc.isChecked(),
            "remove_burned_subtitles": self.main.remove_burned_subtitles.isChecked(),
            "subtitle_removal_provider": self.main.subtitle_removal_provider.currentData() or "local",
            "tts_type": self.main.tts_type.currentIndex(),
            "volume": f"+{volume}%" if volume >= 0 else f"{volume}%",
            "pitch": f"+{pitch}Hz" if pitch >= 0 else f"{pitch}Hz",
            "recogn_type": self.main.recogn_type.currentIndex(),
            "model_name": self.main.model_name.currentText(),
            "remove_noise": self.main.remove_noise.isChecked(),
            "subtitle_type": self.main.subtitle_type.currentIndex(),
            "voice_rate": f"+{voice_rate}%" if voice_rate >= 0 else f"{voice_rate}%",
            "voice_autorate": self.main.voice_autorate.isChecked(),
            "video_autorate": self.main.video_autorate.isChecked(),
            "is_separate": self.main.is_separate.isChecked(),
            "embed_bgm": self.main.embed_bgm.isChecked(),
            "background_music": self.main.back_audio.text().strip(),
            "enable_diariz": self.main.enable_diariz.isChecked(),
            "recogn2pass": self.main.recogn2pass.isChecked(),
            "nums_diariz": self.main.nums_diariz.currentIndex(),
            "rephrase": self.main.rephrase.currentIndex(),
            "is_cuda": self.main.enable_cuda.isChecked(),
            "remove_silent_mid": self.main.remove_silent_mid.isChecked(),
            "align_sub_audio": self.main.align_sub_audio.isChecked(),
            "app_mode": "biaozhun",
            "output_srt": self.main.output_srt.currentIndex(),
            "burned_subtitle_ocr": bool(settings.get("burned_subtitle_ocr", True)),
            "subtitle_removal_rect": None,
            "subtitle_removal_aspect_ratio": 0.0,
            "loop_backaudio": self.main.is_loop_bgm.currentIndex(),
            "backaudio_volume": float(self.main.bgmvolume.text() or 0.8),
        }

    def _start(self) -> None:
        if self.scheduler and self.scheduler.isRunning():
            return
        if app_cfg.current_status == "ing":
            QMessageBox.warning(self, "已有任务运行", "请等待主窗口中的当前任务结束后再开始。")
            return
        runnable = [project for project in self.projects if project.videos and project.languages]
        if not runnable:
            QMessageBox.warning(self, "无法开始", "请先添加文件夹，并为文件夹至少选择一种目标语言。")
            return
        base_cfg = self._collect_base_cfg()
        if not base_cfg.get("source_language_code"):
            QMessageBox.warning(self, "无法开始", "请先在主窗口选择原始语言。")
            return
        if self.main.win_action.check_proxy() is not True:
            return
        recogn_allowed = recognition.is_allow_lang(
            langcode=base_cfg["source_language_code"],
            recogn_type=base_cfg["recogn_type"],
            model_name=base_cfg["model_name"],
        )
        if recogn_allowed is not True:
            QMessageBox.warning(self, "识别配置不可用", str(recogn_allowed))
            return
        if recognition.is_input_api(recogn_type=base_cfg["recogn_type"]) is not True:
            return
        if any(
                language.voice not in ("", "-", "No")
                for project in runnable for language in project.languages
        ) and tts.is_input_api(tts_type=base_cfg["tts_type"]) is not True:
            return
        if base_cfg["is_cuda"] and app_cfg.NVIDIA_GPU_NUMS == 0:
            QMessageBox.warning(self, "CUDA 不可用", "未检测到可用的 NVIDIA GPU，请关闭 CUDA 后重试。")
            return
        for project in runnable:
            for language in project.languages:
                if translator.is_allow_translate(
                        translate_type=base_cfg["translate_type"],
                        show_target=language.code,
                ) is not True:
                    QMessageBox.warning(self, "翻译渠道不支持", f"当前翻译渠道不支持 {language.name}。")
                    return
        if not self._prepare_subtitle_removal(runnable, base_cfg):
            return
        self.scheduler = MultiFolderScheduler(runnable, base_cfg, self)
        self.scheduler.status_changed.connect(self._status_changed)
        self.scheduler.review_ready.connect(self._review_ready)
        self.scheduler.run_finished.connect(self._run_finished)
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.summary.setText("运行中")
        app_cfg.current_status = "ing"
        self.scheduler.start()

    def _prepare_subtitle_removal(self, projects: List[ProjectSpec], base_cfg: dict) -> bool:
        enabled = bool(base_cfg.get("remove_burned_subtitles"))
        for project in projects:
            project.remove_burned_subtitles = False
        if not enabled:
            self._save_projects()
            return True

        provider = base_cfg.get("subtitle_removal_provider", "local")
        if provider != "local":
            reply = QMessageBox.question(
                self,
                "确认使用云端字幕消除",
                "多个文件夹会分别上传工作视频，可能产生费用。是否继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return False

        from videotrans.component.subtitle_removal import select_batch_subtitle_area
        for project in projects:
            videos = [
                path for path in project.videos
                if Path(path).suffix.lower().lstrip(".") in contants.VIDEO_EXTS
            ]
            if not videos:
                continue
            selection = select_batch_subtitle_area(
                input_file=videos[0],
                input_files=videos,
                initial_normalized_rect=project.subtitle_removal_rect,
                parent=self,
            )
            if selection is None:
                return False
            if selection.get("skip_ocr"):
                project.remove_burned_subtitles = False
                project.subtitle_removal_rect = None
                project.subtitle_removal_aspect_ratio = 0.0
            else:
                project.remove_burned_subtitles = True
                project.subtitle_removal_rect = selection["normalized_rect"]
                project.subtitle_removal_aspect_ratio = selection["reference_aspect_ratio"]
        self._save_projects()
        return True

    def _stop(self) -> None:
        if self.scheduler:
            self.scheduler.cancel()
        self.stop_button.setEnabled(False)

    def _status_changed(self, project_id: str, key: str, text: str) -> None:
        item = self.items.get((project_id, key))
        if item:
            item.setText(4, text)

    def _review_ready(self, request: ReviewRequest) -> None:
        key = (request.project_id, request.language_code)
        self.review_counts[key] = self.review_counts.get(key, 0) + 1
        self.review_center.add_request(request)
        project = self._project(request.project_id)
        item = self.items.get(key)
        if item:
            self._set_review_button(project, item, request.language_code)

    def _approve_request(self, request_id: str) -> None:
        request = self.review_center.requests.get(request_id)
        if not request or not self.scheduler:
            return
        key = (request.project_id, request.language_code)
        self.review_counts[key] = max(0, self.review_counts.get(key, 1) - 1)
        self.review_center.remove_request(request_id)
        project = self._project(request.project_id)
        item = self.items.get(key)
        if item:
            self._set_review_button(project, item, request.language_code)
            item.setText(4, "已通过，等待继续")
        self.scheduler.approve(request_id)

    def _run_finished(self, succeed: bool, message: str) -> None:
        if self.scheduler and self.scheduler._cancelled:
            for request_id in list(self.review_center.requests):
                self.review_center.remove_request(request_id)
            self.review_counts.clear()
            self._render()
        self.start_button.setText("开始处理" if succeed else "重试未完成")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.summary.setText(message.splitlines()[0])
        if not self.review_center.requests:
            QMessageBox.information(self, "任务结束", message.splitlines()[0])

    def closeEvent(self, event) -> None:
        if self.scheduler and self.scheduler.isRunning():
            self.hide()
            event.ignore()
            return
        super().closeEvent(event)
