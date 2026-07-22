# CineFlow Cloud

面向中文短剧、影视片段和多角色内容的独立云端翻译配音服务。

当前暂时存放在 `projects/cineflow-cloud`，但可以直接整体移动为一个新仓库：

- 不导入 `videotrans`；
- 不读取父项目配置、缓存或数据库；
- 有自己的 `pyproject.toml`、Dockerfile、测试、文档和 CI；
- 父项目只需要把已有 OSS URL 作为普通 HTTP API 参数传入。

## 产品边界

### 继续沿用当前 pyVideoTrans 的部分

CineFlow **不重新实现**：

1. 原视频上传 OSS；
2. 私有 Bucket、STS、分片上传、断点续传和对象校验；
3. 本地、Caca 或阿里云 IMS 原字幕消除；
4. 字幕消除任务恢复、结果校验和上游 OSS 清理。

上游完成后提交：

```text
input_url          原视频 OSS URL
clean_video_url    可选，现有字幕消除流程的干净视频 URL
source_audio_url   可选，现有流程已经产生的音频 URL
```

### CineFlow 负责

- 云端音频准备和可选声伴分离；
- 中文 ASR；
- 多模态人物归因；
- DeepSeek 字幕翻译；
- Azure 多角色 TTS；
- 云端配音放置、字幕渲染和最终合成；
- 新生成文件的服务端 OSS 存储和结果交付。

服务端生成文件使用独立 OSS 前缀，不会覆盖或替换桌面端已有上传逻辑。

## 五分钟目标

输入视频最长 300 秒。目标是**尽可能在任务提交后 300 秒内完成**，但不是硬超时：

- 预测超过 300 秒仍然接收；
- 没有空闲槽位时排队，不返回 429；
- 冷 Worker 产生性能警告，不仅因冷启动拒绝；
- 超过 300 秒继续处理；
- 成功任务仍为 `succeeded`，并用 `target_exceeded=true` 标记；
- 只有输入错误、费用超限或必要 Provider 没有可用路径时才拒绝或失败。

上传耗时不计入该目标，因为公网带宽不受服务端控制。

## Provider 顺序

除已明确保留的 DeepSeek 和 Azure TTS 外：

```text
阿里云 API / Worker
        ↓ 不满足功能、效果或速度
火山引擎 API / Worker
        ↓ 仍不满足
自建云端 Worker
```

首选端点使用 `*_WORKER_URL`，后备端点使用 `SECONDARY_*_WORKER_URL`。

## 已实现：阿里云 Media Worker

`cineflow.media_worker` 已经实现真实的阿里云 ICE/OSS 适配，而不是接口占位。

### 云端准备

- 画面优先使用 `clean_video_url`；
- 音频优先复用 `source_audio_url`；
- 没有音频 URL 时用 ICE Timeline 抽取音频；
- `separate_background=true` 时调用 `MusicDemix`；
- 返回 `source_audio_url`、`vocal_url`、`background_url`、任务 ID 和降级信息。

### Azure TTS 片段存储

控制平面把 Azure 返回的单条音频交给 `/v1/artifacts/base64`，Worker 写入私有 OSS，并返回内部规范 URL和签名下载 URL。

### 最终合成

- 原视频音轨静音；
- 可选背景音按配置音量混入；
- Azure TTS 片段按字幕开始时间放入 Timeline；
- 重叠配音自动分配到多条音频轨；
- 硬字幕通过 ICE `SubtitleTracks` 渲染；
- 软字幕当前单独交付 SRT；
- 最终视频输出私有 OSS，并返回签名 URL、ICE JobId 和元数据。

默认部署：

```text
CINEFLOW_MEDIA_WORKER_URL=http://media-aliyun:8093
CINEFLOW_MEDIA_WORKER_TIMEOUT_SECONDS=900
```

详见 `docs/media-worker.md`。

## 已实现：云 ASR

### 首选：阿里云 Fun-ASR

- 直接提交 `source_audio_url`、`clean_video_url` 或 `input_url`；
- 异步提交、轮询和结果下载；
- 句级和词级时间戳；
- `speaker_id`；
- 可选 `speaker_count` 提示；
- 返回实际语音秒数和任务 ID。

