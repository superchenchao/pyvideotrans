import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from PySide6.QtCore import QEvent, QObject, QSize, Qt, Signal
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from videotrans.configure import config
from videotrans.configure.contants import LISTEN_TEXT
from videotrans.util.voice_catalog import (
    VoiceEntry,
    build_voice_entries,
    get_voice_favorites,
    set_voice_favorite,
)


class VoiceCard(QFrame):
    preview_requested = Signal(str)
    favorite_requested = Signal(str)
    use_requested = Signal(str)

    COLORS = ("#ff9f43", "#54a0ff", "#ff6baf", "#48dbcf", "#a29bfe")

    def __init__(
        self,
        entry: VoiceEntry,
        *,
        favorite: bool,
        selected: bool,
        parent=None,
    ):
        super().__init__(parent)
        self.entry = entry
        self.setObjectName("voiceCard")
        self.setMinimumHeight(70)
        self._set_selected(selected)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(10)

        self.preview_button = QPushButton("▶")
        self.preview_button.setObjectName("previewButton")
        self.preview_button.setFixedSize(38, 38)
        marker_color = self.COLORS[sum(ord(char) for char in entry.role) % len(self.COLORS)]
        self.preview_button.setStyleSheet(
            f"QPushButton{{border-radius:19px;background:{marker_color};color:white;"
            "font-size:15px;border:2px solid rgba(255,255,255,55);}}"
            "QPushButton:hover{border:2px solid white;}"
            "QPushButton:disabled{background:#46515c;color:#8a959f;}"
        )
        self.preview_button.setEnabled(entry.previewable)
        self.preview_button.setToolTip("试听" if entry.previewable else "该音色不支持直接试听")
        self.preview_button.clicked.connect(
            lambda: self.preview_requested.emit(self.entry.role)
        )
        layout.addWidget(self.preview_button)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)
        name_label = QLabel(entry.name)
        name_label.setObjectName("voiceName")
        name_label.setStyleSheet("font-weight:600;font-size:14px;color:#f2f5f7;")
        text_layout.addWidget(name_label)

        detail = entry.description
        if entry.voice_id and entry.voice_id != entry.role:
            detail = f"{detail} · {entry.voice_id}" if detail else entry.voice_id
        detail_label = QLabel(detail or entry.role)
        detail_label.setObjectName("voiceDescription")
        detail_label.setStyleSheet("color:#8f9ba6;font-size:11px;")
        detail_label.setToolTip(detail or entry.role)
        text_layout.addWidget(detail_label)
        layout.addLayout(text_layout, 1)

        tags = entry.tags[:4]
        if tags:
            tags_label = QLabel("  ".join(tags))
            tags_label.setObjectName("voiceTags")
            tags_label.setStyleSheet(
                "color:#b9c3cb;background:#26313a;border-radius:9px;"
                "padding:3px 7px;font-size:10px;"
            )
            layout.addWidget(tags_label)

        self.favorite_button = QPushButton()
        self.favorite_button.setObjectName("favoriteButton")
        self.favorite_button.setFixedSize(34, 34)
        self.favorite_button.clicked.connect(
            lambda: self.favorite_requested.emit(self.entry.role)
        )
        layout.addWidget(self.favorite_button)
        self.set_favorite(favorite)

        use_button = QPushButton("使用")
        use_button.setObjectName("useButton")
        use_button.setMinimumSize(52, 34)
        use_button.clicked.connect(lambda: self.use_requested.emit(self.entry.role))
        layout.addWidget(use_button)

    def _set_selected(self, selected: bool):
        border = "#2f9cf4" if selected else "#34414c"
        background = "#24333f" if selected else "#1b252d"
        self.setStyleSheet(
            f"QFrame#voiceCard{{background:{background};border:1px solid {border};"
            "border-radius:7px;}}"
            "QFrame#voiceCard:hover{background:#22313c;border-color:#4d6679;}"
            "QPushButton#favoriteButton,QPushButton#useButton{background:#26333d;"
            "border:1px solid #42515d;border-radius:6px;color:#e5ebef;}"
            "QPushButton#favoriteButton:hover,QPushButton#useButton:hover{"
            "background:#324451;border-color:#5c7486;}"
        )

    def set_favorite(self, favorite: bool):
        self.favorite_button.setText("★" if favorite else "☆")
        self.favorite_button.setToolTip("取消收藏" if favorite else "收藏")

    def set_previewing(self, previewing: bool):
        self.preview_button.setText("…" if previewing else "▶")
        self.preview_button.setEnabled(self.entry.previewable and not previewing)


