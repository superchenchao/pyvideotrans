import copy
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Union

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QFileDialog

from videotrans import translator, recognition, tts
from videotrans.component.progressbar import ClickableProgressBar
from videotrans.configure import contants
from videotrans.configure.config import ROOT_DIR, tr, params, settings, app_cfg
from videotrans.mainwin._actions_base import WinActionBase
from videotrans.task.taskcfg import InputFile, SignMsg
from videotrans.util import tools
from videotrans.util.subtitle_import import (
    match_subtitles_to_videos,
    read_timed_subtitle,
)


def _should_prompt_for_ocr_area(
        *, burned_subtitle_ocr, app_mode, first_video, recogn_type,
        source_language_code, initial_rect=None):
    """Require confirmation for every eligible batch; saved ROI only pre-fills."""
    return bool(
        burned_subtitle_ocr
        and app_mode != 'tiqu'
        and first_video
        and recogn_type == recognition.FASTER_WHISPER
        and str(source_language_code).lower().startswith('zh')
    )


@dataclass
class WinAction(WinActionBase):

    imported_subtitle_files: List[str] = field(default_factory=list, init=False)
    imported_subtitle_map: Dict[str, str] = field(default_factory=dict, init=False)

    def _reset(self):
        # 存放需要处理的视频dict信息，包括uuid
        self.obj_list = []
        self.main.source_mp4.setText(tr("No select videos"))
        self.imported_subtitle_files = []
        self.imported_subtitle_map = {}
        self._set_import_subtitle_status()

    # 删除进度按钮
    def delete_process(self):
        for i in range(self.main.processlayout.count()):
            item = self.main.processlayout.itemAt(i)
            if item.widget():
                try:
                    item.widget().deleteLater()
                except Exception:
                    pass
        self.processbtns = {}

    # 将倒计时设为立即超时
    def set_djs_timeout(self):
        app_cfg.set_countdown(-1)
        if self.had_click_btn:
            return
        self.had_click_btn = True
        self.main.subtitle_area.setReadOnly(True)
        self.had_click_btn = False

    # 翻译渠道变化时，检测条件
    def set_translate_type(self, idx):
        try:
            t = self.main.target_language.currentText()
            if t not in ['-']:
                rs = translator.is_allow_translate(translate_type=idx, show_target=t)
                if rs is not True:
                    return False
        except Exception as e:
            tools.show_error(str(e))

    def set_subtitle_type(self, idx):
        if idx < 3:
            self.main.output_srt.hide()
        else:
            self.main.output_srt.setCurrentIndex(2)
            self.main.output_srt.show()

    def show_xxl_select(self):
        import sys
        if sys.platform != 'win32':
            tools.show_error(
                tr("faster-whisper-xxl.exe is only available on Windows"))
            return False
        xxl_path = settings.get('Faster_Whisper_XXL', '')
        if not xxl_path or not Path(xxl_path).exists():
            from videotrans.component.set_xxl import SetFasterXXL
            dialog = SetFasterXXL()
            if dialog.exec():  # OK 按钮被点击时 exec 返回 True
                xxl_path = dialog.get_values()
                if xxl_path and Path(xxl_path).is_file():
                    return True
            tools.show_error(
                tr("Must be selected, otherwise it cannot be used"))
            return False
        return True

    def show_cpp_select(self):
        cpp_path = settings.get('Whisper_cpp', '')
        if not cpp_path or not Path(cpp_path).exists():
            from videotrans.component.set_cpp import SetWhisperCPP
            dialog = SetWhisperCPP()
            if dialog.exec():  # OK 按钮被点击时 exec 返回 True
                cpp_path = dialog.get_values()
                if cpp_path and Path(cpp_path).is_file():
                    return True
            tools.show_error(
                tr("Must be selected, otherwise it cannot be used"))
            return False
        return True

    # 语音识别方式改变时
    def recogn_type_change(self):
        recogn_type = self.main.recogn_type.currentIndex()
        if recogn_type == recognition.Faster_Whisper_XXL and not self.show_xxl_select():
            return
        if recogn_type == recognition.Whisper_CPP and not self.show_cpp_select():
            return

        if recogn_type not in [recognition.FASTER_WHISPER, recognition.OPENAI_WHISPER, recognition.Faster_Whisper_XXL,
                               recognition.FUNASR_CN, recognition.Deepgram, recognition.Whisper_CPP,
                               recognition.WHISPERX_API, recognition.HUGGINGFACE_ASR, recognition.QWENASR,
                               recognition.WHISPER_NET]:

            # 禁止模块选择
            self.main.model_name.setDisabled(True)
            self.main.model_name_help.setDisabled(True)
        else:
            # 允许模块选择
            self.main.model_name_help.setDisabled(False)
            self.main.model_name.setDisabled(False)
            self.main.model_name.clear()
            if recogn_type in [recognition.FASTER_WHISPER, recognition.OPENAI_WHISPER, recognition.Faster_Whisper_XXL,
                               recognition.WHISPERX_API]:
                self.main.model_name.addItems(
                    settings.WHISPER_MODEL_LIST if recogn_type != recognition.OPENAI_WHISPER else contants.Openai_Whisper_Models.split(','))
            elif recogn_type == recognition.Deepgram:
                self.main.model_name.addItems(contants.DEEPGRAM_MODEL)
            elif recogn_type == recognition.Whisper_CPP:
                self.main.model_name.addItems(settings.Whisper_CPP_MODEL_LIST)
            elif recogn_type == recognition.WHISPER_NET:
                self.main.model_name.addItems(settings.Whisper_NET_MODEL_LIST)

            elif recogn_type == recognition.QWENASR:
                self.main.model_name.addItems(['1.7B', '0.6B'])
            elif recogn_type == recognition.HUGGINGFACE_ASR:
                self.main.model_name.addItems(list(recognition.HUGGINGFACE_ASR_MODELS.keys()))
            else:
                self.main.model_name.addItems(contants.FUNASR_MODEL)

        lang = translator.get_code(show_text=self.main.source_language.currentText())

        is_allow_lang = recognition.is_allow_lang(langcode=lang, recogn_type=recogn_type,
                                                  model_name=self.main.model_name.currentText())
        if is_allow_lang is not True:
            self.main.show_tips.setText(is_allow_lang)
        else:
            self.main.show_tips.setText('')

        if recognition.is_input_api(recogn_type=recogn_type) is not True:
            return

    def model_type_change(self):
        lang = translator.get_code(show_text=self.main.source_language.currentText())
        recogn_type = self.main.recogn_type.currentIndex()
        is_allow_lang = recognition.is_allow_lang(langcode=lang, recogn_type=recogn_type,
                                                  model_name=self.main.model_name.currentText())
        if is_allow_lang is not True:
            self.main.show_tips.setText(is_allow_lang)
        else:
            self.main.show_tips.setText('')

    # tts类型改变时
    def tts_type_change(self, type):
        api_ready = tts.is_input_api(tts_type=type)
        # Azure's voice catalog is local, so it can be refreshed while the
        # credential dialog is open. Otherwise the previous channel's voices
        # remain visible until the user switches channels again.
        if api_ready is not True and type != tts.AZURE_TTS:
            self.main.voice_role.clear()
            self.main.current_rolelist = ["No"]
            self.main.voice_role.addItems(self.main.current_rolelist)
            return

        lang = translator.get_code(show_text=self.main.target_language.currentText())
        if lang and lang != '-':
            is_allow_lang = tts.is_allow_lang(langcode=lang, tts_type=type)
            self.main.show_tips.setText(is_allow_lang if is_allow_lang is not True else '')

        app_cfg.line_roles = {}
        _role_list = tools.role_menu(type, lang if lang and lang != '-' else None)
        self.main.voice_role.clear()
        self.main.current_rolelist = _role_list
        self.main.voice_role.addItems(self.main.current_rolelist)

    # 语言选择变化时
    def set_voice_role(self, t):
        role = self.main.voice_role.currentText()
        code = translator.get_code(show_text=t)
        if code and code != '-':
            is_allow_lang = tts.is_allow_lang(langcode=code, tts_type=self.main.tts_type.currentIndex())
            self.main.show_tips.setText(is_allow_lang if is_allow_lang is not True else '')
            # 判断翻译渠道是否支持翻译到该目标语言
            if translator.is_allow_translate(translate_type=self.main.translate_type.currentIndex(),
                                             show_target=t) is not True:
                return
        # 如果不是需要跟随语言变化角色渠道，到此结束
        if self.main.tts_type.currentIndex() not in tts.CHANGE_BY_LANGUAGE:
            if role != 'No' and self.main.app_mode in ['biaozhun']:
                self.main.listen_btn.show()
                self.main.listen_btn.setDisabled(False)
            else:
                self.main.listen_btn.hide()
            return

        # 只有当前配音渠道角色跟随语言选择变化，才继续向下执行
        self.main.voice_role.clear()
        if t == '-' or not code:
            self.main.voice_role.addItems(['No'])
            return

        tts_type = self.main.tts_type.currentIndex()
        role_language = code if tts_type == tts.AZURE_TTS else code.split('-')[0]
        _role_list = tools.role_menu(tts_type, role_language)
        self.main.current_rolelist = _role_list
        self.main.voice_role.addItems(_role_list)

    def _set_import_subtitle_status(self):
        button = getattr(self.main, 'import_sub', None)
        if not button:
            return
        if not self.imported_subtitle_files:
            button.setText(tr("Import original language SRT"))
            button.setToolTip(tr("Import text to be translated from a file.."))
            return
        if not self.queue_mp4:
            button.setText(tr("Imported subtitle files", len(self.imported_subtitle_files)))
        else:
            button.setText(tr(
                "Matched subtitle files",
                len(self.imported_subtitle_map),
                len(self.queue_mp4),
            ))
        button.setToolTip("\n".join(self.imported_subtitle_files))

    def _subtitle_match_error(self, result):
        messages = [tr("Subtitle files could not be matched to every video")]
        if result.unmatched_videos:
            messages.append(tr("Unmatched videos") + ":\n" + "\n".join(
                Path(path).name for path in result.unmatched_videos
            ))
        if result.ambiguous_videos:
            messages.append(tr("Ambiguous subtitle matches") + ":\n" + "\n".join(
                Path(path).name for path in result.ambiguous_videos
            ))
        if result.unmatched_subtitles:
            messages.append(tr("Unused subtitle files") + ":\n" + "\n".join(
                Path(path).name for path in result.unmatched_subtitles
            ))
        return "\n\n".join(messages)

    @staticmethod
    def _collect_subtitles(folder, *, recursive=False):
        iterator = Path(folder).rglob('*') if recursive else Path(folder).iterdir()
        return sorted(
            path.resolve().as_posix() for path in iterator
            if path.is_file()
            and path.suffix.casefold() in ('.srt', '.txt')
            and not path.name.casefold().startswith('combined')
        )

    def _auto_import_subtitles_for_video_folder(self, selected_folder):
        if self.imported_subtitle_files:
            return False
        folder = Path(selected_folder)
        candidates = [folder / '字幕']
        if folder.name.casefold() == '视频':
            candidates.insert(0, folder.parent / '字幕')
        for subtitle_folder in candidates:
            if not subtitle_folder.is_dir():
                continue
            subtitles = self._collect_subtitles(subtitle_folder)
            if subtitles:
                return bool(self._set_imported_subtitle_files(subtitles))
        return False

    def _refresh_imported_subtitle_matches(
            self, *, show_error=False, reload_single=True):
        self.imported_subtitle_map = {}
        if not self.imported_subtitle_files:
            self._set_import_subtitle_status()
            return True
        if not self.queue_mp4:
            self.main.subtitle_area.clear()
            self._set_import_subtitle_status()
            return True

        result = match_subtitles_to_videos(
            self.queue_mp4,
            self.imported_subtitle_files,
        )
        self.imported_subtitle_map = result.mapping
        if result.complete and len(self.queue_mp4) == 1:
            if reload_single:
                subtitle_path = next(iter(result.mapping.values()))
                self.main.subtitle_area.clear()
                self.main.subtitle_area.insertPlainText(
                    read_timed_subtitle(subtitle_path)
                )
        else:
            self.main.subtitle_area.clear()
        self._set_import_subtitle_status()
        if result.unmatched_videos:
            tips = getattr(self.main, 'show_tips', None)
            if tips:
                tips.setText(tr(
                    "Videos without matched subtitles will use speech recognition",
                    len(result.unmatched_videos),
                ))
        if result.ambiguous_videos and show_error:
            tools.show_error(self._subtitle_match_error(result))
        return result.safe_to_run

    def _set_imported_subtitle_files(self, filenames):
        subtitle_files = [Path(path).resolve().as_posix() for path in filenames]
        invalid = []
        for path in subtitle_files:
            try:
                read_timed_subtitle(path)
            except (OSError, ValueError, UnicodeError):
                invalid.append(Path(path).name)
        if invalid:
            self.imported_subtitle_files = []
            self.imported_subtitle_map = {}
            self.main.subtitle_area.clear()
            self._set_import_subtitle_status()
            tools.show_error(
                tr("Subtitle files must contain valid SRT timestamps")
                + "\n" + "\n".join(invalid)
            )
            return False
        self.imported_subtitle_files = subtitle_files
        params['last_opendir'] = Path(subtitle_files[0]).parent.as_posix()
        params.save()
        return self._refresh_imported_subtitle_matches(show_error=True)

    # 从本地导入一个或多个字幕文件
    def import_sub_fun(self):
        return self.import_sub_files()

    def import_sub_files(self):
        filenames, _ = QFileDialog.getOpenFileNames(
            self.main,
            tr("Select one or more subtitle files"),
            params.get('last_opendir', ''),
            "Subtitle files (*.srt *.txt)",
        )
        if not filenames:
            return False
        return self._set_imported_subtitle_files(filenames)

    def import_sub_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self.main,
            tr("Select subtitle folder"),
            params.get('last_opendir', ''),
        )
        if not folder:
            return False
        filenames = self._collect_subtitles(folder, recursive=True)
        if not filenames:
            tools.show_error(tr("No subtitle files found in the selected folder"))
            return False
        return self._set_imported_subtitle_files(filenames)

    # 判断是否需要翻译
    def shound_translate(self):
        if self.main.target_language.currentText() == '-' or self.main.source_language.currentText() == '-':
            return False
        if self.main.target_language.currentText() == self.main.source_language.currentText():
            return False
        return True

    # 核对tts选择是否正确
    def check_tts(self):
        if tts.is_input_api(tts_type=self.main.tts_type.currentIndex()) is not True:
            return False
        # 如果没有选择目标语言，但是选择了配音角色，无法配音
        if self.main.target_language.currentText() == '-' and self.main.voice_role.currentText() not in ['No', '', ' ']:
            tools.show_error(tr('wufapeiyin'))
            return False
        return True

    # 核对所选语音识别模式是否正确
    def check_reccogn(self):
        langcode = translator.get_code(show_text=self.main.source_language.currentText())
        recogn_type = self.main.recogn_type.currentIndex()
        model_name = self.main.model_name.currentText()
        res = recognition.is_allow_lang(langcode=langcode, recogn_type=recogn_type, model_name=model_name)
        self.main.show_tips.setText(res if res is not True else '')

        # 判断是否填写自定义识别 api openai-api识别
        return recognition.is_input_api(recogn_type=recogn_type)

    def check_output(self):
        from PySide6.QtWidgets import QMessageBox
        input_folder = Path(self.queue_mp4[0]).parent
        output_folder = input_folder / '_video_out' if not self.main.target_dir else Path(self.main.target_dir)
        # 输出文件夹尚不存在
        if not output_folder.exists():
            return True

        # 输入输出是同个文件夹，
        if self.main.only_out_mp4.isChecked() and input_folder.samefile(output_folder):
            tools.show_error(
                tr("The output directory is not allowed to point to the input directory"))
            return False

        # 输出目录是空的
        if not self.main.clear_cache.isChecked():
            return True
        for it in self.queue_mp4:
            p = Path(it)
            folder = output_folder / f'{p.stem}-{p.suffix.lower()[1:]}'
            if folder.exists():
                reply = QMessageBox.question(
                    self.main,
                    tr("Are you sure the cleanup has been output?"),
                    tr("If you confirm to clean up, all files in the output directory will be deleted. If you manually specify the output directory, please make sure there are no important files in the directory and back it up in advance to avoid data loss.",
                       folder.as_posix()),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No
                )

                if reply != QMessageBox.StandardButton.Yes:
                    return False
                return True
        return True

    def check_name_length(self):
        if sys.platform != 'win32':
            return True
        from PySide6.QtWidgets import QMessageBox
        for it in self.queue_mp4:
            _itlen = len(it)
            _namelen = len(Path(it).name)
            if _itlen >= 170 and _namelen >= 90:
                reply = QMessageBox.question(
                    self.main,
                    tr("The filename is too long"),
                    tr("Filename length check", _namelen, _itlen) + f"\n\n{it}",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No
                )

                if reply != QMessageBox.StandardButton.Yes:
                    return False
                return True
        return True

    # 检测开始状态并启动
    def check_start(self):
        # 已在执行中，则停止
        if app_cfg.current_status == 'ing':
            self.update_status('stop')
            return
        self.main.startbtn.setDisabled(True)
        # 存储所有音视频文件都需要用到信息，例如原始语言 目标语言、 渠道、角色等
        self.cfg = {}
        # 重置字幕行角色
        app_cfg.line_roles = {}
        self.is_render = False
        # 倒计时
        app_cfg.set_countdown(int(float(settings.get('countdown_sec', 1))))

        # 无视频选择 ，也无导入字幕，无法处理
        if len(self.queue_mp4) < 1:
            tools.show_error(tr("Video file must be selected"))
            self.main.startbtn.setDisabled(False)
            return
        if not self._refresh_imported_subtitle_matches(
                show_error=True, reload_single=False):
            self.main.startbtn.setDisabled(False)
            return
        # 核对代理
        if self.check_proxy() is not True:
            self.main.startbtn.setDisabled(False)
            return

        # 先确定原始和目标语言
        self.cfg['translate_type'] = self.main.translate_type.currentIndex()
        # 存储 原始语言 目标语言显示文字，非语言代码
        self.cfg['source_language'] = self.main.source_language.currentText()
        self.cfg['target_language'] = self.main.target_language.currentText()
        # 存储语言代码
        self.cfg['source_language_code'] = translator.get_code(show_text=self.cfg['source_language'])
        self.cfg['target_language_code'] = translator.get_code(show_text=self.cfg['target_language'])

        # 清理缓存
        self.cfg['clear_cache'] = self.main.clear_cache.isChecked()
        self.cfg['only_out_mp4'] = self.main.only_out_mp4.isChecked()
        self.cfg['fix_punc'] = self.main.fix_punc.isChecked()
        self.cfg['remove_burned_subtitles'] = self.main.remove_burned_subtitles.isChecked()

        # 配音设置
        self.cfg['tts_type'] = self.main.tts_type.currentIndex()
        self.cfg['voice_role'] = self.main.voice_role.currentText()
        try:
            volume = int(self.main.volume_rate.value())
            pitch = int(self.main.pitch_rate.value())
        except (ValueError,TypeError):
            volume = 0
            pitch = 0
        self.cfg['volume'] = f'+{volume}%' if volume >= 0 else f'{volume}%'
        self.cfg['pitch'] = f'+{pitch}Hz' if pitch >= 0 else f'{pitch}Hz'

        # 语音识别设置
        self.cfg['recogn_type'] = self.main.recogn_type.currentIndex()
        if self.cfg['recogn_type'] == recognition.Faster_Whisper_XXL and not self.show_xxl_select():
            self.main.startbtn.setDisabled(False)
            return
        self.cfg['model_name'] = self.main.model_name.currentText()
        # 降噪
        self.cfg['remove_noise'] = self.main.remove_noise.isChecked()

        # 字幕嵌入类型
        self.cfg['subtitle_type'] = self.main.subtitle_type.currentIndex()

        # 对齐控制 配音加速 视频慢速
        self.cfg['voice_rate'] = self.main.voice_rate.value()
        try:
            voice_rate = int(self.main.voice_rate.value())
        except (TypeError,ValueError):
            voice_rate = 0
        self.cfg['voice_rate'] = f"+{voice_rate}%" if voice_rate >= 0 else f"{voice_rate}%"
        self.cfg['voice_autorate'] = self.main.voice_autorate.isChecked()
        self.cfg['video_autorate'] = self.main.video_autorate.isChecked()

        # 人声背景音分离 添加背景音频
        self.cfg['is_separate'] = self.main.is_separate.isChecked()
        self.cfg['embed_bgm'] = self.main.embed_bgm.isChecked()
        self.cfg['background_music'] = self.main.back_audio.text().strip()
        self.cfg['enable_diariz'] = self.main.enable_diariz.isChecked()
        self.cfg['recogn2pass'] = self.main.recogn2pass.isChecked()
        self.cfg['nums_diariz'] = self.main.nums_diariz.currentIndex()

        # 核对识别是否正确
        if self.check_reccogn() is not True:
            self.main.startbtn.setDisabled(False)
            return

        # 如果需要翻译，再判断是否符合翻译规则
        if self.shound_translate() and translator.is_allow_translate(
                translate_type=self.cfg['translate_type'],
                show_target=self.cfg['target_language_code']) is not True:
            self.main.startbtn.setDisabled(False)
            return
        # 字幕区文字
        txt = self.main.subtitle_area.toPlainText().strip()
        if self.check_txt(txt) is not True:
            self.main.startbtn.setDisabled(False)
            return

        # tts类型
        if self.check_tts() is not True:
            self.main.tts_type.setCurrentIndex(0)
            self.main.startbtn.setDisabled(False)
            return

        # LLM重新断句
        self.cfg['rephrase'] = self.main.rephrase.currentIndex()
        # 判断CUDA
        self.cfg['is_cuda'] = self.main.enable_cuda.isChecked()
        self.cfg['remove_silent_mid'] = False
        self.cfg['align_sub_audio'] = True
        # 只有未启用 音频加速 视频慢速时才起作用
        if not self.cfg['voice_autorate'] and not self.cfg['video_autorate']:
            self.cfg['remove_silent_mid'] = self.main.remove_silent_mid.isChecked()
            self.cfg['align_sub_audio'] = self.main.align_sub_audio.isChecked()
        if self.cuda_isok() is not True:
            self.main.startbtn.setDisabled(False)
            return

        # 未设置目标语言，不允许嵌入字幕
        if not self.cfg.get('target_language_code') and self.cfg['subtitle_type'] > 0:
            self.main.startbtn.setDisabled(False)
            return tools.show_error(
                tr("Target language must be selected to embed subtitles"))

        # 核对是否存在名字相同后缀不同的文件，以及若存在音频则强制为tiqu模式
        if self.check_name() is not True:
            self.main.startbtn.setDisabled(False)
            return

        # LLM 重新断句时，需判断 deepseek或openai chatgpt填写了信息
        if self.main.rephrase.currentIndex() == 1:
            ai_type = settings.get('llm_ai_type', 'chatgpt')
            if (ai_type in ['chatgpt','openai'] and not params.get('chatgpt_key')) or (ai_type == 'deepseek' and not params.get('deepseek_key')):
                self.main.startbtn.setDisabled(False)
                tools.show_error(tr('llmduanju'))
                from videotrans.winform import get_win
                get_win('deepseek' if ai_type == 'deepseek' else  'chatgpt').openwin()
                return

        # 检查输入 输出目录
        if self.check_name_length() is not True:
            self.main.startbtn.setDisabled(False)
            return
        if self.check_output() is not True:
            self.main.startbtn.setDisabled(False)
            return

        # 设置各项模式参数
        self.set_mode()
        self.cfg['app_mode'] = self.main.app_mode
        self.cfg['output_srt'] = self.main.output_srt.currentIndex()

        batch_videos = [
            video_path for video_path in self.queue_mp4
            if Path(video_path).suffix.lower().lstrip('.') in contants.VIDEO_EXTS
        ]
        first_video = batch_videos[0] if batch_videos else None
        should_remove_subtitles = bool(
            self.cfg['remove_burned_subtitles']
            and self.main.app_mode != 'tiqu'
            and first_video
        )
        self.cfg['remove_burned_subtitles'] = should_remove_subtitles
        self.cfg['burned_subtitle_ocr'] = bool(
            settings.get("burned_subtitle_ocr", True)
        )
        settings['remove_burned_subtitles'] = self.main.remove_burned_subtitles.isChecked()

        initial_rect = None
        saved_rect = settings.get("subtitle_removal_last_rect", "")
        if saved_rect:
            try:
                candidate = json.loads(saved_rect) if isinstance(saved_rect, str) else saved_rect
                if isinstance(candidate, list) and len(candidate) == 4:
                    initial_rect = candidate
            except (TypeError, ValueError, json.JSONDecodeError):
                pass

        should_prompt_for_ocr_area = _should_prompt_for_ocr_area(
            burned_subtitle_ocr=self.cfg['burned_subtitle_ocr'],
            app_mode=self.main.app_mode,
            first_video=first_video,
            recogn_type=self.cfg.get('recogn_type'),
            source_language_code=self.cfg.get('source_language_code', ''),
            initial_rect=initial_rect,
        ) and len(self.imported_subtitle_map) < len(self.queue_mp4)
        engine = None
        if should_remove_subtitles or should_prompt_for_ocr_area:
            from videotrans.subtitle_removal import find_subtitle_remover_engine
            engine = find_subtitle_remover_engine(ROOT_DIR)
            if should_remove_subtitles and not engine:
                self.main.startbtn.setDisabled(False)
                tools.show_error(tr("Subtitle removal engine is not installed"))
                return
            missing = engine.missing_files("auto") if engine else []
            if should_remove_subtitles and missing:
                self.main.startbtn.setDisabled(False)
                tools.show_error(
                    tr("Subtitle removal model is incomplete")
                    + "\n" + "\n".join(str(path) for path in missing)
                )
                return
            # OCR-only selection needs the text detector, not the inpainting
            # model required by subtitle removal.
            ocr_detector = (
                engine.root / "backend" / "models" / "V5"
                / "ch_det_fast" / "inference.json"
            ) if engine else None
            if not ocr_detector or not ocr_detector.is_file():
                should_prompt_for_ocr_area = False

        if should_remove_subtitles or should_prompt_for_ocr_area:
            from videotrans.component.subtitle_removal import select_batch_subtitle_area
            selection = select_batch_subtitle_area(
                input_file=first_video,
                input_files=batch_videos,
                initial_normalized_rect=initial_rect,
                parent=self.main,
            )
            if selection is None:
                self.main.startbtn.setDisabled(False)
                return
            if selection.get("skip_ocr"):
                self.cfg['burned_subtitle_ocr'] = False
                self.cfg['remove_burned_subtitles'] = False
                self.cfg['subtitle_removal_rect'] = None
                self.cfg['subtitle_removal_aspect_ratio'] = 0.0
            else:
                self.cfg['subtitle_removal_rect'] = selection['normalized_rect']
                self.cfg['subtitle_removal_aspect_ratio'] = selection['reference_aspect_ratio']
                settings['subtitle_removal_last_rect'] = json.dumps(selection['normalized_rect'])
                settings['subtitle_removal_last_aspect_ratio'] = selection['reference_aspect_ratio']
        else:
            self.cfg['subtitle_removal_rect'] = None
            self.cfg['subtitle_removal_aspect_ratio'] = 0.0
        settings.save()

        if self.main.recogn_type.currentIndex() == recognition.FASTER_WHISPER or self.main.app_mode == 'biaozhun':
            # 背景音量
            self.cfg['loop_backaudio'] = self.main.is_loop_bgm.currentIndex()
            try:
                self.cfg['backaudio_volume'] = float(self.main.bgmvolume.text())
            except (TypeError,ValueError):
                pass

        params.getset_params(self.cfg | {"select_file_type": self.main.select_file_type.isChecked()})
        params.save()

        self.delete_process()

        # 设为开始
        self.update_status('ing')

        # AI翻译发送完整字幕
        settings['aisendsrt'] = self.main.aisendsrt.isChecked()
        settings.save()

        self._disabled_button(True)
        self.main.subtitle_area.setReadOnly(True)
        self.main.startbtn.setDisabled(False)
        self.retry_queue_mp4 = []
        self.uuid_queue_mp4 = {}
        self.main.retrybtn.setVisible(False)
        self.create_btns()

    def retry(self):
        if not self.retry_queue_mp4:
            self.main.retrybtn.setVisible(False)
            return

        self._disabled_button(True)
        self.main.retrybtn.setVisible(False)
        self.main.subtitle_area.setReadOnly(True)
        self.delete_process()
        # 设为开始
        self.update_status('ing')
        # 待翻译的文件列表
        self.obj_list = []

        cfg = copy.deepcopy(self.cfg)
        for v in self.retry_queue_mp4:
            obj:InputFile = tools.format_video(v.get('name'), v.get('target_dir'))
            app_cfg.rm_uuid(obj['uuid'])
            self.obj_list.append(obj)
            self.add_process_btn(
                target_dir=Path(obj['target_dir']).as_posix() if cfg.get('app_mode') == 'tiqu' or not cfg.get(
                    'only_out_mp4') else v.get('target_dir'),
                name=obj['name'],
                uuid=obj['uuid'])

        cfg['series_video_paths'] = [Path(obj['name']).as_posix() for obj in self.obj_list]
        cfg['clear_cache'] = False

        from videotrans.task.mult_video import MultVideo
        task = MultVideo(parent=self.main, cfg=cfg, input_file_list=self.obj_list)
        task.start()
        self.main.startbtn.setDisabled(False)
        # 不再重试
        self.retry_queue_mp4 = []

    # 创建进度按钮
    def create_btns(self):
        self.main.show_tips.show()
        self.main.show_tips.setText(tr('Creating progress bar, please wait'))
        # 输出目录，此时该目录是 视频名子文件夹的父级
        target_dir = (Path(
            self.queue_mp4[0]).parent / '_video_out').as_posix() if not self.main.target_dir else self.main.target_dir
        # 待翻译的文件列表
        self.obj_list = []

        # 判断非法文件名
        forbid_names = []
        for video_path in self.queue_mp4:
            obj:InputFile = tools.format_video(video_path, target_dir)
            if sys.platform == "win32" and re.search(r'[?:<>*|/"]', obj['basename']):
                forbid_names.append(obj['basename'])
                continue
            self.obj_list.append(obj)

        if forbid_names:
            self.update_status("stop")
            tools.show_error(tr('win-forbid-name', '?:<>*|/"') + "\n" + ("\n".join(forbid_names)))
            return

        txt = self.main.subtitle_area.toPlainText().strip()
        self.cfg['subtitle_files'] = copy.deepcopy(self.imported_subtitle_map)
        if self.imported_subtitle_files:
            # Single-file text remains editable in the text area; batch items
            # are read from their own matched files inside TransCreate.
            txt = txt if len(self.queue_mp4) == 1 else ''
        self.cfg.update(
            {
                'subtitles': txt,
                'app_mode': self.main.app_mode,
                'series_video_paths': [
                    Path(obj['name']).as_posix() for obj in self.obj_list
                ],
            }
        )
        cfg = copy.deepcopy(self.cfg)

        for obj in self.obj_list:
            self.add_process_btn(
                target_dir=Path(obj['target_dir']).as_posix() if cfg.get('app_mode') == 'tiqu' or not cfg.get(
                    'only_out_mp4') else target_dir,
                name=obj['name'],
                uuid=obj['uuid'])
            self.uuid_queue_mp4[obj['uuid']] = (obj['name'], target_dir)
        self.main.show_tips.setText('')
        # 单个视频处理模式
        if self.main.app_mode not in ['tiqu'] and len(self.obj_list) == 1:
            from videotrans.task.only_one import Worker
            task = Worker(
                parent=self.main,
                file=self.obj_list[0],
                cfg=cfg
            )
            task.uito.connect(self.update_data)
            task.start()
            return

        from videotrans.task.mult_video import MultVideo
        task = MultVideo(parent=self.main, cfg=cfg, input_file_list=self.obj_list)
        task.start()


    # 添加进度条
    def add_process_btn(self, *, target_dir: str = None, name: str = None, uuid=None):

        clickable_progress_bar = ClickableProgressBar(self)
        clickable_progress_bar.progress_bar.setValue(0)  # 设置当前进度值
        clickable_progress_bar.setText(tr("waitforstart"))
        clickable_progress_bar.setMinimumSize(500, 50)
        clickable_progress_bar.setToolTip(tr('mubiao'))
        # # 将按钮添加到布局中
        if self.cfg.get('app_mode') == 'tiqu' and self.cfg.get('copysrt_rawvideo'):
            target_dir = Path(name).parent.as_posix()

        clickable_progress_bar.setTarget(
            target_dir=target_dir,
            name=name
        )
        clickable_progress_bar.setCursor(Qt.PointingHandCursor)
        self.main.processlayout.addWidget(clickable_progress_bar)
        if uuid:
            self.processbtns[uuid] = clickable_progress_bar

    # 设置按钮上的日志信息
    def set_process_btn_text(self, d):
        text, uuid, _type = d['text'], d.get('uuid', ''), d.get('type', 'logs')
        if not uuid or uuid not in self.processbtns:
            return
        if _type == 'set_precent' and self.processbtns[uuid].precent < 100:
            t, precent = text.split('???')
            precent = int(float(precent) * 100) / 100
            self.processbtns[uuid].setPrecent(precent)
            self.processbtns[uuid].setText(f'{t}')
        elif _type == 'logs' and self.processbtns[uuid].precent < 100:
            self.processbtns[uuid].setText(text)
        elif _type == 'succeed':
            self.processbtns[uuid].setEnd()
            if self.processbtns[uuid].name in self.queue_mp4:
                self.queue_mp4.remove(self.processbtns[uuid].name)
        elif _type == 'error':
            self.processbtns[uuid].setError(text)
            self.processbtns[uuid].progress_bar.setStyleSheet('color:#ff0000')
            self.processbtns[uuid].setCursor(Qt.PointingHandCursor)

    # 更新执行状态
    def update_status(self, type):
        if self.had_click_btn: return
        self.had_click_btn = True
        app_cfg.current_status = type
        if type == 'ing':
            # 重设为开始状态
            self.disabled_widget(True)
            self.main.startbtn.setText(tr("starting..."))
            self.had_click_btn = False
            return
        # stop 停止，end=结束
        self.main.subtitle_area.clear()
        self.main.startbtn.setText(tr(type))

        # 启用
        self.disabled_widget(False)
        # 启用相关模式
        self._disabled_button(False)
        for it in self.obj_list:
            app_cfg.stoped_uuid_set.add(it['uuid'])

        if type == 'end':
            # 全部完成
            self.main.subtitle_area.clear()
            for prb in self.processbtns.values():
                prb.setEnd()
            # 关机
            if self.main.shutdown.isChecked():
                try:
                    tools.shutdown_system()
                except Exception as e:
                    tools.show_error(tr('shutdownerror') + str(e))
        else:
            # 手动暂停 stop
            app_cfg.set_countdown(-1)
            self.set_djs_timeout()
            # 任务队列中设为停止并删除队列，防止后续到来的日志继续显示
            for it in self.obj_list:
                # 按钮设为暂停
                if it['uuid'] in self.processbtns:
                    self.processbtns[it['uuid']].setPause()

        if self.main.app_mode == 'tiqu':
            self.set_tiquzimu()
        self._reset()
        self.had_click_btn = False

    # 更新 UI
    def update_data(self, uuid: Union[str, None] = "", d: Union[SignMsg, None] = None):
        if d['type'] == 'ffmpeg':
            self.main.startbtn.setText(d['text'])
            self.main.startbtn.setDisabled(True)
            self.main.startbtn.setStyleSheet("""color:#ff0000""")
            return
        if d['type'] == 'refreshtts':
            currentIndex = self.main.tts_type.currentIndex()
            if currentIndex > 0:
                self.main.tts_type.setCurrentIndex(0)
                QTimer.singleShot(100, lambda: self.main.tts_type.setCurrentIndex(currentIndex))
            return
        if d['type'] == 'refreshmodel_list' and self.main.recogn_type.currentIndex() in [recognition.FASTER_WHISPER,
                                                                                         recognition.Faster_Whisper_XXL,
                                                                                         recognition.Whisper_CPP]:
            current_model_name = self.main.model_name.currentText()
            self.main.model_name.clear()
            self.main.model_name.addItems(
                settings.Whisper_CPP_MODEL_LIST if self.main.recogn_type.currentIndex() == recognition.Whisper_CPP else settings.WHISPER_MODEL_LIST)
            self.main.model_name.setCurrentText(current_model_name)
            return
        # 任务开始执行，初始化按钮等
        if d['type'] == 'shitingerror':
            tools.show_error(d['text'])
            return


        if d['type'] in ['logs', 'error', 'succeed', 'set_precent']:
            self.set_process_btn_text(d)
            if uuid and d['type'] in ['error', 'succeed']:
                app_cfg.stoped_uuid_set.add(d['uuid'])
                self._check_all_done()

            if not uuid or d['type'] != 'error': return
            #将出错的加入重试队列
            vdata = self.uuid_queue_mp4.get(uuid)
            if not vdata: return
            self.retry_queue_mp4.append( InputFile(name=vdata[0], target_dir=vdata[1]) )
            return

        if d['type'] == 'end':
            # 任务全部完成时出现 end
            self.update_status('end')
            self.main.retrybtn.setVisible(True if self.retry_queue_mp4 else False)
            return

        if d['type'] == 'edit_dubbing':
            # 显示编辑翻译框
            from videotrans.component.onlyone_set_editdubb import EditDubbingResultDialog

            cache_folder, language = d['text'].split('<|>')
            dialog = EditDubbingResultDialog(
                cache_folder=cache_folder,
                language=language,
                parent=self.main

            )
            if dialog.exec():
                self.set_djs_timeout()
            else:
                self.update_status('stop')
            return
        if d['type'] == 'edit_subtitle_source':
            # 显示编辑翻译框
            from videotrans.component.onlyone_set_recogn import EditRecognResultDialog

            dialog = EditRecognResultDialog(
                source_sub=app_cfg.onlyone_source_sub,
                parent=self.main
            )

            if dialog.exec():
                self.set_djs_timeout()
            else:
                self.update_status('stop')
            return
        if d['type'] == 'edit_recogn2_subtitle':
            # 显示编辑翻译框
            from videotrans.component.onlyone_set_recogn2 import EditRecognResultDialog2

            dialog = EditRecognResultDialog2(
                target_sub=app_cfg.onlyone_target_sub,
                parent=self.main
            )

            if dialog.exec():
                self.set_djs_timeout()
            else:
                self.update_status('stop')
            return
        if d['type'] == 'edit_subtitle_target':
            # 弹出编辑配音字幕
            from videotrans.component.onlyone_set_role import SpeakerAssignmentDialog
            try:
                role_payload = json.loads(d['text'])
            except (TypeError, ValueError, json.JSONDecodeError):
                parts = d['text'].split('<|>', 3)
                role_payload = {
                    'cache_folder': parts[0],
                    'target_language': parts[1],
                    'tts_type': parts[2],
                    'source_audio': parts[3] if len(parts) > 3 else None,
                }
            dialog = SpeakerAssignmentDialog(
                source_sub=None if not app_cfg.onlyone_trans else app_cfg.onlyone_source_sub,
                source_audio=role_payload.get('source_audio'),
                target_sub=app_cfg.onlyone_target_sub,
                all_voices=self.main.current_rolelist,
                cache_folder=role_payload.get('cache_folder'),
                target_language=role_payload.get('target_language', 'en'),
                source_language=role_payload.get('source_language', ''),
                tts_type=int(role_payload.get('tts_type', 0)),
                video_path=role_payload.get('video_path'),
                series_folder=role_payload.get('series_folder'),
                series_video_paths=role_payload.get('series_video_paths'),
                series_output_dir=role_payload.get('series_output_dir'),
                default_role=self.main.voice_role.currentText(),
                parent=self.main

            )
            if dialog.exec():
                self.set_djs_timeout()
            else:
                self.update_status('stop')
            return
        # 一行一行插入字幕到字幕编辑区
        if d['type'] == "subtitle" and app_cfg.current_status == 'ing':
            self.main.subtitle_area.moveCursor(QTextCursor.End)
            self.main.subtitle_area.insertPlainText(d['text'])
            return
        if d['type'] == 'replace_subtitle':
            # 完全替换字幕区
            self.main.subtitle_area.clear()
            self.main.subtitle_area.insertPlainText(d['text'])
            return


    def _check_all_done(self):
        active = [obj for obj in self.obj_list if obj['uuid'] not in app_cfg.stoped_uuid_set]
        if not active:
            self.update_status('end')
            self.main.retrybtn.setVisible(bool(self.retry_queue_mp4))