### 后备：火山引擎豆包语音大模型极速版

- Worker 内下载签名 URL；
- FFmpeg 转为 16kHz、单声道、64kbps MP3；
- 支持新版 API Key 和旧版 AppID + Access Token；
- 短重试；
- 句级时间戳、说话人标签和 trace ID。

统一输出示例：

```json
{
  "language": "zh-CN",
  "provider": "aliyun_fun_asr",
  "task_id": "...",
  "usage_seconds": 42.5,
  "lines": [
    {
      "line_id": 1,
      "start_ms": 0,
      "end_ms": 1260,
      "text": "你怎么来了？",
      "speaker_id": "spk1",
      "words": []
    }
  ]
}
```

默认部署：

```text
CINEFLOW_ASR_WORKER_URL=http://asr-aliyun:8091
CINEFLOW_SECONDARY_ASR_WORKER_URL=http://asr-volcengine:8091
```

详见 `docs/asr-worker.md`。

## 翻译：固定 DeepSeek

当前版本不按目标语言切换引擎：

```text
provider: deepseek
model: deepseek-v4-flash
base URL: https://api.deepseek.com/v1
max completion tokens: 65536
thinking: disabled
```

代码是本项目自己的独立 HTTP 客户端，不导入父项目实现。翻译请求使用 JSON Output，并要求：

- 固定 `line_id`、行数和顺序；
- 不合并、不拆分字幕；
- 携带角色名和术语表；
- 根据原时间槽生成适合配音的简洁口语；
- 普通翻译默认关闭思考模式。

## 配音：保留 Azure TTS

- `character_voices` 为每个角色指定目标语言音色；
- `target_voice` 是后备音色；
- 主区域和备用区域容灾；
- 有界并发和 429/5xx 短重试；
- 单层 SSML `<prosody>`。

## 说话人方案

不再让 ali_CAM 单独决定角色。CineFusion 汇总：

1. 阿里云或火山 ASR 返回的说话人标签；
2. ASR 无标签时的 pyannote Community-1 后备；
3. Light-ASD/LR-ASD 主动说话人分数；
4. ArcFace 等跨镜头人脸身份；
5. 画外音、重叠说话和音画同步；
6. DeepSeek 低置信度文本软证据；
7. 动态权重和 Viterbi 式序列解码。

默认 `CINEFLOW_SPEAKER_AUDIO_BACKEND=auto`：优先使用云 ASR `speaker_id`，缺失时才运行 pyannote。

Light-ASD/LR-ASD 模型本体不复制进仓库，部署时按许可证挂载，并通过统一 JSON 命令适配器连接。

## 当前包含

- FastAPI 控制平面和 SSE；
- 软 300 秒性能目标、费用估算、有界并发和等待队列；
- 阿里云 ICE/OSS Media Worker；
- 阿里云 Fun-ASR Worker；
- 火山引擎 ASR 后备 Worker；
- 云 ASR 优先的 CineFusion Worker；
- DeepSeek JSON 翻译；
- Azure Speech REST TTS；
- Docker、Kubernetes、测试和性能脚本。

GitHub Actions 已验证包安装、Ruff 和完整 pytest 套件。该验证覆盖本地契约与编排逻辑，不代表真实云账号已完成端到端验收。

> 目前已落地 ASR 和阿里云 Media Worker。正式上线仍需要部署真实主动说话人模型、完成跨进程任务持久化、验证 ICE 的正式账号返回结构，并用代表性素材测量 p50/p95、人物准确率和实际账单。

## 本地运行

```bash
cp .env.example .env
python -m pip install -e '.[dev]'
python -m pytest -q
uvicorn cineflow.api:app --host 0.0.0.0 --port 8080
```

默认 `CINEFLOW_MODE=demo`，不会调用付费 API。

### 启动阿里云 Media Worker

