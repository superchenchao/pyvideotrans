# 云端 ASR Worker

CineFlow 的 ASR 已实现两个独立适配器，统一输出 `Transcript`：

1. **首选：阿里云 Fun-ASR 非实时文件转写**；
2. **后备：火山引擎豆包语音大模型极速版**。

控制平面只调用 `/v1/transcribe`，不会知道厂商私有返回格式。两套 Worker 可以分别部署，首选端点失败后由控制平面自动调用后备端点。

## 为什么默认使用阿里云 Fun-ASR

- 可以直接读取已有 OSS 签名 URL，不需要把原视频再次上传给控制平面；
- 支持异步文件任务；
- 返回句级和词级时间戳；
- 开启 `diarization_enabled` 后返回 `speaker_id`；
- `speaker_count` 只在用户已经知道角色数量时发送；
- ASR 返回的 `speaker_id` 会直接进入 CineFusion，作为音频侧第一证据。

提交、轮询和结果下载都由 `cineflow.providers.aliyun_asr` 完成。

## 火山引擎后备

`cineflow.providers.volcengine_asr` 使用豆包语音大模型极速版：

- 下载 `source_audio_url`、`clean_video_url` 或 `input_url`；
- 在 Worker 内用 FFmpeg 转为 16kHz、单声道、64kbps MP3；
- 调用极速版接口；
- 开启 `show_utterances` 和 `enable_speaker_info`；
- 将 `additions.speaker` 归一化为 `spk0`、`spk1` 等标签；
- 网络或服务端临时故障会短重试。

火山接口当前需要把音频编码进请求体，因此它作为后备路径，而不是首选路径。

## 启动

### 阿里云

```bash
export CINEFLOW_ASR_BACKEND=aliyun
export CINEFLOW_ASR_ALIYUN_API_KEY='...'
export CINEFLOW_ASR_ALIYUN_REGION=cn-beijing
export CINEFLOW_ASR_ALIYUN_MODEL=fun-asr
uvicorn cineflow.asr_worker:app --host 0.0.0.0 --port 8091
```

有 Workspace ID 时可以设置：

```bash
export CINEFLOW_ASR_ALIYUN_WORKSPACE_ID='...'
```

也可以用 `CINEFLOW_ASR_ALIYUN_BASE_URL` 显式覆盖端点。

### 火山引擎

```bash
export CINEFLOW_ASR_BACKEND=volcengine
export CINEFLOW_ASR_VOLCENGINE_API_KEY='...'
uvicorn cineflow.asr_worker:app --host 0.0.0.0 --port 8092
```

旧账号也可以配置 AppID 和 Access Token：

```bash
export CINEFLOW_ASR_VOLCENGINE_APP_ID='...'
export CINEFLOW_ASR_VOLCENGINE_ACCESS_TOKEN='...'
```

### Docker Compose

```bash
docker compose --profile asr up -d asr-aliyun asr-volcengine
```

Compose 中：

- `asr-aliyun:8091` 是首选；
- `asr-volcengine:8091` 是后备；
- 主机端口分别映射为 `8091` 和 `8092`。

控制平面配置：

```text
CINEFLOW_ASR_WORKER_URL=http://asr-aliyun:8091
CINEFLOW_SECONDARY_ASR_WORKER_URL=http://asr-volcengine:8091
CINEFLOW_ASR_WORKER_TIMEOUT_SECONDS=300
```

300 秒是调用超时配置，不是整条任务的强制完成时间。控制平面不会在总处理时间达到 300 秒时取消任务。

## API 契约

### 健康检查

```http
GET /healthz
```

示例：

```json
{
  "ok": true,
  "healthy": true,
  "warm": true,
  "backend": "aliyun",
  "detail": "model=fun-asr, endpoint=https://dashscope.aliyuncs.com",
  "max_concurrency": 4
}
```

### 转写

```http
POST /v1/transcribe
Content-Type: application/json
```

输入为完整 `JobRequest`。Worker 按以下顺序选择媒体：

```text
source_audio_url
    ↓ 不存在
clean_video_url
    ↓ 不存在
input_url
```

统一输出示例：

```json
{
  "language": "zh-CN",
  "provider": "aliyun_fun_asr",
  "task_id": "task-id",
  "usage_seconds": 38.6,
  "metadata": {"request_id": "request-id"},
  "lines": [
    {
      "line_id": 1,
      "start_ms": 0,
      "end_ms": 1260,
      "text": "你怎么来了？",
      "speaker_id": "spk1",
      "words": [
        {
          "start_ms": 0,
          "end_ms": 310,
          "text": "你",
          "punctuation": ""
        }
      ]
    }
  ]
}
```

## 与 CineFusion 的关系

`CINEFLOW_SPEAKER_AUDIO_BACKEND=auto` 时：

1. 如果 ASR 返回了 `speaker_id`，直接把字幕区间转换为音频说话人时间段；
2. 再与 Light-ASD/LR-ASD 的主动说话人轨迹关联；
3. 只有 ASR 没有返回说话人标签时，才调用 pyannote；
4. `CINEFLOW_SPEAKER_AUDIO_BACKEND=pyannote` 可以强制进行 A/B 对照；
5. `CINEFLOW_SPEAKER_AUDIO_BACKEND=asr` 则要求 ASR 必须返回说话人标签。

这样默认路径优先使用云 API，避免每条任务都运行一遍本地音频说话人模型，同时保留 pyannote 作为质量兜底和评测基线。

## 生产注意事项

- 阿里云异步任务状态与任务 ID 应由外层持久化存储记录，方便进程重启后继续轮询；当前 Worker MVP 在单次 HTTP 请求内完成轮询；
- 火山后备路径需要下载媒体并转码，容器必须安装 FFmpeg；
- 不要在日志中输出 API Key、Access Token 或完整音频 Base64；
- 应记录 provider、task_id、trace_id、实际语音秒数、总耗时和说话人数量；
- 上线前分别测量阿里云和火山路径的 p50/p95，并用人工标注字幕验证说话人标签质量。
