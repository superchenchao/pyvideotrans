from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QPaintEvent
from PySide6.QtWidgets import QComboBox, QStyle, QStyleOptionComboBox, QStylePainter


class CheckableComboBox(QComboBox):
    """A compact multi-select combo while preserving a primary currentText."""

    checkedTextsChanged = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._primary_text = "-"
        self.view().viewport().installEventFilter(self)

    def addItem(self, text, userData=None):
        super().addItem(text, userData)
        item = self.model().item(self.count() - 1)
        if item:
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setData(
                Qt.Checked if text == "-" and self.count() == 1 else Qt.Unchecked,
                Qt.CheckStateRole,
            )

    def addItems(self, texts):
        for text in texts:
            self.addItem(text)

    def clear(self):
        super().clear()
        self._primary_text = "-"

    def checkedTexts(self):
        values = []
        for row in range(self.count()):
            item = self.model().item(row)
            if item and item.checkState() == Qt.Checked and item.text() != "-":
                values.append(item.text())
        return values

    def setCheckedTexts(self, texts):
        wanted = {str(text) for text in (texts or []) if text and text != "-"}
        first = None
        for row in range(self.count()):
            item = self.model().item(row)
            if not item:
                continue
            checked = item.text() in wanted or (not wanted and item.text() == "-")
            item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
            if checked and item.text() != "-" and first is None:
                first = (row, item.text())
        self._primary_text = first[1] if first else "-"
        if first:
            super().setCurrentIndex(first[0])
        elif self.count():
            super().setCurrentIndex(0)
        self.update()
        self.checkedTextsChanged.emit(self.checkedTexts())

    def setCurrentText(self, text):
        self.setCheckedTexts([] if text in (None, "", "-") else [text])

    def currentText(self):
        selected = self.checkedTexts()
        if self._primary_text in selected:
            return self._primary_text
        return selected[0] if selected else "-"

    def displayText(self):
        selected = self.checkedTexts()
        if not selected:
            return "-"
        if len(selected) <= 2:
            return "、".join(selected)
        return f"{selected[0]}、{selected[1]} +{len(selected) - 2}"

    def eventFilter(self, watched, event):
        if watched is self.view().viewport() and event.type() == QEvent.MouseButtonRelease:
            index = self.view().indexAt(event.position().toPoint())
            if not index.isValid():
                return True
            item = self.model().itemFromIndex(index)
            if not item:
                return True
            text = item.text()
            if text == "-":
                self.setCheckedTexts([])
                return True
            item.setCheckState(
                Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked
            )
            dash = self.model().item(0) if self.count() else None
            if dash and dash.text() == "-":
                dash.setCheckState(Qt.Unchecked)
            selected = self.checkedTexts()
            if item.checkState() == Qt.Checked:
                self._primary_text = text
                super().setCurrentIndex(index.row())
            elif self._primary_text == text:
                self._primary_text = selected[0] if selected else "-"
                target = self.findText(self._primary_text)
                if target >= 0:
                    super().setCurrentIndex(target)
            if not selected and dash:
                dash.setCheckState(Qt.Checked)
                self._primary_text = "-"
                super().setCurrentIndex(0)
            self.update()
            self.checkedTextsChanged.emit(selected)
            return True
        return super().eventFilter(watched, event)

    def paintEvent(self, event: QPaintEvent):
        painter = QStylePainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        option.currentText = self.displayText()
        painter.drawComplexControl(QStyle.CC_ComboBox, option)
        painter.drawControl(QStyle.CE_ComboBoxLabel, option)