```bash
python -m pip install -e '.[aliyun]'
CINEFLOW_MEDIA_ALIYUN_ACCESS_KEY_ID='...' \
CINEFLOW_MEDIA_ALIYUN_ACCESS_KEY_SECRET='...' \
CINEFLOW_MEDIA_OSS_BUCKET='your-private-bucket' \
uvicorn cineflow.media_worker:app --host 0.0.0.0 --port 8093
```

### 启动 ASR Worker

```bash
# 阿里云首选
CINEFLOW_ASR_BACKEND=aliyun \
CINEFLOW_ASR_ALIYUN_API_KEY='...' \
uvicorn cineflow.asr_worker:app --host 0.0.0.0 --port 8091

# 火山后备
CINEFLOW_ASR_BACKEND=volcengine \
CINEFLOW_ASR_VOLCENGINE_API_KEY='...' \
uvicorn cineflow.asr_worker:app --host 0.0.0.0 --port 8092
```

### Docker Compose

```bash
docker compose --profile media --profile asr up -d \
  media-aliyun asr-aliyun asr-volcengine
```

## 准入预估

```bash
curl -X POST http://127.0.0.1:8080/v1/admission \
  -H 'Content-Type: application/json' \
  -d '{
    "input_url": "https://example.com/oss-source.mp4",
    "clean_video_url": "https://example.com/oss-cleaned.mp4",
    "source_audio_url": "https://example.com/oss-source.wav",
    "probe": {
      "duration_seconds": 300,
      "input_bytes": 180000000,
      "codec": "h264",
      "width": 1920,
      "height": 1080,
      "fps": 25
    },
    "source_language": "zh-CN",
    "target_language": "en-US",
    "target_voice": "en-US-AvaMultilingualNeural",
    "character_voices": {
      "character_001": "en-US-AvaMultilingualNeural",
      "character_002": "en-US-AndrewMultilingualNeural"
    },
    "estimated_tts_characters": 3000,
    "max_cost_cny": 5.0,
    "subtitle_mode": "soft",
    "multi_speaker": true,
    "optimize_for_target": true
  }'
```

即使返回 `likely_within_target=false`，任务仍可正常提交并继续执行。

## 提交和查询

```bash
curl -X POST http://127.0.0.1:8080/v1/jobs \
  -H 'Content-Type: application/json' \
  --data @job.json

curl http://127.0.0.1:8080/v1/jobs/JOB_ID
curl -N http://127.0.0.1:8080/v1/jobs/JOB_ID/events
```

## API

- `GET /healthz`：控制平面和 Provider 状态；
- `GET /readyz`：Provider 是否可用；忙碌不代表不就绪；
- `GET /v1/capacity`：运行数、等待数和可用槽位；
- `POST /v1/admission`：免费预估；
- `POST /v1/jobs`：提交任务；
- `GET /v1/jobs/{job_id}`：任务状态；
- `GET /v1/jobs/{job_id}/events`：SSE；
- Swagger：`/docs`。

## 生产配置摘要

```text
CINEFLOW_MODE=production
CINEFLOW_TARGET_PROCESSING_SECONDS=300

CINEFLOW_MEDIA_WORKER_URL=http://media-aliyun:8093
CINEFLOW_ASR_WORKER_URL=http://asr-aliyun:8091
CINEFLOW_SECONDARY_ASR_WORKER_URL=http://asr-volcengine:8091
CINEFLOW_SPEAKER_WORKER_URL=http://speaker-worker:8090

CINEFLOW_DEEPSEEK_API_KEY=...
CINEFLOW_DEEPSEEK_MODEL=deepseek-v4-flash
CINEFLOW_DEEPSEEK_THINKING=false

CINEFLOW_AZURE_SPEECH_KEY=...
CINEFLOW_AZURE_SPEECH_REGION=eastasia
CINEFLOW_AZURE_SPEECH_SECONDARY_KEY=...
CINEFLOW_AZURE_SPEECH_SECONDARY_REGION=southeastasia
```

详细内容：

- `docs/architecture.md`
- `docs/media-worker.md`
- `docs/asr-worker.md`
- `docs/sla.md`
- `docs/speaker-worker.md`
- `docs/deployment.md`
