# 阿里云 Media Worker

`cineflow.media_worker` 是独立的阿里云 ICE/OSS 适配服务，负责 CineFlow 产生的新媒体文件，不替代现有 pyVideoTrans 的 OSS 上传和字幕消除代码。

## 边界

现有客户端继续负责：

- 原视频上传 OSS；
- STS、私有 Bucket、分片上传、断点续传和对象校验；
- 本地、Caca 或阿里云 IMS 原字幕消除；
- 字幕消除任务恢复、结果校验与临时对象清理。

Media Worker 只接收这些上游 URL：

```text
input_url
clean_video_url（可选，优先用于最终画面）
source_audio_url（可选，优先用于 ASR 和声伴分离）
```

它自己的 OSS 配置仅用于保存：

- 云端抽取的音频；
- `MusicDemix` 输出；
- Azure TTS 生成的音频片段；
- 最终视频；
- 单独交付的 SRT。

因此，后续把 `projects/cineflow-cloud` 整体移动到新仓库，不会改变当前客户端上传或字幕消除流程。

## 功能

### `/v1/prepare`

输入完整 `JobRequest`，返回 `MediaArtifacts`。

处理顺序：

1. 画面优先使用 `clean_video_url`，否则使用 `input_url`；
2. 音频优先使用 `source_audio_url`；
3. 没有音频 URL 时，通过 ICE `SubmitMediaProducingJob` 的纯音频 Timeline 抽取音轨；
4. `separate_background=true` 时提交 `SubmitIProductionJob(FunctionName=MusicDemix)`；
5. 解析 `{resultType}` 输出，归一化为 `vocal_url` 与 `background_url`；
6. 可选增强失败时写入 `degraded_features` 和 `metadata`，不伪造成成功。

返回示例：

```json
{
  "video_url": "https://bucket.oss-cn-beijing.aliyuncs.com/clean.mp4",
  "source_audio_url": "https://bucket.oss-cn-beijing.aliyuncs.com/source.wav",
  "vocal_url": "https://bucket.oss-cn-beijing.aliyuncs.com/demix-vocal.wav",
  "background_url": "https://bucket.oss-cn-beijing.aliyuncs.com/demix-accompaniment.wav",
  "provider": "aliyun_ice",
  "task_ids": {
    "music_demix": "job-id"
  },
  "metadata": {},
  "degraded_features": []
}
```

### `/v1/artifacts/base64`

控制平面将 Azure TTS 返回的单条音频写入 Media Worker：

```json
{
  "name": "line-17.mp3",
  "content_base64": "..."
}
```

Worker 将其写入私有 OSS，并返回：

- `url`：无查询参数的内部规范 URL，供同账号 ICE Timeline 使用；
- `download_url`：有时效签名地址；
- `oss_uri`；
- `object_key`。

接口有单文件大小上限，默认 32 MiB。日志不得输出音频 Base64。

### `/v1/assemble`

输入：

```text
JobRequest
MediaArtifacts
翻译后的 Transcript
DubbingArtifact
```

Worker 构造 ICE Timeline：

- 视频轨使用干净视频，并把原视频音量设为 0；
- 有 `background_url` 时按 `CINEFLOW_MEDIA_BACKGROUND_GAIN` 混入背景；
- 每条 Azure TTS 片段按字幕 `start_ms` 设置 `TimelineIn`；
- 相互重叠的配音片段自动分配到不同音频轨；
- 硬字幕使用 `SubtitleTracks[].SubtitleTrackClips`；
- 最终输出到私有 OSS；
- `soft` 模式当前单独交付 SRT，不把 mov_text 字幕轨封装进 MP4；
- 返回最终视频签名 URL、字幕 URL、ICE 任务 ID 和输出元数据。

## 运行

安装阿里云依赖：

```bash
python -m pip install -e '.[aliyun]'
```

启动：

```bash
uvicorn cineflow.media_worker:app --host 0.0.0.0 --port 8093
```

Docker Compose：

```bash
docker compose --profile media up -d media-aliyun
```

Kubernetes：

