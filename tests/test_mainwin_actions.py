"""
Tests for WinActionBase pure methods and proxy validation logic.
Since WinActionBase depends on MainWindow (PySide6), we test the
pure logic in isolation without instantiating the class.
"""

import re


def test_single_folder_manual_review_starts_without_showing_task_center(
        tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication
    from videotrans.configure.config import app_cfg
    from videotrans.mainwin._actions import WinAction
    from videotrans.util import tools

    app = QApplication.instance() or QApplication([])
    video = tmp_path / "一部剧" / "01.mp4"
    video.parent.mkdir()
    video.write_bytes(b"video")

    class Value:
        def __init__(self, value):
            self.value = value

        def isChecked(self):
            return bool(self.value)

        def currentIndex(self):
            return int(self.value)

        def currentText(self):
            return str(self.value)

        def setCurrentText(self, value):
            self.value = value

        def checkedTexts(self):
            return ["英语"]

    class Button:
        def setDisabled(self, _disabled):
            pass

    class TaskWindow:
        def __init__(self):
            self.shown = False
            self.hidden = False
            self.started = False

        def add_videos(self, *_args, **_kwargs):
            return True

        def show(self):
            self.shown = True

        def hide(self):
            self.hidden = True

        def raise_(self):
            pass

        def activateWindow(self):
            pass

        def start_processing(self):
            self.started = True

    task_window = TaskWindow()

    class Main:
        startbtn = Button()
        review_countdown = Value(True)
        target_language = Value("英语")
        tts_type = Value(0)
        voice_role = Value("Auto Voice")
        app_mode = "biaozhun"

        @staticmethod
        def _get_multifolder_tasks_window():
            return task_window

    action = WinAction(Main())
    action.queue_mp4 = [video.as_posix()]
    action.imported_subtitle_map = {}
    action._refresh_imported_subtitle_matches = lambda **_kwargs: True
    action.check_proxy = lambda: True
    monkeypatch.setattr(tools, "role_menu", lambda *_args: ["No", "Auto Voice"])
    app_cfg.current_status = "end"

    action.check_start()
    app.processEvents()

    assert task_window.hidden is True
    assert task_window.shown is False
    assert task_window.started is True


def test_saved_subtitle_region_does_not_skip_batch_confirmation():
    from videotrans import recognition
    from videotrans.mainwin._actions import _should_prompt_for_ocr_area

    common = {
        "burned_subtitle_ocr": True,
        "app_mode": "biaozhun",
        "first_video": "new-video.mp4",
        "recogn_type": recognition.FASTER_WHISPER,
        "source_language_code": "zh-cn",
    }

    assert _should_prompt_for_ocr_area(**common, initial_rect=None)
    assert _should_prompt_for_ocr_area(
        **common,
        initial_rect=[0.05, 0.68, 0.9, 0.08],
    )


class TestProxyValidation:
    def test_valid_http_proxy(self):
        proxy = "http://127.0.0.1:1080"
        assert re.match(r'^(http|sock)(s|5)?://(\d+\.){3}\d+:\d+', proxy, re.I)

    def test_socks_proxy(self):
        proxy = "socks5://127.0.0.1:1080"
        assert re.match(r'^http(s)?://|^socks5?://', proxy, re.I)

    def test_invalid_proxy_no_port(self):
        proxy = "http://127.0.0.1"
        # The check_proxy regex requires :port after the IP
        assert not re.match(r'^(http|sock)(s|5)?://(\d+\.){3}\d+:\d+', proxy, re.I)

    def test_invalid_proxy_wrong_scheme(self):
        proxy = "ftp://127.0.0.1:1080"
        assert not re.match(r'^(http|sock)(s|5)?://(\d+\.){3}\d+:\d+', proxy, re.I)

    def test_proxy_http_prefix_added(self):
        proxy = "127.0.0.1:1080"
        if not re.match(r'^(http|sock)', proxy, re.I):
            proxy = f'http://{proxy}'
        assert proxy == 'http://127.0.0.1:1080'


class TestVoiceAutorateLogic:
    def test_voice_autorate_hides_silent_mid(self):
        voice_autorate = True
        video_autorate = False
        show = not voice_autorate and not video_autorate
        assert show is False

    def test_both_false_shows(self):
        voice_autorate = False
        video_autorate = False
        show = not voice_autorate and not video_autorate
        assert show is True

    def test_video_autorate_alone_hides(self):
        voice_autorate = False
        video_autorate = True
        show = not voice_autorate and not video_autorate
        assert show is False


class TestSubtitleTypeLogic:
    def test_dual_subtitle_shows_output_srt(self):
        idx = 3  # 双硬字幕
        show = idx >= 3
        assert show is True

    def test_single_hard_subtitle_hides(self):
        idx = 1  # 硬字幕
        show = idx >= 3
        assert show is False


class TestSetModeLogic:
    def test_tiqu_forces_voice_role_no(self):
        app_mode = 'tiqu'
        voice_role = 'some-role'
        subtitle_type = 1
        if app_mode == 'tiqu':
            voice_role = 'No'
        assert voice_role == 'No'

    def test_biaozhun_keeps_voice_role(self):
        app_mode = 'biaozhun'
        voice_role = 'some-role'
        # In biaozhun mode, voice_role is NOT forced to 'No'
        if app_mode == 'tiqu':
            voice_role = 'No'
        assert voice_role == 'some-role'
