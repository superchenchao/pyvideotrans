import json
import time
from collections import Counter
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt, QTimer, QSize, QUrl, QPoint
from PySide6.QtGui import QIcon, QDesktopServices, QColor
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLineEdit, QLabel, QCheckBox,
    QComboBox, QPushButton, QWidget, QGroupBox,
    QMessageBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QGridLayout, QScrollArea, QFrame,
    QListWidget, QListWidgetItem, QTabWidget
)

from videotrans.configure.config import ROOT_DIR, tr, app_cfg, settings,  logger
from videotrans.configure import config
from videotrans.util import tools


class CharacterPickerDialog(QDialog):
    """Compact per-subtitle character picker."""

    def __init__(
        self, *, episode_options, series_options, current_id="",
        show_series_scope=True, parent=None
    ):
        super().__init__(parent)
        self.selected_character_id = ""
        self.current_id = str(current_id or "")
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setModal(True)
        self.show_series_scope = bool(show_series_scope)
        visible_count = max(
            len(episode_options),
            len(series_options) if self.show_series_scope else 0,
        )
        content_height = min(360, max(1, visible_count) * 42 + 8)
        self.setFixedSize(
            270, content_height + (42 if self.show_series_scope else 0)
        )
        self.setStyleSheet("""
            QDialog { background:#f7f8fa; border:1px solid #d9dde3; color:#253047; }
            QTabWidget::pane { border:none; background:white; }
            QTabBar::tab { background:#f1f2f4; color:#4f566b; padding:8px 18px; }
            QTabBar::tab:selected { background:white; color:#20283a; font-weight:600; }
            QListWidget { background:white; color:#34405a; border:none; outline:none; }
            QListWidget::item { color:#34405a; padding:2px 10px; }
            QListWidget::item:hover { background:#eef4ff; }
            QListWidget::item:selected { background:#e7efff; color:#5872e8; }
            QListWidget::item:disabled { color:#34405a; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        if self.show_series_scope:
            self.tabs = QTabWidget()
            self.tabs.addTab(
                self._build_list(episode_options), f"本集 {len(episode_options)}"
            )
            self.tabs.addTab(
                self._build_list(series_options), f"全片 {len(series_options)}"
            )
            layout.addWidget(self.tabs)
        else:
            self.tabs = None
            layout.addWidget(self._build_list(episode_options))

    def _build_list(self, options):
        widget = QListWidget()
        for option in options:
            character_id = str(option.get("character_id", ""))
            selected = character_id == self.current_id
            marker = "●" if selected else "○"
            percent = int(option.get("percent", 0))
            item = QListWidgetItem(
                f"{marker}  {option.get('name', '未命名角色')}    {percent}%"
            )
            item.setSizeHint(QSize(0, 40))
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            item.setData(Qt.UserRole, character_id)
            widget.addItem(item)
            if selected:
                widget.setCurrentItem(item)
        widget.itemClicked.connect(self._choose)
        return widget

    def _choose(self, item):
        self.selected_character_id = str(item.data(Qt.UserRole) or "")
        if self.selected_character_id:
            self.accept()

    def place_below(self, widget):
        position = widget.mapToGlobal(QPoint(0, widget.height()))
        self.move(position)


class SpeakerAssignmentDialog(QDialog):
    def __init__(
            self,
            parent=None,
            target_sub: str = None,
            all_voices: Optional[List[str]] = None,
            source_sub: str = None,
            source_audio: str = None,
            cache_folder=None,
            target_language="en",
            source_language="",
            tts_type=0,
            default_role="",
            video_path=None,
            series_folder=None,
            series_video_paths=None,
            series_output_dir=None,
            countdown_enabled: bool = True,
    ):
        super().__init__()
        self.parent = parent
        self.target_sub = target_sub
        self.source_sub = source_sub
        self.source_audio = source_audio
        self.source_srtstring = None
        self.cache_folder = cache_folder
        self.target_language = target_language
        self.source_language = source_language
        self.tts_type = tts_type
        self.default_role = default_role
        self.countdown_enabled = countdown_enabled
        from videotrans.process.series_speakers import voice_scope_key
        self.voice_scope = voice_scope_key(tts_type, target_language)
        self.video_path = video_path
        self.series_folder = series_folder or (
            Path(video_path).parent.as_posix() if video_path else ""
        )
        self.series_video_paths = [
            Path(value).as_posix()
            for value in (series_video_paths or ([video_path] if video_path else []))
            if value
        ]
        self.has_series_scope = len(self.series_video_paths) > 1
        self.series_output_dir = series_output_dir
        if series_output_dir:
            from videotrans.process.series_speakers import manifest_path_for_series
            self.series_manifest_path = manifest_path_for_series(
                series_output_dir, self.series_folder, self.series_video_paths
            )
        else:
            self.series_manifest_path = None
        self.series_manifest = {
            "version": 1,
            "series_folder": self.series_folder,
            "series_videos": self.series_video_paths,
            "characters": [],
            "episodes": {},
        }
        self.current_episode_key = (
            Path(video_path).name.casefold() if video_path
            else Path(target_sub).parent.name.casefold()
        )
        self.current_character_map = {}
        self.sidebar_checks = {}
        self.sidebar_scope = "series" if self.has_series_scope else "episode"

        if source_sub:
            sour_pt = Path(source_sub)
            if sour_pt.as_posix() and not sour_pt.samefile(Path(target_sub)):
                try:
                    self.source_srtstring = sour_pt.read_text(encoding="utf-8-sig")
                except Exception:
                    self.source_srtstring = ""

        self.srt_list_dict = tools.get_subtitle_from_srt(self.target_sub)
        self.source_srt_list = (
            tools.get_subtitle_from_srt(self.source_sub)
            if self.source_sub and Path(self.source_sub).is_file() else []
        )

        # 说话人数据初始化
        self.speaker_list_sub = []
        self.speakers = {}
        try:
            spk_json_path = Path(f'{self.cache_folder}/speaker.json')
            _list_sub = [] if not spk_json_path.exists() else json.loads(spk_json_path.read_text(encoding='utf-8'))
            _set = set(_list_sub) if _list_sub else None
            if _set and len(_set) > 1:
                self.speaker_list_sub = _list_sub
                self.speakers = {it: None for it in sorted(list(_set))}
        except Exception as e:
            logger.exception(f'获取说话人id失败:{e}', exc_info=True)

        self.all_voices = all_voices or []
        self._auto_assigned_speakers = False
        self.voice_tags = self._load_voice_tags()

        self.setWindowTitle(tr("zidonghebingmiaohou"))
        self.setWindowIcon(QIcon(f"{ROOT_DIR}/videotrans/styles/icon.ico"))
        parent_width = getattr(parent, 'width', 1280)
        parent_height = getattr(parent, 'height', 800)
        if callable(parent_width):
            parent_width = parent_width()
        if callable(parent_height):
            parent_height = parent_height()
        self.setMinimumWidth(int(parent_width * 0.95))
        self.setMinimumHeight(int(parent_height * 0.95))
        self.setWindowFlags(
            Qt.WindowTitleHint |            # 显示标题栏
            Qt.CustomizeWindowHint |        # 允许自定义标题栏按钮（否则OS会强制加关闭按钮）
            Qt.WindowMaximizeButtonHint     # 只加最大化按钮，不加关闭按钮
        )

        self.setStyleSheet("""
            QDialog { background:#121d28; color:#e8eef5; }
            QWidget#sidebar { background:#101a24; border-right:1px solid #2c3b4b; }
            QWidget#sidebarList { background:#101a24; }
            QFrame#speakerRow { background:#101a24; border:1px solid transparent; border-radius:7px; }
            QFrame#speakerRow:hover { background:#17283a; border-color:#29445f; }
            QPushButton { min-height:28px; padding:0 12px; border:1px solid #40546a;
                          border-radius:5px; background:#26374a; color:#eef5ff; }
            QPushButton:hover { background:#30465f; }
            QPushButton#primaryButton { background:#2878d4; border-color:#3889e6; }
            QPushButton#dangerButton { background:transparent; border:none; color:#ff705f; }
            QPushButton#scopeButton { background:#172330; border-color:#31465c; }
            QPushButton#scopeButton:checked { background:#294f7d; border-color:#397bd0; }
            QLineEdit, QComboBox { min-height:28px; border:1px solid #40546a;
                                  border-radius:5px; background:#14212e; padding:0 8px; }
            QTableWidget { background:#121d28; border:none; gridline-color:#263747; }
            QTableWidget::item { padding:7px; border-bottom:1px solid #263747; }
            QHeaderView::section { background:#162330; color:#dce7f3; padding:8px;
                                   border:none; border-bottom:1px solid #33485e; }
            QScrollArea { border:none; background:transparent; }
        """)

        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.sidebar = self._create_sidebar()
        main_layout.addWidget(self.sidebar)

        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(14, 12, 14, 10)
        content_layout.setSpacing(10)
        main_layout.addWidget(content_widget, stretch=1)

        # --- 顶部工具栏：查找替换 ---
        search_replace_layout = QHBoxLayout()
        self.series_status_label = QLabel(
            "正在读取剧集角色档案…"
            if self.has_series_scope else "正在读取当前视频角色…"
        )
        self.series_status_label.setStyleSheet("color:#9fb2c5")
        search_replace_layout.addWidget(self.series_status_label)
        search_replace_layout.addStretch()
        self.search_input = QLineEdit()
        self.search_input.setMaximumWidth(220)
        self.search_input.setPlaceholderText("查找字幕")
        search_replace_layout.addWidget(self.search_input)
        self.replace_input = QLineEdit()
        self.replace_input.setPlaceholderText("替换为")
        self.replace_input.setMaximumWidth(220)
        search_replace_layout.addWidget(self.replace_input)
        replace_button = QPushButton("全部替换")
        replace_button.setMinimumWidth(100)
        replace_button.setCursor(Qt.PointingHandCursor)
        replace_button.clicked.connect(self.replace_text)
        search_replace_layout.addWidget(replace_button)
        content_layout.addLayout(search_replace_layout)

        self.info_banner = QLabel(
            "已根据说话人自动匹配音色。跨集结果置信度不足时会保留为新角色，避免误合并。"
            if self.has_series_scope
            else "已根据当前视频的说话人自动匹配角色和音色，可逐句检查并修改。"
        )
        self.info_banner.setWordWrap(True)
        self.info_banner.setStyleSheet(
            "background:#15375a;color:#cfe7ff;border:1px solid #285d8e;"
            "border-radius:5px;padding:8px 10px;"
        )
        content_layout.addWidget(self.info_banner)

        self.right_widget = QWidget()
        self.right_layout = QVBoxLayout(self.right_widget)
        self.right_layout.setContentsMargins(0, 0, 0, 0)
        
        # Loading 区域
        self.loading_widget = QWidget()
        load_layout = QVBoxLayout(self.loading_widget)
        self.loading_label = QLabel(tr('Loading...'), self)
        self.loading_label.setAlignment(Qt.AlignCenter)
        load_layout.addWidget(self.loading_label)
        self.right_layout.addWidget(self.loading_widget)

        # 表格容器
        self.table_container = QWidget()
        self.table_container_layout = QVBoxLayout(self.table_container)
        self.table_container.setVisible(False)
        self.right_layout.addWidget(self.table_container)
        
        # 底部按钮容器
        self.bottom_button_container = QWidget()
        self.bottom_button_container_layout = QHBoxLayout(self.bottom_button_container)
        self.bottom_button_container.setVisible(False)
        self.right_layout.addWidget(self.bottom_button_container)
        content_layout.addWidget(self.right_widget, stretch=1)

        # --- 底部按钮 ---
        self.save_button = QPushButton("保存修改并继续")
        self.save_button.setObjectName("primaryButton")
        self.save_button.setCursor(Qt.PointingHandCursor)
        self.save_button.setMinimumSize(QSize(300, 35))
        self.save_button.clicked.connect(self.save_and_close)

        self.save_button2 = QPushButton("不保存只继续")
        self.save_button2.setCursor(Qt.PointingHandCursor)
        self.save_button2.setToolTip(tr('bubaocunshuoming', self.target_sub))
        self.save_button2.setMinimumSize(QSize(200, 35))
        self.save_button2.clicked.connect(self.save_and_close2)

        self.opendir_button = QPushButton("打开字幕文件夹")
        self.opendir_button.setCursor(Qt.PointingHandCursor)
        self.opendir_button.setMaximumSize(QSize(150, 30))
        self.opendir_button.clicked.connect(self.opendir_sub)

        cancel_button = QPushButton("终止本次任务")
        cancel_button.setObjectName("dangerButton")
        cancel_button.setCursor(Qt.PointingHandCursor)
        cancel_button.setMinimumSize(QSize(150, 30))
        cancel_button.clicked.connect(self.cancel_and_close)

        bottom_layout = QHBoxLayout()
        bottom_layout.addWidget(self.opendir_button)
        bottom_layout.addStretch()
        bottom_layout.addWidget(self.save_button2)
        bottom_layout.addWidget(self.save_button)
        bottom_layout.addWidget(cancel_button)
        content_layout.addLayout(bottom_layout)

        countdown_layout = QHBoxLayout()
        countdown_layout.addStretch()
        self.count_down = int(float(settings.get('countdown_sec', 1)))
        self.prompt_label = QLabel(f"将在 {self.count_down} 秒后自动继续")
        self.prompt_label.setStyleSheet("color:#8ca1b5")
        countdown_layout.addWidget(self.prompt_label)
        self.stop_button = QPushButton("停止倒计时")
        self.stop_button.setCursor(Qt.PointingHandCursor)
        self.stop_button.clicked.connect(self.stop_countdown)
        countdown_layout.addWidget(self.stop_button)
        content_layout.addLayout(countdown_layout)
        self.prompt_label.setVisible(countdown_enabled)
        self.stop_button.setVisible(countdown_enabled)

        # 延迟加载表格
        QTimer.singleShot(10, self.load_table)

    def _create_sidebar(self):
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        # 常用音色名称（例如 Florian Multilingual(Male)）较长，保留足够
        # 宽度完整展示，避免用户只能依赖悬浮提示辨认音色。
        sidebar.setFixedWidth(370)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 16, 14, 14)
        layout.setSpacing(10)

        title = QLabel("音色管理")
        title.setStyleSheet("font-size:20px;font-weight:600;color:#f3f7fb")
        layout.addWidget(title)

        scope_layout = QHBoxLayout()
        scope_layout.setSpacing(0)
        self.series_scope_button = QPushButton("全片 0")
        self.series_scope_button.setObjectName("scopeButton")
        self.series_scope_button.setCheckable(True)
        self.series_scope_button.setChecked(self.has_series_scope)
        self.series_scope_button.clicked.connect(
            lambda: self._set_sidebar_scope("series")
        )
        scope_layout.addWidget(self.series_scope_button)
        self.series_scope_button.setVisible(self.has_series_scope)
        self.episode_scope_button = QPushButton(
            f"{'本集' if self.has_series_scope else '当前视频'} {len(self.speakers)}"
        )
        self.episode_scope_button.setObjectName("scopeButton")
        self.episode_scope_button.setCheckable(True)
        self.episode_scope_button.setChecked(not self.has_series_scope)
        self.episode_scope_button.clicked.connect(
            lambda: self._set_sidebar_scope("episode")
        )
        scope_layout.addWidget(self.episode_scope_button)
        layout.addLayout(scope_layout)

        self.sidebar_hint = QLabel(
            "全片包含本次导入批次中的所有视频"
            if self.has_series_scope else "当前仅处理这一个视频"
        )
        self.sidebar_hint.setWordWrap(True)
        self.sidebar_hint.setStyleSheet("color:#8195a9;font-size:12px")
        layout.addWidget(self.sidebar_hint)

        self.sidebar_scroll = QScrollArea()
        self.sidebar_scroll.setWidgetResizable(True)
        self.sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.sidebar_list_widget = QWidget()
        self.sidebar_list_widget.setObjectName("sidebarList")
        self.sidebar_list_layout = QVBoxLayout(self.sidebar_list_widget)
        self.sidebar_list_layout.setContentsMargins(0, 0, 0, 0)
        self.sidebar_list_layout.setSpacing(4)
        self.sidebar_list_layout.addStretch()
        self.sidebar_scroll.setWidget(self.sidebar_list_widget)
        layout.addWidget(self.sidebar_scroll, stretch=1)

        name_label = QLabel(
            "角色名称（确认一次后全剧复用）"
            if self.has_series_scope else "角色名称"
        )
        name_label.setStyleSheet("color:#9fb2c5")
        layout.addWidget(name_label)
        name_row = QHBoxLayout()
        self.character_name_input = QLineEdit()
        self.character_name_input.setPlaceholderText("输入角色名")
        name_row.addWidget(self.character_name_input, stretch=1)
        name_button = QPushButton("确认名称")
        name_button.clicked.connect(self.confirm_character_name)
        name_row.addWidget(name_button)
        layout.addLayout(name_row)

        self.identity_actions_widget = QWidget()
        identity_actions = QHBoxLayout(self.identity_actions_widget)
        identity_actions.setContentsMargins(0, 0, 0, 0)
        merge_button = QPushButton("合并所选角色")
        merge_button.setToolTip("用于把跨集识别出的重复角色合并为同一个人")
        merge_button.clicked.connect(self.merge_selected_characters)
        identity_actions.addWidget(merge_button)
        split_button = QPushButton("本集设为新角色")
        split_button.setToolTip("跨集识别错误时，将本集选中的说话人拆分为独立角色")
        split_button.clicked.connect(self.split_selected_episode_speaker)
        identity_actions.addWidget(split_button)
        layout.addWidget(self.identity_actions_widget)
        self.identity_actions_widget.setVisible(self.has_series_scope)

        voice_label = QLabel("为选中角色指定音色")
        voice_label.setStyleSheet("color:#9fb2c5")
        layout.addWidget(voice_label)
        self.speaker_combo = QComboBox()
        self._fill_role_combo(self.speaker_combo)
        from videotrans.component.voice_selector import install_voice_selector
        install_voice_selector(
            self.speaker_combo,
            tts_type_getter=lambda: self.tts_type,
            language_getter=lambda: self.target_language,
        )
        layout.addWidget(self.speaker_combo)
        voice_actions = QHBoxLayout()
        assign_button = QPushButton("应用音色")
        assign_button.clicked.connect(self.assign_speaker_roles)
        voice_actions.addWidget(assign_button, stretch=1)
        self.speaker_listen_button = QPushButton("试听")
        self.speaker_listen_button.clicked.connect(self.listen_speaker_dubbing)
        voice_actions.addWidget(self.speaker_listen_button)
        layout.addLayout(voice_actions)
        return sidebar

    def _set_sidebar_scope(self, scope):
        if not self.has_series_scope:
            scope = "episode"
        self.sidebar_scope = scope
        is_series = scope == "series"
        self.series_scope_button.setChecked(is_series)
        self.episode_scope_button.setChecked(not is_series)
        self.sidebar_hint.setText(
            "全片包含本次导入批次中的所有视频"
            if is_series else (
                "本集只修改当前正在处理的视频"
                if self.has_series_scope else "当前仅处理这一个视频"
            )
        )
        self._refresh_sidebar()

    def _clear_sidebar_rows(self):
        while self.sidebar_list_layout.count():
            item = self.sidebar_list_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.hide()
                widget.deleteLater()

    def _add_sidebar_row(self, *, key, name, stats, voice, tooltip=""):
        row = QFrame()
        row.setObjectName("speakerRow")
        row.setToolTip(tooltip)
        row_layout = QVBoxLayout(row)
        row_layout.setContentsMargins(7, 7, 7, 7)
        row_layout.setSpacing(5)

        identity_layout = QHBoxLayout()
        identity_layout.setContentsMargins(0, 0, 0, 0)
        identity_layout.setSpacing(8)

        checkbox = QCheckBox()
        checkbox.stateChanged.connect(self._sync_name_input_from_selection)
        identity_layout.addWidget(checkbox)

        avatar = QLabel(name[:1] if name else "?")
        avatar.setAlignment(Qt.AlignCenter)
        avatar.setFixedSize(34, 34)
        hue = abs(hash(str(key))) % 240 + 40
        avatar.setStyleSheet(
            f"background:hsl({hue},45%,30%);border:2px solid hsl({hue},65%,60%);"
            "border-radius:17px;font-weight:600;color:white"
        )
        identity_layout.addWidget(avatar)

        text_layout = QVBoxLayout()
        text_layout.setSpacing(1)
        name_label = QLabel(name)
        name_label.setStyleSheet("font-weight:600;color:#eef5fb")
        text_layout.addWidget(name_label)
        stats_label = QLabel(stats)
        stats_label.setStyleSheet("color:#8195a9;font-size:12px")
        text_layout.addWidget(stats_label)
        identity_layout.addLayout(text_layout, stretch=1)
        row_layout.addLayout(identity_layout)

        voice_button = QPushButton(
            f"{self._role_label(voice) if voice else '选择音色'}  ▾"
        )
        voice_button.setObjectName("characterVoiceButton")
        voice_button.setCursor(Qt.PointingHandCursor)
        voice_button.setToolTip(
            f"当前音色：{self._role_label(voice)}\n点击切换"
            if voice else "点击选择音色"
        )
        voice_button.setStyleSheet(
            "QPushButton{background:#172635;border:1px solid #2b4257;"
            "border-radius:5px;padding:5px 7px;text-align:left;"
            f"color:{'#9fc9ff' if voice else '#ff9a91'};}}"
            "QPushButton:hover{background:#20364a;border-color:#4e83b2;}"
        )
        voice_button.clicked.connect(
            lambda checked=False, identity=key: self.open_character_voice_selector(identity)
        )
        voice_layout = QHBoxLayout()
        voice_layout.setContentsMargins(68, 0, 0, 0)
        voice_layout.addWidget(voice_button, stretch=1)
        row_layout.addLayout(voice_layout)
        self.sidebar_list_layout.addWidget(row)
        self.sidebar_checks[checkbox] = key

    def _voice_for_sidebar_key(self, key):
        kind, value = key
        if kind == "speaker":
            speaker = str(value)
            character = self._character_for_speaker(speaker)
            if self.speakers.get(speaker):
                return self.speakers[speaker]
        else:
            character = self._character_index().get(str(value))
        if character:
            from videotrans.process.series_speakers import character_voice
            return character_voice(character, self.voice_scope)
        return ""

    def open_character_voice_selector(self, key):
        from videotrans.component.voice_selector import VoiceSelectorDialog
        dialog = VoiceSelectorDialog(
            parent=self,
            tts_type=self.tts_type,
            language=self.target_language,
            roles=self.all_voices,
            current_role=self._voice_for_sidebar_key(key),
        )
        if dialog.exec() == QDialog.Accepted and dialog.selected_role is not None:
            self._apply_voice_to_sidebar_key(key, dialog.selected_role)

    def _apply_voice_to_sidebar_key(self, key, voice):
        role_value = None if str(voice) == "No" else str(voice)
        kind, value = key
        characters = self._character_index()
        from videotrans.process.series_speakers import set_character_voice
        if kind == "speaker":
            speaker = str(value)
            self.speakers[speaker] = role_value
            character_id = self.current_character_map.get(speaker, "")
            if character_id in characters:
                set_character_voice(
                    self.series_manifest,
                    character_id,
                    role_value or "",
                    scope=self.voice_scope,
                )
        else:
            character_id = str(value)
            if character_id in characters:
                set_character_voice(
                    self.series_manifest,
                    character_id,
                    role_value or "",
                    scope=self.voice_scope,
                )
            for speaker, mapped_id in self.current_character_map.items():
                if mapped_id == character_id and speaker in self.speakers:
                    self.speakers[speaker] = role_value
        self._save_series_manifest()
        self._update_role_column()

    def _refresh_sidebar(self):
        if not hasattr(self, 'sidebar_list_layout'):
            return
        self._clear_sidebar_rows()
        self.sidebar_checks = {}
        from videotrans.process.series_speakers import character_voice
        counts = Counter(self.speaker_list_sub)
        total_lines = sum(counts.values()) or 1

        if self.sidebar_scope == "episode":
            for speaker in sorted(
                self.speakers,
                key=lambda value: (-counts.get(value, 0), str(value)),
            ):
                line_count = counts.get(speaker, 0)
                character = self._character_for_speaker(speaker)
                if character:
                    from videotrans.process.series_speakers import character_display_name
                    name = character_display_name(character)
                    voice = self.speakers.get(speaker) or character_voice(
                        character, self.voice_scope
                    )
                    tooltip = f"本集说话人 {speaker} · {character.get('id', '')}"
                else:
                    name = f"未命名 · {speaker}"
                    voice = self.speakers.get(speaker) or ''
                    tooltip = "跨集身份尚未建立"
                percent = round(line_count * 100 / total_lines)
                self._add_sidebar_row(
                    key=("speaker", speaker),
                    name=name,
                    stats=f"{line_count}句 · {percent}%",
                    voice=self._role_label(voice) if voice else "",
                    tooltip=tooltip,
                )
        else:
            characters = list(self.series_manifest.get('characters', []))
            characters.sort(
                key=lambda item: (
                    -sum(
                        int(ep.get('line_count', 0))
                        for ep in item.get('episodes', {}).values()
                    ),
                    str(item.get('id', '')),
                )
            )
            for character in characters:
                from videotrans.process.series_speakers import character_display_name
                episodes = character.get('episodes', {})
                line_count = sum(
                    int(ep.get('line_count', 0)) for ep in episodes.values()
                )
                confirmed = bool(character.get('name_confirmed'))
                match_text = "已确认" if confirmed else "待确认"
                self._add_sidebar_row(
                    key=("character", character.get('id')),
                    name=character_display_name(character),
                    stats=f"{len(episodes)}集 · {line_count}句 · {match_text}",
                    voice=self._role_label(
                        character_voice(character, self.voice_scope)
                    ),
                    tooltip=str(character.get('id', '')),
                )
            if not characters:
                empty_label = QLabel("尚无全片角色数据\n处理本集后会自动建立跨集角色档案")
                empty_label.setAlignment(Qt.AlignCenter)
                empty_label.setWordWrap(True)
                empty_label.setStyleSheet("color:#71869a;padding:30px 12px")
                self.sidebar_list_layout.addWidget(empty_label)
        self.sidebar_list_layout.addStretch()

    def _character_index(self):
        return {
            str(item.get('id')): item
            for item in self.series_manifest.get('characters', [])
            if item.get('id')
        }

    def _character_for_speaker(self, speaker):
        character_id = self.current_character_map.get(str(speaker), '')
        return self._character_index().get(character_id)

    def _selected_character_ids(self):
        character_ids = []
        for checkbox, (kind, value) in self.sidebar_checks.items():
            if not checkbox.isChecked():
                continue
            character_id = (
                value if kind == "character"
                else self.current_character_map.get(str(value), "")
            )
            if character_id and character_id not in character_ids:
                character_ids.append(character_id)
        return character_ids

    def _sync_name_input_from_selection(self):
        character_ids = self._selected_character_ids()
        if len(character_ids) != 1:
            return
        character = self._character_index().get(character_ids[0], {})
        name = str(character.get('name', '') or '').strip()
        if not name:
            candidates = character.get('name_candidates', [])
            if candidates:
                name = str(candidates[0].get('name', '') or '').strip()
        self.character_name_input.setText(name)

    def confirm_character_name(self):
        character_ids = self._selected_character_ids()
        name = self.character_name_input.text().strip()
        if len(character_ids) != 1 or not name:
            QMessageBox.information(self, "角色名称", "请只选择一个角色并输入名称。")
            return
        from videotrans.process.series_speakers import set_character_name
        set_character_name(self.series_manifest, character_ids[0], name, confirmed=True)
        self._save_series_manifest()
        self._refresh_sidebar()
        self._refresh_table_speaker_names()

    def merge_selected_characters(self):
        character_ids = self._selected_character_ids()
        if len(character_ids) < 2:
            QMessageBox.information(self, "合并角色", "请至少选择两个跨集角色。")
            return
        characters = self._character_index()
        target_id = next(
            (
                character_id for character_id in character_ids
                if characters.get(character_id, {}).get('name_confirmed')
            ),
            character_ids[0],
        )
        target_name = characters.get(target_id, {}).get('name') or target_id
        reply = QMessageBox.question(
            self,
            "合并角色",
            f"确定将选中的 {len(character_ids)} 个角色合并到“{target_name}”吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            from videotrans.process.series_speakers import merge_characters
            merge_characters(
                self.series_manifest,
                target_id,
                [value for value in character_ids if value != target_id],
            )
        except (KeyError, ValueError) as error:
            QMessageBox.warning(self, "无法合并", str(error))
            return
        self._save_series_manifest()
        self._sync_current_character_map()
        self._load_series_manifest()

    def split_selected_episode_speaker(self):
        selected_speakers = [
            str(value)
            for checkbox, (kind, value) in self.sidebar_checks.items()
            if checkbox.isChecked() and kind == "speaker"
        ]
        if self.sidebar_scope != "episode" or len(selected_speakers) != 1:
            QMessageBox.information(
                self, "拆分角色", "请切换到“本集”并只选择一个说话人。"
            )
            return
        try:
            from videotrans.process.series_speakers import detach_episode_speaker
            detach_episode_speaker(
                self.series_manifest,
                self.current_episode_key,
                selected_speakers[0],
            )
        except KeyError as error:
            QMessageBox.warning(self, "无法拆分", str(error))
            return
        self._save_series_manifest()
        self._sync_current_character_map()
        self._load_series_manifest()

    def _sync_current_character_map(self):
        episode = self.series_manifest.get('episodes', {}).get(
            self.current_episode_key, {}
        )
        self.current_character_map = {
            str(speaker): str(data.get('character_id', ''))
            for speaker, data in episode.get('speakers', {}).items()
            if data.get('character_id')
        }

    def _load_series_manifest(self):
        if self.series_manifest_path:
            from videotrans.process.series_speakers import load_manifest
            self.series_manifest = load_manifest(
                self.series_manifest_path,
                series_folder=self.series_folder,
                series_videos=self.series_video_paths,
            )
        self._sync_current_character_map()
        characters = self._character_index()
        from videotrans.process.series_speakers import character_voice
        for speaker, character_id in self.current_character_map.items():
            character = characters.get(character_id, {})
            inherited_voice = character_voice(character, self.voice_scope)
            if inherited_voice and speaker in self.speakers:
                self.speakers[speaker] = inherited_voice

        total_videos = len(self.series_video_paths)
        processed = len(self.series_manifest.get('episodes', {}))
        character_count = len(self.series_manifest.get('characters', []))
        if self.has_series_scope:
            total_text = str(total_videos) if total_videos else str(processed)
            self.series_status_label.setText(
                f"已跨集识别 {processed}/{total_text} 集 · {character_count} 个角色"
            )
            self.series_scope_button.setText(f"全片 {character_count}")
        else:
            self.series_status_label.setText(
                f"已识别当前视频 · {len(self.speakers)} 个角色"
            )
        self.episode_scope_button.setText(
            f"{'本集' if self.has_series_scope else '当前视频'} {len(self.speakers)}"
        )
        self._refresh_sidebar()
        self._refresh_table_speaker_names()

    def _save_series_manifest(self):
        if not self.series_manifest_path:
            return
        from videotrans.process.series_speakers import save_manifest
        save_manifest(self.series_manifest_path, self.series_manifest)

    def _speaker_display_name(self, speaker):
        character = self._character_for_speaker(speaker)
        if not character:
            return f"未识别 · {speaker}"
        from videotrans.process.series_speakers import character_display_name
        return character_display_name(character)

    def _refresh_table_speaker_names(self):
        if not hasattr(self, 'table'):
            return
        for row, data in enumerate(getattr(self, 'display_data', [])):
            display_name = self._speaker_display_name(data.get('spk', ''))
            item = self.table.item(row, 2)
            if item:
                # 该列使用 cellWidget 按钮展示；底层 item 只负责单元格
                # 状态。保留文字会透过按钮产生“双层文字”重影。
                item.setText("")
            button = getattr(self, 'row_character_buttons', {}).get(row)
            if button:
                button.setText(f"{display_name}  ▾")

    def _character_picker_options(self, scope):
        characters = self._character_index()
        counts = Counter(self.speaker_list_sub)
        if scope == "episode":
            totals = Counter()
            for speaker, character_id in self.current_character_map.items():
                totals[character_id] += counts.get(speaker, 0)
            total_lines = sum(totals.values()) or 1
            character_ids = list(totals)
        else:
            totals = Counter({
                character_id: sum(
                    int(episode.get("line_count", 0))
                    for episode in character.get("episodes", {}).values()
                )
                for character_id, character in characters.items()
            })
            total_lines = sum(totals.values()) or 1
            character_ids = list(characters)
        from videotrans.process.series_speakers import character_display_name
        options = [
            {
                "character_id": character_id,
                "name": character_display_name(characters[character_id]),
                "percent": round(totals.get(character_id, 0) * 100 / total_lines),
            }
            for character_id in character_ids
            if character_id in characters
        ]
        return sorted(
            options,
            key=lambda option: (
                -totals.get(option["character_id"], 0),
                option["name"],
            ),
        )

    def _open_row_character_picker(self, row):
        if row >= len(self.display_data):
            return
        speaker = str(self.display_data[row].get("spk", ""))
        current_id = self.current_character_map.get(speaker, "")
        button = getattr(self, 'row_character_buttons', {}).get(row)
        picker = CharacterPickerDialog(
            parent=self,
            episode_options=self._character_picker_options("episode"),
            series_options=(
                self._character_picker_options("series")
                if self.has_series_scope else []
            ),
            current_id=current_id,
            show_series_scope=self.has_series_scope,
        )
        if button:
            picker.place_below(button)
        if picker.exec() == QDialog.Accepted and picker.selected_character_id:
            self._assign_row_character(row, picker.selected_character_id)

    def _speaker_for_character(self, character_id):
        character_id = str(character_id)
        for speaker, mapped_id in self.current_character_map.items():
            if mapped_id == character_id:
                return speaker

        base = f"manual_{character_id}"
        speaker = base
        suffix = 2
        while speaker in self.speakers:
            speaker = f"{base}_{suffix}"
            suffix += 1
        character = self._character_index().get(character_id, {})
        from videotrans.process.series_speakers import character_voice
        self.speakers[speaker] = character_voice(character, self.voice_scope) or None
        self.current_character_map[speaker] = character_id
        episode = self.series_manifest.setdefault("episodes", {}).setdefault(
            self.current_episode_key,
            {"name": Path(self.video_path).name if self.video_path else self.current_episode_key,
             "speakers": {}},
        )
        episode.setdefault("speakers", {})[speaker] = {
            "character_id": character_id,
            "similarity": None,
            "match_state": "manual_assign",
            "line_count": 0,
            "embedding": [],
            "sample_count": 0,
        }
        character.setdefault("episodes", {})[self.current_episode_key] = {
            "episode_name": episode.get("name", self.current_episode_key),
            "speaker": speaker,
            "line_count": 0,
        }
        return speaker

    def _sync_episode_line_counts(self):
        counts = Counter(self.speaker_list_sub)
        episode = self.series_manifest.get("episodes", {}).get(
            self.current_episode_key, {}
        )
        characters = self._character_index()
        for speaker, data in episode.get("speakers", {}).items():
            line_count = int(counts.get(speaker, 0))
            data["line_count"] = line_count
            character = characters.get(str(data.get("character_id", "")))
            if character:
                character_episode = character.setdefault("episodes", {}).get(
                    self.current_episode_key
                )
                if character_episode is not None:
                    character_episode["line_count"] = line_count

    def _assign_row_character(self, row, character_id):
        if row < 0 or row >= len(self.display_data):
            return
        new_speaker = self._speaker_for_character(character_id)
        self.display_data[row]["spk"] = new_speaker
        self.display_data[row]["role"] = ""
        if row < len(self.speaker_list_sub):
            self.speaker_list_sub[row] = new_speaker
        self._sync_episode_line_counts()
        self._update_role_column()


    def load_table(self):
        """极致性能加载表格"""
        if not self.isVisible() or hasattr(self, 'table'):
            return

        try:
            # 1. 创建 QTableWidget（比 Model/View 快得多）
            self.table = QTableWidget()
            self.row_character_buttons = {}
            
            # 2. 【极致性能配置】禁用所有非必要功能
            self.table.setColumnCount(5)
            self.table.setHorizontalHeaderLabels([
                "选择", "行号 / 时间", "角色", "原文", "译文（可编辑）"
            ])
            
            # 禁用所有视觉效果
            self.table.setAlternatingRowColors(False)
            self.table.setShowGrid(False)  # 不显示网格线
            
            # 禁用选择
            self.table.setSelectionMode(QAbstractItemView.NoSelection)
            self.table.setSelectionBehavior(QAbstractItemView.SelectItems)
            
            # 禁用焦点
            self.table.setFocusPolicy(Qt.NoFocus)
            
            # 固定行高，避免动态计算
            self.table.verticalHeader().setDefaultSectionSize(52)
            self.table.verticalHeader().setVisible(False)
            
            # 列宽设置
            header = self.table.horizontalHeader()
            header.setSectionResizeMode(0, QHeaderView.Fixed)  # Sel
            header.setSectionResizeMode(1, QHeaderView.Fixed)  # ID
            header.setSectionResizeMode(2, QHeaderView.Fixed)  # Spk
            header.setSectionResizeMode(3, QHeaderView.Stretch)  # Source
            header.setSectionResizeMode(4, QHeaderView.Stretch)  # Target
            
            self.table.setColumnWidth(0, 30)
            self.table.setColumnWidth(1, 175)
            self.table.setColumnWidth(2, 120)
            
            # 最小样式
            self.table.setStyleSheet("""
                QTableWidget {
                    color: #eeeeee;
                    border: none;
                }
                QHeaderView::section {
                    background-color: #2b2b2b;
                    color: white;
                    padding: 2px;
                    border: none;
                    border-right: 1px solid #3e3e3e;
                }
                QTableWidget::item {
                    padding: 2px;
                }
            """)
            
            # 3. 预计算所有显示数据
            speaker_keys = list(self.speakers.keys()) if self.speakers else []
            default_spk = speaker_keys[0] if speaker_keys else ''
            
            self.display_data = []
            for i, item in enumerate(self.srt_list_dict):
                # Speaker ID
                if self.speakers and i < len(self.speaker_list_sub):
                    spk = self.speaker_list_sub[i]
                else:
                    spk = default_spk if self.speakers else ''
                
                # 时间字符串
                duration = (item['end_time'] - item['start_time']) / 1000.0
                time_str = f"{item['startraw']}->{item['endraw']}({duration:.1f}s)"
                
                self.display_data.append({
                    'line': item['line'],
                    'spk': spk,
                    'time_str': time_str,
                    'text': item['text'],
                    'source_text': (
                        self.source_srt_list[i]['text']
                        if i < len(self.source_srt_list) else ''
                    ),
                    'startraw': item['startraw'],
                    'endraw': item['endraw'],
                    'start_time': item['start_time'],
                    'end_time': item['end_time'],
                    'checked': False,
                    'role': ''
                })
            
            # 4. 设置行数
            total_rows = len(self.display_data)
            self.table.setRowCount(total_rows)
            
            # 5. 【批量填充数据】一次性创建所有单元格
            self._batch_fill_table(0, min(total_rows, 100))  # 先填充前100行
            
            # 6. 添加到布局
            self.table_container_layout.addWidget(self.table)
            
            # 7. 添加底部按钮
            self._load_series_manifest()
            self._auto_assign_speaker_roles()
            self._setup_bottom_buttons()
            
            # 8. 显示表格
            self.loading_widget.setVisible(False)
            self.table_container.setVisible(True)
            self.bottom_button_container.setVisible(True)
            
            # 9. 延迟加载剩余数据
            if total_rows > 100:
                QTimer.singleShot(0, lambda: self._load_remaining_rows(100))
            
            # 10. 启动倒计时
            if self.countdown_enabled:
                self.timer = QTimer(self)
                self.timer.timeout.connect(self.update_countdown)
                self.timer.start(1000)
            self._active()
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.loading_label.setText(f"Error: {e}")

    def _batch_fill_table(self, start_row, end_row):
        """批量填充表格数据 - 减少重绘"""
        for row in range(start_row, end_row):
            data = self.display_data[row]
            
            # 第0列：复选框
            chk_item = QTableWidgetItem()
            chk_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            chk_item.setCheckState(Qt.Unchecked)
            self.table.setItem(row, 0, chk_item)
            
            # 第1列：行号与时间（只读）
            id_item = QTableWidgetItem(f"{data['line']}\n{data['time_str']}")
            id_item.setFlags(Qt.ItemIsEnabled)  # 只读
            self.table.setItem(row, 1, id_item)
            
            # 第2列：角色（点击可按“本集 / 全片”直接切换）
            display_name = self._speaker_display_name(data['spk'])
            spk_item = QTableWidgetItem("")
            spk_item.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(row, 2, spk_item)
            character_button = QPushButton(f"{display_name}  ▾")
            character_button.setObjectName("rowCharacterButton")
            character_button.setCursor(Qt.PointingHandCursor)
            character_button.setToolTip("点击切换该句角色")
            character_button.setStyleSheet("""
                QPushButton {
                    background:#101e2c;border:none;color:#e8edf2;
                    padding:4px 8px;text-align:left;
                }
                QPushButton:hover { background:#24384b;color:#9fc9ff; }
            """)
            character_button.clicked.connect(
                lambda checked=False, current_row=row:
                    self._open_row_character_picker(current_row)
            )
            self.table.setCellWidget(row, 2, character_button)
            self.row_character_buttons[row] = character_button
            
            # 第3列：原文（只读）
            source_item = QTableWidgetItem(data.get('source_text', ''))
            source_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            source_item.setForeground(QColor("#9fb0c1"))
            self.table.setItem(row, 3, source_item)

            # 第4列：译文（可编辑）
            text_item = QTableWidgetItem(data['text'])
            text_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsEditable | Qt.ItemIsSelectable)
            text_item.setBackground(QColor("#172635"))
            self.table.setItem(row, 4, text_item)

    def _load_remaining_rows(self, start_row):
        """延迟加载剩余行 - 避免界面冻结"""
        total = len(self.display_data)
        batch_size = 200  # 每批加载200行
        
        end_row = min(start_row + batch_size, total)
        self._batch_fill_table(start_row, end_row)
        
        if end_row < total:
            # 还有数据，继续加载
            QTimer.singleShot(0, lambda: self._load_remaining_rows(end_row))

    def _setup_bottom_buttons(self):
        """设置底部按钮区域"""
        selected_label = QLabel("选中字幕音色")
        selected_label.setStyleSheet("color:#9fb2c5")
        self.bottom_button_container_layout.addWidget(selected_label)
        self.subtitle_combo = QComboBox()
        self._fill_role_combo(self.subtitle_combo)
        from videotrans.component.voice_selector import install_voice_selector
        install_voice_selector(
            self.subtitle_combo,
            tts_type_getter=lambda: self.tts_type,
            language_getter=lambda: self.target_language,
        )
        self.bottom_button_container_layout.addWidget(self.subtitle_combo)

        assign_button = QPushButton("应用到选中")
        assign_button.setCursor(Qt.PointingHandCursor)
        assign_button.clicked.connect(self.assign_subtitle_roles)
        assign_button.setMinimumSize(QSize(180, 28))
        self.bottom_button_container_layout.addWidget(assign_button)

        self.listen_button = QPushButton("试听")
        self.listen_button.setCursor(Qt.PointingHandCursor)
        self.listen_button.clicked.connect(self.listen_dubbing)
        self.bottom_button_container_layout.addWidget(self.listen_button)
        
        if self.speakers:
            change_label = QLabel("修改说话人")
            change_label.setStyleSheet("color:#9fb2c5;margin-left:12px")
            self.bottom_button_container_layout.addWidget(change_label)
            self.change_speaker_combo = QComboBox()
            for spk_id in self.speakers:
                self.change_speaker_combo.addItem(str(spk_id), spk_id)
            self.bottom_button_container_layout.addWidget(self.change_speaker_combo)
            change_button = QPushButton("应用到选中")
            change_button.clicked.connect(self.assign_selected_speaker)
            self.bottom_button_container_layout.addWidget(change_button)
        self.bottom_button_container_layout.addStretch()

    def _create_speaker_assignment_area(self):
        """创建说话人分配区域"""
        group = QGroupBox("")
        group.setStyleSheet("QGroupBox{border:none;}")
        layout = QVBoxLayout(group)
        label_tips = QLabel(tr("Assign a timbre to each speaker"))
        label_tips.setStyleSheet("color:#aaaaaa")
        layout.addWidget(label_tips)

        self.speaker_checks = {}
        self.speaker_labels = {}

        grid_layout = QGridLayout()
        grid_layout.setContentsMargins(0, 5, 0, 5)
        grid_layout.setHorizontalSpacing(15)
        grid_layout.setVerticalSpacing(5)

        for i, spk_id in enumerate(self.speakers):
            row = i // 3
            col = (i % 3) * 2

            check = QCheckBox(f'{tr("Speaker")}{spk_id}')
            check.setStyleSheet("color: #dddddd;")
            
            label = QLabel("")
            label.setMinimumWidth(80)
            label.setStyleSheet("color: #ffcccc;")

            grid_layout.addWidget(check, row, col)
            grid_layout.addWidget(label, row, col + 1)

            self.speaker_checks[check] = spk_id
            self.speaker_labels[check] = label

        layout.addLayout(grid_layout)

        bottom_row = QHBoxLayout()
        self.speaker_combo = QComboBox()
        self._fill_role_combo(self.speaker_combo)
        from videotrans.component.voice_selector import install_voice_selector
        install_voice_selector(
            self.speaker_combo,
            tts_type_getter=lambda: self.tts_type,
            language_getter=lambda: self.target_language,
        )
        
        lbl = QLabel(tr('Dubbing role'))
        lbl.setStyleSheet("color: #dddddd;")
        bottom_row.addWidget(lbl)
        bottom_row.addWidget(self.speaker_combo)

        assign_button = QPushButton("为说话人指定音色")
        assign_button.setCursor(Qt.PointingHandCursor)
        assign_button.clicked.connect(self.assign_speaker_roles)
        assign_button.setMinimumSize(QSize(140, 26))
        bottom_row.addWidget(assign_button)

        self.speaker_listen_button = QPushButton("试听")
        self.speaker_listen_button.setCursor(Qt.PointingHandCursor)
        self.speaker_listen_button.clicked.connect(self.listen_speaker_dubbing)
        self.speaker_listen_button.setMinimumSize(QSize(50, 26))
        bottom_row.addWidget(self.speaker_listen_button)
        bottom_row.addStretch()

        layout.addLayout(bottom_row)

        change_speaker_row = QHBoxLayout()
        change_speaker_label = QLabel("说话人")
        change_speaker_label.setStyleSheet("color: #dddddd;")
        change_speaker_row.addWidget(change_speaker_label)

        self.change_speaker_combo = QComboBox()
        for spk_id in self.speakers:
            self.change_speaker_combo.addItem(str(spk_id), spk_id)
        change_speaker_row.addWidget(self.change_speaker_combo)

        change_speaker_button = QPushButton("修改选中字幕说话人")
        change_speaker_button.setCursor(Qt.PointingHandCursor)
        change_speaker_button.clicked.connect(self.assign_selected_speaker)
        change_speaker_button.setMinimumSize(QSize(150, 26))
        change_speaker_row.addWidget(change_speaker_button)
        change_speaker_row.addStretch()

        layout.addLayout(change_speaker_row)
        return group

    def _load_voice_tags(self):
        if self.tts_type != 0:
            return {}
        tag_path = Path(f'{ROOT_DIR}/videotrans/voicejson/edge_voice_tags.json')
        try:
            return json.loads(tag_path.read_text(encoding='utf-8'))
        except Exception:
            return {}

    def _role_label(self, role: str) -> str:
        role = str(role or '').strip()
        if not role or role in {'No', '-'}:
            return role
        return self.voice_tags.get(role, {}).get('label', role)

    def _fill_role_combo(self, combo: QComboBox):
        for role in self.all_voices:
            combo.addItem(self._role_label(role), role)

    def _combo_role_value(self, combo: QComboBox) -> str:
        data = combo.currentData()
        if data is not None:
            return str(data)
        return combo.currentText()

    def _is_assignable_voice(self, voice: str) -> bool:
        voice = str(voice or '').strip()
        return bool(voice) and voice.lower() not in {'no', '-', 'clone'}

    def _auto_assign_speaker_roles(self):
        """Automatically map detected speakers to available dubbing roles."""
        if self._auto_assigned_speakers or not self.speakers:
            return

        voices = [voice for voice in self.all_voices if self._is_assignable_voice(voice)]
        if not voices:
            return

        mapping = {}
        recalculated = False
        report_path = Path(f'{self.cache_folder}/speaker_roles.json')
        if report_path.is_file():
            try:
                report = json.loads(report_path.read_text(encoding='utf-8'))
                if (
                    int(report.get('profile_version', 0)) >= 2
                    and
                    report.get('target_language_code') == self.target_language
                    and int(report.get('tts_type', -1)) == int(self.tts_type)
                ):
                    mapping = {
                        speaker: voice
                        for speaker, voice in report.get('speaker_to_voice', {}).items()
                        if voice in voices
                    }
            except Exception as e:
                logger.warning(f'读取自动说话人音色映射失败,重新计算:{e}')
        if not mapping:
            from videotrans.process.speaker_roles import (
                assign_speaker_voices,
                estimate_speaker_profiles,
            )

            profiles = estimate_speaker_profiles(
                audio_file=self.source_audio,
                subtitles=self.srt_list_dict,
                speakers=self.speaker_list_sub,
            )
            preferred_voices = {}
            locked_speakers = set()
            characters = self._character_index()
            from videotrans.process.series_speakers import character_voice
            for speaker, character_id in self.current_character_map.items():
                character = characters.get(character_id, {})
                voice = character_voice(character, self.voice_scope)
                if voice:
                    preferred_voices[speaker] = voice
                if character.get('voice_sources', {}).get(self.voice_scope) == 'manual':
                    locked_speakers.add(speaker)
            mapping, assignment = assign_speaker_voices(
                self.speaker_list_sub,
                voices,
                default_voice=self.default_role,
                speaker_profiles=profiles,
                preferred_voices=preferred_voices,
                locked_speakers=locked_speakers,
            )
            recalculated = True
            try:
                report_payload = json.dumps({
                        'profile_version': 2,
                        'speaker_count': len(Counter(self.speaker_list_sub)),
                        'speaker_counts': dict(Counter(self.speaker_list_sub)),
                        'speaker_profiles': profiles,
                        'speaker_to_voice': mapping,
                        'line_role_count': len(self.speaker_list_sub),
                        'target_language_code': self.target_language,
                        'tts_type': self.tts_type,
                        'default_voice': self.default_role,
                        **assignment,
                    }, ensure_ascii=False, indent=2)
                report_path.write_text(report_payload, encoding='utf-8')
                Path(self.target_sub).parent.joinpath(
                    'speaker_roles.json'
                ).write_text(
                    report_payload,
                    encoding='utf-8',
                )
            except OSError as error:
                logger.warning(f'保存新版角色音色分析失败:{error}')
        for spk_id in self.speakers:
            self.speakers[spk_id] = mapping.get(spk_id)

        if recalculated:
            from videotrans.process.series_speakers import set_character_voice
            characters = self._character_index()
            for speaker, voice in mapping.items():
                character_id = self.current_character_map.get(speaker, '')
                character = characters.get(character_id)
                if not character or speaker in locked_speakers:
                    continue
                set_character_voice(
                    self.series_manifest,
                    character_id,
                    voice,
                    scope=self.voice_scope,
                    source='auto',
                )
            self._save_series_manifest()

        for check, spk_id in getattr(self, 'speaker_checks', {}).items():
            role = self.speakers.get(spk_id, '') or ''
            self.speaker_labels[check].setText(self._role_label(role) if role else '')

        self._auto_assigned_speakers = True
        self._update_role_column()

    def _get_effective_role(self, data):
        role = data.get('role', '')
        if not role and data.get('spk'):
            role = self.speakers.get(data['spk'], '')
        return role or ''

    def assign_speaker_roles(self):
        """分配角色给说话人"""
        selected_role = self._combo_role_value(self.speaker_combo)
        role_value = None if selected_role == "No" else selected_role
        selected = [
            (checkbox, key)
            for checkbox, key in self.sidebar_checks.items()
            if checkbox.isChecked()
        ]
        if not selected:
            return
        characters = self._character_index()
        from videotrans.process.series_speakers import set_character_voice
        for checkbox, (kind, value) in selected:
            if kind == "speaker":
                speaker = str(value)
                self.speakers[speaker] = role_value
                character_id = self.current_character_map.get(speaker, "")
                if character_id in characters:
                    set_character_voice(
                        self.series_manifest,
                        character_id,
                        role_value or "",
                        scope=self.voice_scope,
                    )
            else:
                character_id = str(value)
                if character_id in characters:
                    set_character_voice(
                        self.series_manifest,
                        character_id,
                        role_value or "",
                        scope=self.voice_scope,
                    )
                for speaker, mapped_id in self.current_character_map.items():
                    if mapped_id == character_id and speaker in self.speakers:
                        self.speakers[speaker] = role_value
            checkbox.setChecked(False)
        self._save_series_manifest()
        self._update_role_column()

    def assign_selected_speaker(self):
        """修改选中字幕的说话人"""
        if not hasattr(self, 'change_speaker_combo'):
            return

        new_spk = self.change_speaker_combo.currentData()
        if new_spk is None:
            new_spk = self.change_speaker_combo.currentText()
        new_spk = str(new_spk)
        if not new_spk:
            return

        for row in range(self.table.rowCount()):
            chk_item = self.table.item(row, 0)
            if not chk_item or chk_item.checkState() != Qt.Checked:
                continue

            self.display_data[row]['spk'] = new_spk
            self.display_data[row]['role'] = ''
            if row < len(self.speaker_list_sub):
                self.speaker_list_sub[row] = new_spk

            spk_item = self.table.item(row, 2)
            if spk_item:
                spk_item.setText(self._speaker_display_name(new_spk))
            chk_item.setCheckState(Qt.Unchecked)

        self._update_role_column()

    def _update_role_column(self):
        """更新 Role 列显示"""
        if hasattr(self, "episode_scope_button"):
            self.episode_scope_button.setText(
                f"{'本集' if self.has_series_scope else '当前视频'} "
                f"{len(self.speakers)}"
            )
        self._refresh_sidebar()
        self._refresh_table_speaker_names()

    def assign_subtitle_roles(self):
        """分配角色给选中的行"""
        selected_role = self._combo_role_value(self.subtitle_combo)
        role_value = None if selected_role == "No" else selected_role

        for row in range(self.table.rowCount()):
            chk_item = self.table.item(row, 0)
            if chk_item and chk_item.checkState() == Qt.Checked:
                self.display_data[row]['role'] = role_value
                chk_item.setCheckState(Qt.Unchecked)
        
        self._update_role_column()

    def replace_text(self):
        """替换文本"""
        search_text = self.search_input.text()
        replace_text = self.replace_input.text()

        if not search_text:
            return

        self.table.setUpdatesEnabled(False)  # 禁用更新，提升性能
        
        for row, data in enumerate(self.display_data):
            if search_text in data['text']:
                new_text = data['text'].replace(search_text, replace_text)
                data['text'] = new_text
                item = self.table.item(row, 4)
                if item:
                    item.setText(new_text)
        
        self.table.setUpdatesEnabled(True)  # 恢复更新

    def _listen_combo_role(self, combo: QComboBox, button: QPushButton, reset_text: str):
        """试听指定下拉框当前选中的配音角色"""
        selected_role = self._combo_role_value(combo)
        role_value = None if selected_role == "No" else selected_role
        if not role_value:
            return

        first_text = self.display_data[0]['text'] if self.display_data else ''
        if not first_text:
            return

        from videotrans.util.ListenVoice import ListenVoice
        
        def feed(d):
            button.setText(reset_text)
            button.setDisabled(False)
            if d != "ok":
                tools.show_error(d)

        wk = ListenVoice(parent=self, queue_tts=[{
            "text": first_text,
            "role": role_value,
            "filename": config.TEMP_DIR + f"/{time.time()}-onlyone_setrole.wav",
            "tts_type": self.tts_type}],
            language=self.target_language,
            tts_type=self.tts_type)
        wk.uito.connect(feed)
        wk.start()
        button.setText('试听中...')
        button.setDisabled(True)

    def listen_dubbing(self):
        """试听底部字幕配音角色"""
        self._listen_combo_role(self.subtitle_combo, self.listen_button, "试听")

    def listen_speaker_dubbing(self):
        """试听说话人配音角色"""
        self._listen_combo_role(self.speaker_combo, self.speaker_listen_button, "试听")

    def _active(self):
        if self.parent:
            self.parent.activateWindow()

    def cancel_and_close(self):
        if hasattr(self, 'timer') and self.timer:
            self.timer.stop()
        self.reject()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            event.ignore()
        else:
            super().keyPressEvent(event)

    def update_countdown(self):
        self.count_down -= 1
        if self.prompt_label and hasattr(self.prompt_label, 'setText'):
            self.prompt_label.setText(f"将在 {self.count_down} 秒后自动继续")
        if self.count_down <= 0:
            self.timer.stop()
            self.save_and_close()

    def stop_countdown(self):
        if hasattr(self, 'timer'):
            self.timer.stop()
        self.stop_button.setDisabled(True)
        self.stop_button.setText("已停止倒计时")
        self.prompt_label.setText("你可以完成检查后手动继续")

    def save_and_close2(self):
        self.accept()

    def opendir_sub(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(Path(self.target_sub).parent.as_posix()))


    def closeEvent(self, event):
        event.ignore()  # 忽略关闭请求，窗口保持不动
    
    def save_and_close(self):
        self.save_button.setDisabled(True)
        app_cfg.line_roles = {}
        srt_str_list = []

        speaker_keys = list(self.speakers.keys()) if self.speakers else []
        for row, data in enumerate(self.display_data):
            # 获取当前文本（从表格中获取最新值）
            text_item = self.table.item(row, 4)
            text = text_item.text().strip() if text_item else data['text'].strip()
            
            srt_str_list.append(f'{data["line"]}\n{data["startraw"]} --> {data["endraw"]}\n{text}')

            # 角色保存逻辑
            role = data.get('role', '')
            if not role and self.speakers and data['spk']:
                role = self.speakers.get(data['spk'], '')

            if role:
                app_cfg.line_roles[str(data["line"])] = role

        try:
            Path(self.target_sub).write_text("\n\n".join(srt_str_list), encoding="utf-8")
            if self.cache_folder and self.speaker_list_sub:
                Path(f'{self.cache_folder}/speaker.json').write_text(
                    json.dumps(self.speaker_list_sub, ensure_ascii=False),
                    encoding="utf-8"
                )
                Path(f'{self.cache_folder}/line_roles.json').write_text(
                    json.dumps(app_cfg.line_roles, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
            self._sync_episode_line_counts()
            self._save_series_manifest()
        except Exception as e:
            logger.error(f"Save subtitle failed: {e}")
            QMessageBox.critical(self, "Error", f"Save failed: {e}")
            self.save_button.setDisabled(False)
            return

        self.accept()
