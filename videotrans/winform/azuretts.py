def openwin():
    from PySide6 import QtWidgets
    from videotrans.configure.config import tr,app_cfg, params
    from videotrans.configure import config
    from videotrans.util import tools
    from videotrans.util.ListenVoice import ListenVoice

    def apply_form_params():
        key = winobj.speech_key.text().strip()
        region = winobj.speech_region.text().strip()
        if not region or not region.startswith('https:'):
            region = winobj.azuretts_area.currentText()
        params['azure_speech_key'] = key
        params['azure_speech_region'] = region
        params['azure_tts_concurrency'] = winobj.azure_tts_concurrency.value()
        params['azure_tts_wait'] = winobj.azure_tts_wait.value()
        params['azure_tts_adaptive'] = winobj.azure_tts_adaptive.isChecked()
        return key

    def toggle_key_visibility(visible):
        winobj.speech_key.setEchoMode(
            QtWidgets.QLineEdit.Normal if visible else QtWidgets.QLineEdit.Password
        )
        winobj.show_key.setText('隐藏' if visible else '显示')

    def feed(d):
        if d == "ok":
            QtWidgets.QMessageBox.information(winobj, "ok", "Test Ok")
        else:
            tools.show_error(d)
        winobj.test.setText(tr("Test"))

    def test():
        key = apply_form_params()
        if not key:
            tools.show_error('填写Azure speech key ')
            return
        from videotrans import tts
        import time
        wk = ListenVoice(parent=winobj, queue_tts=[{"text": '你好啊我的朋友', "role": 'zh-CN-YunjianNeural',
                                                    "filename": config.TEMP_DIR + f"/{time.time()}-azure.wav",
                                                    "tts_type": tts.AZURE_TTS}], language="zh", tts_type=tts.AZURE_TTS)
        wk.uito.connect(feed)
        wk.start()
        winobj.test.setText('Testing...')

    def save():
        apply_form_params()
        params.save()
        winobj.close()

    from videotrans.component.set_form import AzurettsForm

    winobj = AzurettsForm()
    app_cfg.child_forms['azuretts'] = winobj
    if params.get('azure_speech_region','') and params.get('azure_speech_region','').startswith('http'):
        winobj.speech_region.setText(params.get('azure_speech_region',''))
    else:
        winobj.azuretts_area.setCurrentText(params.get('azure_speech_region',''))
    if params.get('azure_speech_key',''):
        winobj.speech_key.setText(params.get('azure_speech_key',''))
    try:
        winobj.azure_tts_concurrency.setValue(int(float(params.get('azure_tts_concurrency', 5))))
    except (TypeError, ValueError):
        winobj.azure_tts_concurrency.setValue(5)
    try:
        winobj.azure_tts_wait.setValue(float(params.get('azure_tts_wait', 0)))
    except (TypeError, ValueError):
        winobj.azure_tts_wait.setValue(0)
    adaptive = params.get('azure_tts_adaptive', True)
    if isinstance(adaptive, str):
        adaptive = adaptive.strip().lower() not in {'0', 'false', 'no', 'off'}
    winobj.azure_tts_adaptive.setChecked(bool(adaptive))
    winobj.show_key.toggled.connect(toggle_key_visibility)
    winobj.save.clicked.connect(save)
    winobj.test.clicked.connect(test)
    winobj.show()
