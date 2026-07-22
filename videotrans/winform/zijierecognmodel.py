

def openwin():
    from PySide6 import QtWidgets
    from videotrans.configure.config import tr,app_cfg,params
    from videotrans import recognition
    from videotrans.util import tools
    from videotrans.util.TestSTT import TestSTT

    def feed(d):
        if d.startswith("ok"):
            QtWidgets.QMessageBox.information(winobj, "ok", d[3:])
        else:
            tools.show_error(d)
        winobj.test.setText(tr('Test'))

    def test():

        api_key = winobj.zijierecognmodel_apikey.text().strip()
        appid = winobj.zijierecognmodel_appid.text().strip()
        access = winobj.zijierecognmodel_token.text().strip()
        if not api_key and (not appid or not access):
            return tools.show_error('请填写新版 API Key，或同时填写旧版 AppID 和 Access Token')
        params["zijierecognmodel_apikey"] = api_key
        params["zijierecognmodel_appid"] = appid
        params["zijierecognmodel_token"] = access

        task = TestSTT(parent=winobj, recogn_type=recognition.ZIJIE_RECOGN_MODEL)
        task.uito.connect(feed)
        task.start()

        winobj.test.setText(tr('Testing...'))

    def save():
        api_key = winobj.zijierecognmodel_apikey.text().strip()
        appid = winobj.zijierecognmodel_appid.text().strip()
        access = winobj.zijierecognmodel_token.text().strip()

        params["zijierecognmodel_apikey"] = api_key
        params["zijierecognmodel_appid"] = appid
        params["zijierecognmodel_token"] = access
        params.save()
        winobj.close()



    from videotrans.component.set_form import ZijierecognmodelForm
    winobj = ZijierecognmodelForm()
    app_cfg.child_forms['zijierecognmodel'] = winobj
    winobj.update_ui()
    winobj.set.clicked.connect(save)
    winobj.test.clicked.connect(test)
    winobj.show()
