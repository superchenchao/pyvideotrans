# 阿里云 Media Worker

`cineflow.media_worker` 是独立的阿里云 ICE/OSS 适配服务，位于新项目完整流程的中后段。原视频上传和字幕消除也已经在本项目中实现，但由不同 Worker 负责：

```text
Upload Worker       STS、multipart、断点续传、对象校验
Subtitle Worker     VideoDetext、Caca、本地模型、恢复和清理
Media Worker        音频准备、MusicDemix、Azure 片段、最终合成
```

这种拆分避免单一服务同时承担大文件上传、外部视频任务和最终合成，便于独立扩缩容和故障隔离。

## 输入

Media Worker 接收：

```text
input_url
clean_video_url（可选，Subtitle Worker 的去字幕结果）
source_audio_url（可选，已有音频时直接复用）
```

它自己的 OSS 配置用于保存：

- 云端抽取的音频；
- `MusicDemix` 输出；
- Azure TTS WAV 片段；
- 最终视频；
- 单独交付的 SRT。

原片通常位于 `cineflow/sources`，去字幕结果位于 `cineflow/subtitle-removal`，Media Worker 生成物建议位于 `cineflow/generated`。

## `/v1/prepare`

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
  "task_ids": {"music_demix": "job-id"},
  "metadata": {},
  "degraded_features": []
}
```

## `/v1/artifacts/base64`

控制平面将 Azure TTS 返回的单条 RIFF PCM WAV 写入 Media Worker：

```json
{
  "name": "line-17.wav",
  "content_base64": "..."
}
```

Worker 写入私有 OSS，并返回：

- `url`：无查询参数的规范 URL，供同账号 ICE Timeline 使用；
- `download_url`：短时效签名地址；
- `oss_uri`；
- `object_key`。

接口默认单文件上限为 32 MiB，日志不得输出音频 Base64。

## `/v1/assemble`

输入：

```text
JobRequest
MediaArtifacts
翻译后的 Transcript
DubbingArtifact
```

Worker 构造 ICE Timeline：

- 视频轨使用干净视频，并将原视频音量设为 0；
- 有 `background_url` 时按 `CINEFLOW_MEDIA_BACKGROUND_GAIN` 混入背景；
- 每条 Azure TTS 片段按字幕 `start_ms` 设置 `TimelineIn`；
- 音频轨分配使用 WAV 实测 `duration_ms`；
- 相互重叠的配音自动分配到不同音频轨；
- 硬字幕使用 `SubtitleTracks[].SubtitleTrackClips`；
- `soft` 模式当前单独交付 SRT；
- 最终输出到私有 OSS；
- 返回视频签名 URL、字幕 URL、ICE JobId 和输出元数据。

Azure TTS 已实现：

- RIFF PCM WAV 时长测量；
- 根据时间槽计算 SSML `prosody rate`；
- 有界二次生成；
- 残余超时保留完整音频并产生 `timing_warning`。

尚未完成的下一层闭环是：残余超时自动调用 DeepSeek 缩句，再由 Azure 重合成。

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
CINEFLOW_MEDIA_OSS_PREFIX=cineflow/generated
CINEFLOW_MEDIA_OSS_SIGNED_URL_TTL_SECONDS=86400

CINEFLOW_MEDIA_BACKGROUND_GAIN=0.25
CINEFLOW_MEDIA_OUTPUT_BITRATE_KBPS=3000
CINEFLOW_MEDIA_HARD_SUBTITLE_FONT=Alibaba PuHuiTi
CINEFLOW_MEDIA_HARD_SUBTITLE_FONT_SIZE=54
```

也支持标准阿里云凭据环境变量：

```text
ALIBABA_CLOUD_ACCESS_KEY_ID
ALIBABA_CLOUD_ACCESS_KEY_SECRET
ALIBABA_CLOUD_SECURITY_TOKEN
```

生产环境优先使用实例 RAM 角色或密钥管理服务，不使用主账号 AccessKey。

## 地域与 OSS

ICE 输入输出应满足：

- Bucket 与 ICE 位于兼容地域；
- 输出 Bucket 已授权智能媒体服务；
- RAM 身份具有 ICE 调用权限和指定 OSS 前缀读写权限；
- 上游签名 URL 有效期覆盖排队与处理时间；
- 同账号 OSS 输入可去除易过期查询参数，由 ICE 使用服务端权限读取。

## 300 秒目标

`job_timeout_seconds` 和控制平面 HTTP timeout 是单个云任务异常保护，不是全流程 300 秒硬截止：

- 300 秒是优化目标；
- 超过目标继续等待有效 ICE 任务；
- 控制平面记录阶段耗时和 `target_exceeded=true`；
- 不因达到 300 秒取消、丢弃或伪造结果。

## 当前限制

生产上线前仍需完成：

1. 使用正式账号验证不同地域的 `MusicDemix` 与 Timeline 返回结构；
2. 完成“DeepSeek 缩句 → Azure 重合成”的残余超时闭环；
3. 需要时增加 MP4 内嵌软字幕，目前交付独立 SRT；
4. 将 Media 云 JobId 写入共享数据库，实现 Worker 重启后的跨进程恢复；
5. 实现真实火山 Media 后备；
6. 用 30 秒、2 分钟、5 分钟素材测量 p50/p95、TTS 二次生成率、合成耗时和实际账单。

## 接口安全

设置：

```text
CINEFLOW_MEDIA_BEARER_TOKEN=...
CINEFLOW_WORKER_BEARER_TOKEN=同一个值
```

除 `/healthz` 外，写接口验证 `Authorization: Bearer ...`。生产环境还应使用私网、安全组、mTLS 或服务网格身份，并限制 Base64 大小与调用速率。
