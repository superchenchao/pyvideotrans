# 云端字幕消除

pyVideoTrans 保留原有的本地 STTN / ProPainter，并增加两个用户主动选择的云端后端：

- `Caca API（OSS 链接）`：上传到私有阿里云 OSS 后，向接口提交一条有时效的签名 HTTPS 地址。
- `阿里云 IMS`：上传到同地域私有 OSS，使用 `VideoDetext` 异步任务处理。

本地模型仍是默认选项。切换后端不会覆盖或删除其他后端的结果缓存。

## 上传文件标准

云端后端不会上传源视频，而是直接从源文件生成一份传输视频：

- MP4 / H.264
- 横屏 1920×1080，竖屏 1080×1920
- 30 FPS CFR
- 约 6000 kbps
- 不包含音频

OSS 使用私有 Bucket、断点续传、稳定对象路径、上传后对象大小校验。链接 API 得到的是临时签名 URL，不需要把 Bucket 改成公共读。

## 凭据

密钥只从环境变量读取，不写入普通设置、任务 JSON 或日志。Windows 可在“系统属性 → 环境变量”中添加后重启 pyVideoTrans。

OSS 与 IMS：

```text
OSS_ACCESS_KEY_ID
OSS_ACCESS_KEY_SECRET
```

也支持阿里云标准名称：

```text
ALIBABA_CLOUD_ACCESS_KEY_ID
ALIBABA_CLOUD_ACCESS_KEY_SECRET
```

使用 STS 时可增加 `OSS_SESSION_TOKEN` 或 `ALIBABA_CLOUD_SECURITY_TOKEN`。

Caca API：

```text
PYVIDEOTRANS_CACA_SECRET_ID
PYVIDEOTRANS_CACA_SECRET_KEY
```

请使用专用 RAM 用户和最小权限，不要使用阿里云主账号 AccessKey。

## 使用

1. 勾选“消除原视频字幕”。
2. 在旁边选择“本地模型”“Caca API（OSS 链接）”或“阿里云 IMS”。
3. 云端方式点击“云端设置”，填写 OSS 地域、Bucket 等非敏感配置。
4. 开始任务并框选字幕区域。

链接 API 的坐标单位在接口文档中没有明确说明，因此默认让接口自动检测。只有接口提供方确认接受 0～1 归一化坐标后，才启用“发送归一化 x1/y1/x2/y2”。

## 失败恢复

- OSS 上传和下载均使用断点续传。
- 取得远端任务 ID 后立即持久化；软件重启后继续查询，不重复提交。
- 提交请求超时且无法确认是否已创建任务时，状态标记为 `submit_unknown`，程序不会自动重提，避免重复计费。
- 下载到临时文件后检查大小；云端结果还会校验分辨率、帧率、时长并完整解码，再写入可复用缓存。
- 用户取消时保留任务 ID。链接 API 没有取消接口，下一次可继续查询；IMS 会尽力请求远端取消。

任务状态文件和下载临时文件都在当前视频缓存目录。选择“清理已生成”会清除这些恢复信息，因此远端任务运行期间不要清理缓存。
