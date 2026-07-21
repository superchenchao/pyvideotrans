from __future__ import annotations

import os

from PySide6 import QtWidgets

from videotrans.configure.config import settings


def _number_setting(name, default, cast):
    try:
        return cast(settings.get(name, default))
    except (TypeError, ValueError):
        return cast(default)


class CloudSubtitleRemovalSettingsDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("云端字幕消除设置")
        self.resize(620, 520)
        layout = QtWidgets.QVBoxLayout(self)

        note = QtWidgets.QLabel(
            "云端方式使用同一份无音频 MP4：1080p、30 FPS、约 6000 kbps。\n"
            "凭据只从环境变量读取，不会写入任务缓存或日志。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QtWidgets.QFormLayout()
        self.region = QtWidgets.QComboBox()
        self.region.addItems(["cn-shanghai", "cn-beijing", "ap-southeast-1", "us-west-1"])
        self.region.setEditable(True)
        self.region.setCurrentText(str(settings.get("subtitle_oss_region", "cn-shanghai")))
        form.addRow("OSS / IMS 地域", self.region)

        self.bucket = QtWidgets.QLineEdit(str(settings.get("subtitle_oss_bucket", "")))
        form.addRow("OSS Bucket", self.bucket)

        self.endpoint = QtWidgets.QLineEdit(str(settings.get("subtitle_oss_endpoint", "")))
        self.endpoint.setPlaceholderText("例如 https://oss-cn-shanghai.aliyuncs.com")
        form.addRow("OSS 公网 Endpoint", self.endpoint)
        self._last_auto_endpoint = f"https://oss-{self.region.currentText()}.aliyuncs.com"
        self.region.currentTextChanged.connect(self._region_changed)

        self.prefix = QtWidgets.QLineEdit(
            str(settings.get("subtitle_oss_prefix", "pyvideotrans/subtitle-removal"))
        )
        form.addRow("OSS 对象前缀", self.prefix)

        self.signed_hours = QtWidgets.QDoubleSpinBox()
        self.signed_hours.setRange(0.5, 168)
        self.signed_hours.setValue(
            _number_setting("subtitle_oss_signed_url_hours", 12, float)
        )
        self.signed_hours.setSuffix(" 小时")
        form.addRow("Caca API 签名有效期", self.signed_hours)

        self.poll_seconds = QtWidgets.QSpinBox()
        self.poll_seconds.setRange(30, 300)
        self.poll_seconds.setValue(
            _number_setting("subtitle_cloud_poll_seconds", 30, int)
        )
        self.poll_seconds.setSuffix(" 秒")
        form.addRow("远端任务轮询间隔", self.poll_seconds)

        self.delete_after_download = QtWidgets.QCheckBox(
            "本地完整校验后删除 OSS 输入/输出临时文件"
        )
        self.delete_after_download.setChecked(
            bool(settings.get("subtitle_cloud_delete_after_download", True))
        )
        self.delete_after_download.setToolTip(
            "IMS 删除 OSS 输入和输出；Caca 只删除 OSS 输入，接口方结果不受控制"
        )
        form.addRow("自动清理 OSS", self.delete_after_download)

        self.caca_base_url = QtWidgets.QLineEdit(
            str(settings.get("subtitle_caca_base_url", ""))
        )
        form.addRow("Caca API Base URL", self.caca_base_url)

        self.caca_mode = QtWidgets.QComboBox()
        self.caca_mode.addItem("保护模式 protect", "protect")
        self.caca_mode.addItem("普通模式 normal", "normal")
        mode_index = self.caca_mode.findData(str(settings.get("subtitle_caca_mode", "protect")))
        self.caca_mode.setCurrentIndex(max(0, mode_index))
        form.addRow("Caca API 模式", self.caca_mode)

        self.caca_send_region = QtWidgets.QCheckBox(
            "发送归一化 x1/y1/x2/y2（仅在接口方确认坐标单位后启用）"
        )
        self.caca_send_region.setChecked(bool(settings.get("subtitle_caca_send_region", False)))
        form.addRow("Caca API 字幕区域", self.caca_send_region)

        self.ims_quality = QtWidgets.QComboBox()
        self.ims_quality.addItem("高级版（推荐）", "premium")
        self.ims_quality.addItem("普通版", "normal")
        quality_index = self.ims_quality.findData(
            str(settings.get("subtitle_ims_quality", "premium"))
        )
        self.ims_quality.setCurrentIndex(max(0, quality_index))
        form.addRow("阿里云 IMS 质量", self.ims_quality)
        layout.addLayout(form)

        oss_ok = bool(
            (os.getenv("OSS_ACCESS_KEY_ID") or os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID"))
            and (os.getenv("OSS_ACCESS_KEY_SECRET") or os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET"))
        )
        caca_ok = bool(
            os.getenv("PYVIDEOTRANS_CACA_SECRET_ID")
            and os.getenv("PYVIDEOTRANS_CACA_SECRET_KEY")
        )
        credential_status = QtWidgets.QLabel(
            "凭据状态："
            f"OSS/IMS {'已配置' if oss_ok else '未配置'}；"
            f"Caca API {'已配置' if caca_ok else '未配置'}。\n"
            "OSS/IMS：OSS_ACCESS_KEY_ID、OSS_ACCESS_KEY_SECRET；"
            "Caca API：PYVIDEOTRANS_CACA_SECRET_ID、PYVIDEOTRANS_CACA_SECRET_KEY。"
        )
        credential_status.setWordWrap(True)
        layout.addWidget(credential_status)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _region_changed(self, region):
        current = self.endpoint.text().strip()
        if not current or current == self._last_auto_endpoint:
            current = f"https://oss-{region.strip()}.aliyuncs.com" if region.strip() else ""
            self.endpoint.setText(current)
        self._last_auto_endpoint = (
            f"https://oss-{region.strip()}.aliyuncs.com" if region.strip() else ""
        )

    def save(self):
        region = self.region.currentText().strip()
        endpoint = self.endpoint.text().strip()
        if not endpoint and region:
            endpoint = f"https://oss-{region}.aliyuncs.com"
        settings["subtitle_oss_region"] = region
        settings["subtitle_oss_bucket"] = self.bucket.text().strip()
        settings["subtitle_oss_endpoint"] = endpoint
        settings["subtitle_oss_prefix"] = self.prefix.text().strip().strip("/")
        settings["subtitle_oss_signed_url_hours"] = self.signed_hours.value()
        settings["subtitle_cloud_poll_seconds"] = self.poll_seconds.value()
        settings["subtitle_cloud_delete_after_download"] = (
            self.delete_after_download.isChecked()
        )
        settings["subtitle_caca_base_url"] = self.caca_base_url.text().strip().rstrip("/")
        settings["subtitle_caca_mode"] = self.caca_mode.currentData()
        settings["subtitle_caca_send_region"] = self.caca_send_region.isChecked()
        settings["subtitle_ims_quality"] = self.ims_quality.currentData()
        settings.save()
        self.accept()


def open_cloud_subtitle_removal_settings(parent=None):
    dialog = CloudSubtitleRemovalSettingsDialog(parent)
    dialog.exec()
    return dialog