```bash
kubectl apply -f deploy/kubernetes/media-worker.yaml
```

## 主要配置

```text
CINEFLOW_MEDIA_WORKER_URL=http://media-aliyun:8093
CINEFLOW_MEDIA_WORKER_TIMEOUT_SECONDS=900

CINEFLOW_MEDIA_ALIYUN_REGION=cn-beijing
CINEFLOW_MEDIA_ALIYUN_ACCESS_KEY_ID=...
CINEFLOW_MEDIA_ALIYUN_ACCESS_KEY_SECRET=...
CINEFLOW_MEDIA_ALIYUN_SECURITY_TOKEN=

CINEFLOW_MEDIA_OSS_ENDPOINT=https://oss-cn-beijing.aliyuncs.com
CINEFLOW_MEDIA_OSS_BUCKET=your-private-bucket
CINEFLOW_MEDIA_OSS_PREFIX=cineflow
CINEFLOW_MEDIA_OSS_SIGNED_URL_TTL_SECONDS=86400

CINEFLOW_MEDIA_BACKGROUND_GAIN=0.25
CINEFLOW_MEDIA_OUTPUT_BITRATE_KBPS=3000
CINEFLOW_MEDIA_HARD_SUBTITLE_FONT=Alibaba PuHuiTi
CINEFLOW_MEDIA_HARD_SUBTITLE_FONT_SIZE=54
```

也支持阿里云标准凭据环境变量：

```text
ALIBABA_CLOUD_ACCESS_KEY_ID
ALIBABA_CLOUD_ACCESS_KEY_SECRET
ALIBABA_CLOUD_SECURITY_TOKEN
```

## 地域与 OSS 要求

ICE 输入和输出应满足：

- OSS Bucket 与 ICE 服务位于兼容地域；
- 输出 Bucket 已按阿里云要求注册或授权给智能媒体服务；
- Media Worker 的 RAM 身份有最小化的 ICE 调用权限和指定前缀 OSS 读写权限；
- 上游签名 URL 的有效期覆盖排队和处理时间；
- 对同账号 OSS 输入，Worker 会去除易过期的查询参数，交由 ICE 使用服务端权限读取。

不要使用阿里云主账号 AccessKey。

## 300 秒目标

Media Worker 的 `job_timeout_seconds` 和控制平面 HTTP timeout 是单个云任务的异常保护，不是全流程 300 秒硬截止。

- 300 秒仍是优化目标；
- 超过目标时继续等待有效的 ICE 任务完成；
- 控制平面通过阶段指标与 `target_exceeded=true` 记录慢任务；
- 不因达到 300 秒自动取消、丢弃或伪造结果。

## 当前限制

这是可运行的 Media Worker MVP，但生产上线前仍需完成以下验证：

1. **真实 ICE 返回结构**：不同地域/版本的 `MusicDemix` 结果字段需要用正式账号做契约测试；无法分类时系统保留全部输出到 `metadata` 并标记降级；
2. **真正的配音时长拟合**：当前按字幕开始时间放置 Azure 音频；若配音超过时间槽，会分轨保留声音，但尚未自动 time-stretch；
3. **软字幕封装**：当前返回独立 SRT；需要内嵌软字幕时应增加专门封装步骤；
4. **任务持久化**：当前一次 HTTP 请求内完成提交和轮询；生产版应把 `JobId` 写入 Redis/PostgreSQL，以便 Worker 重启后继续查询；
5. **火山 Media 后备**：控制平面已有 `SECONDARY_MEDIA_WORKER_URL`，但真实火山媒体适配器尚未实现；
6. **实测性能和费用**：必须用真实 30 秒、2 分钟和 5 分钟素材记录 p50/p95、声伴分离耗时、合成耗时和实际账单。

## 接口安全

可设置：

```text
CINEFLOW_MEDIA_BEARER_TOKEN=...
CINEFLOW_WORKER_BEARER_TOKEN=同一个值
```

除 `/healthz` 外，Media Worker 的写接口会验证 `Authorization: Bearer ...`。生产环境还应只暴露在私网，并通过安全组或服务网格限制调用方。
