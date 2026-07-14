from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QThread, QTime, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QImage, QMouseEvent, QPainter, QPen, QTextCursor
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame, QVideoSink
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QSplitter,
    QStyle,
    QTextEdit,
    QTimeEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from videotrans.configure import contants
from videotrans.configure.config import HOME_DIR, ROOT_DIR, params, tr
from videotrans.subtitle_removal.engine import EngineSpec, find_subtitle_remover_engine
from videotrans.util import tools

EVENT_PREFIX = "PYVT_EVENT "


def format_milliseconds(value: int) -> str:
    value = max(0, int(value))
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


class VideoSelectionCanvas(QWidget):
    selection_changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(520, 360)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._image = QImage()
        self._source_width = 0
        self._source_height = 0
        self._selection: QRectF | None = None
        self._drag_start: QPointF | None = None
        self.setStyleSheet("background:#111318;border:1px solid #343840;")

    @property
    def selection(self) -> tuple[int, int, int, int] | None:
        if self._selection is None or self._selection.width() < 2 or self._selection.height() < 2:
            return None
        rect = self._selection.normalized()
        return (
            round(rect.x()),
            round(rect.y()),
            max(1, round(rect.width())),
            max(1, round(rect.height())),
        )

    def set_frame(self, image: QImage) -> None:
        if image.isNull():
            return
        self._image = image.copy()
        self.update()

    def set_source_size(self, width: int, height: int) -> None:
        self._source_width = max(0, int(width))
        self._source_height = max(0, int(height))
        self.clear_selection()

    def _coordinate_size(self) -> tuple[int, int]:
        width = self._source_width or self._image.width()
        height = self._source_height or self._image.height()
        return width, height

    def clear_selection(self) -> None:
        self._selection = None
        self._drag_start = None
        self.selection_changed.emit(None)
        self.update()

    def _image_rect(self) -> QRectF:
        if self._image.isNull():
            return QRectF()
        available = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        scale = min(available.width() / self._image.width(), available.height() / self._image.height())
        width = self._image.width() * scale
        height = self._image.height() * scale
        return QRectF(
            available.center().x() - width / 2,
            available.center().y() - height / 2,
            width,
            height,
        )

    def _to_source(self, point: QPointF) -> QPointF | None:
        image_rect = self._image_rect()
        if image_rect.isEmpty() or not image_rect.contains(point):
            return None
        source_width, source_height = self._coordinate_size()
        x = (point.x() - image_rect.x()) * source_width / image_rect.width()
        y = (point.y() - image_rect.y()) * source_height / image_rect.height()
        return QPointF(
            max(0.0, min(x, float(source_width))),
            max(0.0, min(y, float(source_height))),
        )

    def _to_widget_rect(self, source_rect: QRectF) -> QRectF:
        image_rect = self._image_rect()
        if image_rect.isEmpty() or self._image.isNull():
            return QRectF()
        source_width, source_height = self._coordinate_size()
        return QRectF(
            image_rect.x() + source_rect.x() * image_rect.width() / source_width,
            image_rect.y() + source_rect.y() * image_rect.height() / source_height,
            source_rect.width() * image_rect.width() / source_width,
            source_rect.height() * image_rect.height() / source_height,
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        source = self._to_source(event.position())
        if source is None:
            return
        self._drag_start = source
        self._selection = QRectF(source, source)
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_start is None:
            return super().mouseMoveEvent(event)
        image_rect = self._image_rect()
        clamped = QPointF(
            max(image_rect.left(), min(event.position().x(), image_rect.right())),
            max(image_rect.top(), min(event.position().y(), image_rect.bottom())),
        )
        source = self._to_source(clamped)
        if source is None:
            return
        self._selection = QRectF(self._drag_start, source).normalized()
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._drag_start is None:
            return super().mouseReleaseEvent(event)
        self.mouseMoveEvent(event)
        self._drag_start = None
        if not self.selection:
            self._selection = None
        self.selection_changed.emit(self.selection)
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#111318"))
        if self._image.isNull():
            painter.setPen(QColor("#8e949e"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, tr("Select a video"))
            return
        target = self._image_rect()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(target, self._image)
        if self._selection is None:
            return
        widget_rect = self._to_widget_rect(self._selection.normalized())
        painter.fillRect(widget_rect, QColor(227, 69, 69, 52))
        painter.setPen(QPen(QColor("#ff5b57"), 2))
        painter.drawRect(widget_rect)


class SubtitleRemovalWorker(QThread):
    progress = Signal(int)
    log = Signal(str)
    completed = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        *,
        engine: EngineSpec,
        input_file: str,
        output_file: str,
        mode: str,
        rect: tuple[int, int, int, int] | None,
        start_ms: int,
        end_ms: int,
        parent=None,
    ):
        super().__init__(parent)
        self.engine = engine
        self.input_file = input_file
        self.output_file = output_file
        self.mode = mode
        self.rect = rect
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.process: subprocess.Popen | None = None
        self.cancelled = False

    def _command(self) -> list[str]:
        worker_script = Path(ROOT_DIR) / "scripts" / "subtitle_remove_worker.py"
        command = [
            str(self.engine.python),
            "-u",
            str(worker_script),
            "--project-root",
            str(ROOT_DIR),
            "--engine-root",
            str(self.engine.root),
            "--input",
            self.input_file,
            "--output",
            self.output_file,
            "--mode",
            self.mode,
            "--start-ms",
            str(self.start_ms),
            "--end-ms",
            str(self.end_ms),
        ]
        if self.rect:
            command.extend(["--rect", *(str(value) for value in self.rect)])
        return command

    def run(self) -> None:
        output_tail: list[str] = []
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self.process = subprocess.Popen(
                self._command(),
                cwd=self.engine.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
            assert self.process.stdout is not None
            for raw_line in self.process.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                output_tail.append(line)
                output_tail = output_tail[-20:]
                event_position = line.rfind(EVENT_PREFIX)
                if event_position < 0:
                    continue
                try:
                    event = json.loads(line[event_position + len(EVENT_PREFIX):])
                except json.JSONDecodeError:
                    continue
                event_type = event.get("type")
                if event_type == "progress":
                    self.progress.emit(max(0, min(100, int(event.get("value", 0)))))
                elif event_type == "log":
                    self.log.emit(str(event.get("message", "")))
                elif event_type == "error":
                    self.log.emit(str(event.get("message", "")))

            return_code = self.process.wait()
            if self.cancelled:
                self.failed.emit(tr("Task stopped"))
            elif return_code != 0:
                detail = "\n".join(output_tail[-8:])
                self.failed.emit(tr("Subtitle removal failed") + (f"\n{detail}" if detail else ""))
            elif not Path(self.output_file).is_file():
                self.failed.emit(tr("Output video was not created"))
            else:
                self.progress.emit(100)
                self.completed.emit(self.output_file)
        except Exception as error:
            self.failed.emit(str(error))
        finally:
            self.process = None

    def cancel(self) -> None:
        self.cancelled = True
        process = self.process
        if process is None or process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            )
        else:
            process.terminate()


class SubtitleRemovalWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Remove burned-in subtitles"))
        self.resize(1120, 720)
        self.input_file = ""
        self.output_file = ""
        self.duration_ms = 0
        self.slider_dragging = False
        self.worker: SubtitleRemovalWorker | None = None
        self.engine = find_subtitle_remover_engine(ROOT_DIR)

        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.45)
        self.video_sink = QVideoSink(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_sink)

        self._build_ui()
        self._bind_signals()
        self._update_engine_status()

    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        source_row = QHBoxLayout()
        self.source_path = QLineEdit()
        self.source_path.setReadOnly(True)
        self.source_path.setPlaceholderText(tr("Select a video"))
        self.select_button = QPushButton(tr("Select a video"))
        self.select_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton))
        source_row.addWidget(self.source_path, 1)
        source_row.addWidget(self.select_button)
        root.addLayout(source_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        preview_panel = QWidget()
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(8)
        self.canvas = VideoSelectionCanvas()
        preview_layout.addWidget(self.canvas, 1)

        playback = QHBoxLayout()
        self.play_button = QToolButton()
        self.play_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.play_button.setToolTip(tr("Play or pause"))
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.time_label = QLabel("00:00:00.000 / 00:00:00.000")
        self.time_label.setMinimumWidth(185)
        playback.addWidget(self.play_button)
        playback.addWidget(self.seek_slider, 1)
        playback.addWidget(self.time_label)
        preview_layout.addLayout(playback)

        control_panel = QWidget()
        control_panel.setMinimumWidth(330)
        control_panel.setMaximumWidth(420)
        controls = QVBoxLayout(control_panel)
        controls.setContentsMargins(14, 0, 0, 0)
        controls.setSpacing(10)

        controls.addWidget(QLabel(tr("Selected area")))
        selection_row = QHBoxLayout()
        self.selection_label = QLabel(tr("No area selected"))
        self.selection_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.clear_selection_button = QToolButton()
        self.clear_selection_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogResetButton))
        self.clear_selection_button.setToolTip(tr("Clear selected area"))
        selection_row.addWidget(self.selection_label, 1)
        selection_row.addWidget(self.clear_selection_button)
        controls.addLayout(selection_row)

        controls.addWidget(QLabel(tr("Processing range")))
        time_form = QFormLayout()
        self.start_time = self._new_time_edit()
        self.end_time = self._new_time_edit()
        start_row = self._time_row(self.start_time, tr("Use current position"), self._set_start_to_current)
        end_row = self._time_row(self.end_time, tr("Use current position"), self._set_end_to_current)
        time_form.addRow(tr("Start"), start_row)
        time_form.addRow(tr("End"), end_row)
        controls.addLayout(time_form)

        self.auto_button = QPushButton(tr("Auto detect and remove"))
        self.auto_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
        self.force_button = QPushButton(tr("Force remove selected area"))
        self.force_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon))
        self.force_button.setStyleSheet(
            "QPushButton{background:#c63f3f;color:white;font-weight:600;padding:8px;}"
            "QPushButton:disabled{background:#5f6268;color:#c7c7c7;}"
        )
        self.stop_button = QPushButton(tr("Stop"))
        self.stop_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop))
        self.stop_button.setVisible(False)
        controls.addWidget(self.auto_button)
        controls.addWidget(self.force_button)
        controls.addWidget(self.stop_button)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        controls.addWidget(self.progress_bar)

        self.engine_status = QLabel()
        self.engine_status.setWordWrap(True)
        controls.addWidget(self.engine_status)

        self.logs = QTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMinimumHeight(145)
        controls.addWidget(self.logs, 1)

        result_row = QHBoxLayout()
        self.open_result_button = QPushButton(tr("Open result"))
        self.open_result_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
        self.open_result_button.setEnabled(False)
        self.open_folder_button = QToolButton()
        self.open_folder_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon))
        self.open_folder_button.setToolTip(tr("Open output folder"))
        result_row.addWidget(self.open_result_button, 1)
        result_row.addWidget(self.open_folder_button)
        controls.addLayout(result_row)

        splitter.addWidget(preview_panel)
        splitter.addWidget(control_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        root.addWidget(splitter, 1)
        self.setCentralWidget(central)

    def _new_time_edit(self) -> QTimeEdit:
        edit = QTimeEdit(QTime(0, 0, 0, 0))
        edit.setDisplayFormat("HH:mm:ss.zzz")
        edit.setMaximumTime(QTime(23, 59, 59, 999))
        return edit

    def _time_row(self, edit: QTimeEdit, tooltip: str, callback) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        button = QToolButton()
        button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown))
        button.setToolTip(tooltip)
        button.clicked.connect(callback)
        layout.addWidget(edit, 1)
        layout.addWidget(button)
        return widget

    def _bind_signals(self) -> None:
        self.select_button.clicked.connect(self._select_video)
        self.play_button.clicked.connect(self._toggle_playback)
        self.seek_slider.sliderPressed.connect(lambda: setattr(self, "slider_dragging", True))
        self.seek_slider.sliderReleased.connect(self._seek_from_slider)
        self.seek_slider.sliderMoved.connect(self._preview_slider_time)
        self.clear_selection_button.clicked.connect(self.canvas.clear_selection)
        self.canvas.selection_changed.connect(self._selection_changed)
        self.auto_button.clicked.connect(lambda: self._start_removal("auto"))
        self.force_button.clicked.connect(lambda: self._start_removal("force"))
        self.stop_button.clicked.connect(self._stop_removal)
        self.open_result_button.clicked.connect(self._open_result)
        self.open_folder_button.clicked.connect(self._open_output_folder)

        self.video_sink.videoFrameChanged.connect(self._video_frame_changed)
        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(self._duration_changed)
        self.player.playbackStateChanged.connect(self._playback_state_changed)
        self.player.errorOccurred.connect(lambda _error, text: self._append_log(text) if text else None)

    def _update_engine_status(self) -> None:
        if self.engine is None:
            self.engine_status.setText(tr("Subtitle removal engine is not installed"))
            self.engine_status.setStyleSheet("color:#d9534f;")
            self.auto_button.setEnabled(False)
            self.force_button.setEnabled(False)
            return
        self.engine_status.setText(tr("STTN engine ready"))
        self.engine_status.setStyleSheet("color:#4fa66a;")
        self._update_action_state()

    def _select_video(self) -> None:
        formats = " ".join(f"*.{suffix}" for suffix in contants.VIDEO_EXTS)
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            tr("Select a video"),
            params.get("last_opendir", ""),
            f"Video files ({formats})",
        )
        if not file_path:
            return
        self.input_file = str(Path(file_path).resolve())
        try:
            video_info = tools.get_video_info(self.input_file)
        except Exception as error:
            QMessageBox.critical(self, tr("Remove burned-in subtitles"), str(error))
            self.input_file = ""
            return
        params["last_opendir"] = str(Path(file_path).parent)
        self.source_path.setText(self.input_file)
        self.output_file = ""
        self.open_result_button.setEnabled(False)
        self.canvas.clear_selection()
        self.logs.clear()
        self.progress_bar.setValue(0)
        self.canvas.set_source_size(video_info["width"], video_info["height"])
        self._duration_changed(video_info["time"])
        self.player.setSource(QUrl.fromLocalFile(self.input_file))
        self.player.setPosition(0)
        self.player.play()
        QTimer.singleShot(180, self.player.pause)
        self._update_action_state()

    def _video_frame_changed(self, frame: QVideoFrame) -> None:
        image = frame.toImage()
        if not image.isNull():
            self.canvas.set_frame(image)

    def _toggle_playback(self) -> None:
        if not self.input_file:
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _playback_state_changed(self, state) -> None:
        icon = QStyle.StandardPixmap.SP_MediaPause if state == QMediaPlayer.PlaybackState.PlayingState else QStyle.StandardPixmap.SP_MediaPlay
        self.play_button.setIcon(self.style().standardIcon(icon))

    def _duration_changed(self, duration: int) -> None:
        self.duration_ms = max(0, duration)
        self.seek_slider.setRange(0, self.duration_ms)
        self._set_time_value(self.end_time, self.duration_ms)
        self._update_time_label(self.player.position())

    def _position_changed(self, position: int) -> None:
        if not self.slider_dragging:
            self.seek_slider.setValue(position)
        self._update_time_label(position)

    def _seek_from_slider(self) -> None:
        self.slider_dragging = False
        self.player.setPosition(self.seek_slider.value())

    def _preview_slider_time(self, position: int) -> None:
        self._update_time_label(position)

    def _update_time_label(self, position: int) -> None:
        self.time_label.setText(
            f"{format_milliseconds(position)} / {format_milliseconds(self.duration_ms)}"
        )

    def _set_start_to_current(self) -> None:
        self._set_time_value(self.start_time, self.player.position())

    def _set_end_to_current(self) -> None:
        self._set_time_value(self.end_time, self.player.position())

    @staticmethod
    def _set_time_value(edit: QTimeEdit, milliseconds: int) -> None:
        edit.setTime(QTime(0, 0, 0, 0).addMSecs(max(0, milliseconds)))

    @staticmethod
    def _time_value(edit: QTimeEdit) -> int:
        return QTime(0, 0, 0, 0).msecsTo(edit.time())

    def _selection_changed(self, rect) -> None:
        if rect is None:
            self.selection_label.setText(tr("No area selected"))
        else:
            x, y, width, height = rect
            self.selection_label.setText(f"x={x}, y={y}, {width} x {height}")
        self._update_action_state()

    def _update_action_state(self) -> None:
        ready = bool(self.input_file and self.engine and self.worker is None)
        self.auto_button.setEnabled(ready)
        self.force_button.setEnabled(ready and self.canvas.selection is not None)

    def _start_removal(self, mode: str) -> None:
        if not self.input_file or self.engine is None:
            return
        rect = self.canvas.selection
        if mode == "force" and rect is None:
            QMessageBox.warning(self, tr("Remove burned-in subtitles"), tr("Select an area first"))
            return
        start_ms = self._time_value(self.start_time)
        end_ms = self._time_value(self.end_time)
        if end_ms <= start_ms:
            QMessageBox.warning(self, tr("Remove burned-in subtitles"), tr("End time must be after start time"))
            return
        missing = self.engine.missing_files(mode)
        if missing:
            QMessageBox.critical(
                self,
                tr("Remove burned-in subtitles"),
                tr("Subtitle removal model is incomplete") + "\n" + "\n".join(str(path) for path in missing),
            )
            return

        result_dir = Path(HOME_DIR) / "subtitle_removal"
        result_dir.mkdir(parents=True, exist_ok=True)
        suffix = "forced" if mode == "force" else "auto"
        output_file = result_dir / f"{Path(self.input_file).stem}_{suffix}_{datetime.now():%Y%m%d_%H%M%S}.mp4"
        self.output_file = str(output_file)
        self.logs.clear()
        self.progress_bar.setValue(0)
        self._append_log(tr("Subtitle removal started"))
        self.worker = SubtitleRemovalWorker(
            engine=self.engine,
            input_file=self.input_file,
            output_file=self.output_file,
            mode=mode,
            rect=rect,
            start_ms=start_ms,
            end_ms=end_ms,
            parent=self,
        )
        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.log.connect(self._append_log)
        self.worker.completed.connect(self._removal_completed)
        self.worker.failed.connect(self._removal_failed)
        self.worker.finished.connect(self._worker_finished)
        self._set_busy(True)
        self.worker.start()

    def _set_busy(self, busy: bool) -> None:
        self.select_button.setDisabled(busy)
        self.start_time.setDisabled(busy)
        self.end_time.setDisabled(busy)
        self.clear_selection_button.setDisabled(busy)
        self.stop_button.setVisible(busy)
        self.auto_button.setDisabled(busy)
        self.force_button.setDisabled(busy)
        if not busy:
            self._update_action_state()

    def _stop_removal(self) -> None:
        if self.worker:
            self._append_log(tr("Stopping task"))
            self.worker.cancel()

    def _removal_completed(self, output_file: str) -> None:
        self.output_file = output_file
        self.progress_bar.setValue(100)
        self.open_result_button.setEnabled(True)
        self._append_log(tr("Subtitle removal completed") + f"\n{output_file}")

    def _removal_failed(self, message: str) -> None:
        self._append_log(message)
        QMessageBox.critical(self, tr("Remove burned-in subtitles"), message)

    def _worker_finished(self) -> None:
        worker = self.worker
        self.worker = None
        self._set_busy(False)
        if worker:
            worker.deleteLater()

    def _append_log(self, message: str) -> None:
        if message:
            self.logs.append(message)
            cursor = self.logs.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.logs.setTextCursor(cursor)

    def _open_result(self) -> None:
        if self.output_file and Path(self.output_file).is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.output_file))

    def _open_output_folder(self) -> None:
        result_dir = Path(HOME_DIR) / "subtitle_removal"
        result_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(result_dir)))

    def closeEvent(self, event) -> None:
        self.player.stop()
        if self.worker:
            self.worker.cancel()
            self.worker.wait(5000)
        super().closeEvent(event)


def create_subtitle_removal_window(parent=None) -> SubtitleRemovalWindow:
    window = SubtitleRemovalWindow(parent)
    window.show()
    QApplication.processEvents()
    return window