class VoiceSelectorDialog(QDialog):
    PAGE_SIZE = 80

    def __init__(
        self,
        *,
        tts_type: int,
        language: str,
        roles: Iterable[str],
        current_role: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.tts_type = int(tts_type)
        self.language = str(language or "")
        self.current_role = str(current_role or "")
        self.selected_role: Optional[str] = None
        self.entries = build_voice_entries(self.tts_type, roles, self.language)
        self.favorites = get_voice_favorites(self.tts_type)
        self.filtered_entries = []
        self.visible_cards = {}
        self.shown_count = 0
        self._listen_thread = None
        self._preview_role = None
        self._favorites_only = False

        provider = self.entries[0].provider if self.entries else "当前渠道"
        self.setWindowTitle(f"声音选择 · {provider}")
        self.setModal(True)
        self.resize(920, 650)
        self.setMinimumSize(760, 520)
        self.setStyleSheet(self._dialog_style())

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        title_row = QHBoxLayout()
        title = QLabel("声音选择")
        title.setStyleSheet("font-size:18px;font-weight:700;color:#f4f7f9;")
        subtitle = QLabel(provider)
        subtitle.setStyleSheet("color:#82909b;margin-left:8px;")
        title_row.addWidget(title)
        title_row.addWidget(subtitle)
        title_row.addStretch()
        self.result_label = QLabel()
        self.result_label.setStyleSheet("color:#7f8d97;")
        title_row.addWidget(self.result_label)
        root.addLayout(title_row)

        tab_row = QHBoxLayout()
        self.public_button = QPushButton("公共音色")
        self.favorite_tab_button = QPushButton("收藏音色")
        for button in (self.public_button, self.favorite_tab_button):
            button.setCheckable(True)
            button.setMinimumSize(88, 32)
            tab_row.addWidget(button)
        self.public_button.setChecked(True)
        self.public_button.clicked.connect(lambda: self._set_favorites_only(False))
        self.favorite_tab_button.clicked.connect(lambda: self._set_favorites_only(True))
        tab_row.addStretch()
        root.addLayout(tab_row)

        filter_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜索名称、语言、地区或 Voice ID")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setMinimumHeight(34)
        filter_row.addWidget(self.search_input, 1)

        self.gender_filter = QComboBox()
        self.gender_filter.addItems(["全部性别", "男声", "女声", "中性", "未知"])
        self.gender_filter.setMinimumSize(110, 34)
        filter_row.addWidget(self.gender_filter)

        self.age_filter = QComboBox()
        ages = [entry.age for entry in self.entries if entry.age and entry.age != "未知"]
        self.age_filter.addItems(["全部年龄", *list(dict.fromkeys(ages)), "未知"])
        self.age_filter.setMinimumSize(110, 34)
        filter_row.addWidget(self.age_filter)
        root.addLayout(filter_row)

        self.voice_list = QListWidget()
        self.voice_list.setObjectName("voiceList")
        self.voice_list.setSpacing(5)
        self.voice_list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        root.addWidget(self.voice_list, 1)

        self.empty_label = QLabel("没有符合条件的音色")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setStyleSheet("color:#75838e;padding:24px;")
        self.empty_label.hide()
        root.addWidget(self.empty_label)

        bottom_row = QHBoxLayout()
        self.load_more_button = QPushButton("加载更多")
        self.load_more_button.clicked.connect(self._load_more)
        bottom_row.addWidget(self.load_more_button)
        bottom_row.addStretch()
        close_button = QPushButton("关闭")
        close_button.setMinimumSize(72, 32)
        close_button.clicked.connect(self.reject)
        bottom_row.addWidget(close_button)
        root.addLayout(bottom_row)

        self.search_input.textChanged.connect(self._apply_filters)
        self.gender_filter.currentTextChanged.connect(self._apply_filters)
        self.age_filter.currentTextChanged.connect(self._apply_filters)
        self._apply_filters()

    @staticmethod
    def _dialog_style() -> str:
        return """
            QDialog { background:#11181e; color:#e8edf0; }
            QLineEdit, QComboBox {
                background:#182128; border:1px solid #34424d; border-radius:6px;
                color:#e5ebef; padding:5px 8px;
            }
            QLineEdit:focus, QComboBox:focus { border-color:#2f9cf4; }
            QComboBox QAbstractItemView {
                background:#182128; color:#e5ebef; selection-background-color:#2d536e;
            }
            QListWidget#voiceList { background:transparent; border:none; outline:none; }
            QListWidget#voiceList::item { background:transparent; border:none; }
            QPushButton {
                background:#26333d; border:1px solid #42515d; border-radius:6px;
                color:#e5ebef; padding:5px 10px;
            }
            QPushButton:hover { background:#324451; border-color:#5c7486; }
            QPushButton:checked { background:#e8edf0; color:#162028; border-color:#e8edf0; }
            QPushButton:disabled { color:#64727d; background:#202a32; }
        """

    def _set_favorites_only(self, favorites_only: bool):
        self._favorites_only = favorites_only
        self.public_button.setChecked(not favorites_only)
        self.favorite_tab_button.setChecked(favorites_only)
        self._apply_filters()

    def _apply_filters(self):
        query = self.search_input.text().strip().lower()
        gender = self.gender_filter.currentText()
        age = self.age_filter.currentText()
        result = []
        for entry in self.entries:
            if self._favorites_only and entry.role not in self.favorites:
                continue
            if query and query not in entry.search_text:
                continue
            if gender != "全部性别" and entry.gender != gender:
                continue
            if age != "全部年龄" and entry.age != age:
                continue
            result.append(entry)
        self.filtered_entries = result
        self.shown_count = 0
        self.voice_list.clear()
        self.visible_cards.clear()
        self._load_more()

    def _load_more(self):
        end = min(self.shown_count + self.PAGE_SIZE, len(self.filtered_entries))
        for entry in self.filtered_entries[self.shown_count:end]:
            item = QListWidgetItem()
            item.setSizeHint(QSize(0, 76))
            card = VoiceCard(
                entry,
                favorite=entry.role in self.favorites,
                selected=entry.role == self.current_role,
            )
            card.preview_requested.connect(self._preview_voice)
            card.favorite_requested.connect(self._toggle_favorite)
            card.use_requested.connect(self._use_role)
            self.voice_list.addItem(item)
            self.voice_list.setItemWidget(item, card)
            self.visible_cards[entry.role] = card
        self.shown_count = end
        total = len(self.filtered_entries)
        self.result_label.setText(f"{total} 个音色")
        self.load_more_button.setVisible(self.shown_count < total)
        self.empty_label.setVisible(total == 0)
        self.voice_list.setVisible(total > 0)

    def _toggle_favorite(self, role: str):
        favorite = role not in self.favorites
        self.favorites = set_voice_favorite(self.tts_type, role, favorite)
        if self._favorites_only and not favorite:
            self._apply_filters()
            return
        card = self.visible_cards.get(role)
        if card:
            card.set_favorite(favorite)

    def _use_role(self, role: str):
        if self._listen_thread is not None and self._listen_thread.isRunning():
            QMessageBox.information(self, "试听中", "请等待当前试听生成完成后再选择。")
            return
        self.selected_role = role
        self.accept()

    def _preview_voice(self, role: str):
        if self._listen_thread is not None and self._listen_thread.isRunning():
            return
        entry = next((item for item in self.entries if item.role == role), None)
        if not entry or not entry.previewable:
            return
        language_key = self.language.lower()
        text = LISTEN_TEXT.get(language_key) or LISTEN_TEXT.get(language_key.split("-")[0])
        if not text:
            QMessageBox.information(self, "无法试听", "当前语言暂时没有试听文本。")
            return

        from videotrans.util.ListenVoice import ListenVoice

        Path(config.TEMP_DIR).mkdir(parents=True, exist_ok=True)
        filename = str(Path(config.TEMP_DIR) / f"voice-selector-{time.time()}.wav")
        queue_tts = [
            {
                "text": text,
                "role": role,
                "filename": filename,
                "tts_type": self.tts_type,
                "rate": "+0%",
                "volume": "+0%",
                "pitch": "+0Hz",
            }
        ]
        card = self.visible_cards.get(role)
        if card:
            card.set_previewing(True)
        self._preview_role = role

        def feed(message: str):
            active_card = self.visible_cards.get(self._preview_role)
            if active_card:
                active_card.set_previewing(False)
            self._preview_role = None
            if message != "ok":
                QMessageBox.critical(self, "试听失败", message)

        self._listen_thread = ListenVoice(
            parent=self,
            queue_tts=queue_tts,
            language=self.language,
            tts_type=self.tts_type,
        )
        self._listen_thread.uito.connect(feed)
        self._listen_thread.start()

    def reject(self):
        if self._listen_thread is not None and self._listen_thread.isRunning():
            QMessageBox.information(self, "试听中", "请等待当前试听生成完成。")
            return
        super().reject()


class VoiceSelectorEventFilter(QObject):
    def __init__(
        self,
        combo: QComboBox,
        *,
        tts_type_getter: Callable[[], int],
        language_getter: Callable[[], str],
    ):
        super().__init__(combo)
        self.combo = combo
        self.tts_type_getter = tts_type_getter
        self.language_getter = language_getter
        self._opening = False

    def eventFilter(self, watched, event):
        if watched is self.combo and event.type() == QEvent.Wheel:
            return True
        should_open = False
        if isinstance(event, QMouseEvent) and event.type() == QEvent.MouseButtonPress:
            should_open = event.button() == Qt.LeftButton
        elif isinstance(event, QKeyEvent) and event.type() == QEvent.KeyPress:
            should_open = event.key() in (
                Qt.Key_Return,
                Qt.Key_Enter,
                Qt.Key_Space,
                Qt.Key_Up,
                Qt.Key_Down,
            )
        if watched is self.combo and should_open and self.combo.isEnabled():
            self.open_selector()
            return True
        return super().eventFilter(watched, event)

    def _combo_roles(self) -> list[str]:
        roles = []
        for index in range(self.combo.count()):
            data = self.combo.itemData(index)
            roles.append(str(data) if isinstance(data, str) and data else self.combo.itemText(index))
        return roles

    def _current_role(self) -> str:
        data = self.combo.currentData()
        return str(data) if isinstance(data, str) and data else self.combo.currentText()

    def _select_role(self, role: str):
        for index in range(self.combo.count()):
            data = self.combo.itemData(index)
            value = str(data) if isinstance(data, str) and data else self.combo.itemText(index)
            if value == role:
                self.combo.setCurrentIndex(index)
                return
        self.combo.addItem(role, role)
        self.combo.setCurrentIndex(self.combo.count() - 1)

    def open_selector(self):
        if self._opening:
            return
        self._opening = True
        try:
            dialog = VoiceSelectorDialog(
                parent=self.combo.window(),
                tts_type=int(self.tts_type_getter()),
                language=str(self.language_getter() or ""),
                roles=self._combo_roles(),
                current_role=self._current_role(),
            )
            if dialog.exec() == QDialog.Accepted and dialog.selected_role is not None:
                self._select_role(dialog.selected_role)
        finally:
            self._opening = False


def install_voice_selector(
    combo: QComboBox,
    *,
    tts_type_getter: Callable[[], int],
    language_getter: Callable[[], str],
) -> VoiceSelectorEventFilter:
    existing = getattr(combo, "_voice_selector_filter", None)
    if existing is not None:
        return existing
    event_filter = VoiceSelectorEventFilter(
        combo,
        tts_type_getter=tts_type_getter,
        language_getter=language_getter,
    )
    combo.installEventFilter(event_filter)
    combo.setCursor(Qt.PointingHandCursor)
    combo.setToolTip("点击打开声音选择器")
    combo._voice_selector_filter = event_filter
    return event_filter
