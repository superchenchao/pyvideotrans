# CineFlow Cloud

面向中文短剧、影视片段和多角色内容的独立云端翻译配音服务。

当前暂时存放在 `projects/cineflow-cloud`，但它是一个**可直接整体移动到新仓库的独立项目**：

- 不导入 `videotrans`；
- 不读取父项目配置；
- 不复用父项目 Python 包；
- 有自己的 `pyproject.toml`、依赖、Dockerfile、测试、文档和 CI；
- 父项目只需把已上传的 OSS URL 作为普通 HTTP API 参数传进来。

## 已确认的产品边界

### 上游继续沿用现有实现

本项目**不重新实现**以下功能：

1. 原视频上传 OSS；
2. 私有 Bucket、STS、分片上传、断点续传和对象校验；
3. 当前项目已有的本地/Caca/阿里云 IMS 字幕消除；
4. 字幕消除任务恢复、结果校验和 OSS 清理。

上游处理结束后向 CineFlow 提交：

- `input_url`：现有 OSS 上传流程产生的原视频签名 URL；
- `clean_video_url`：可选，现有字幕消除流程产生的干净视频 URL；
- `source_audio_url`：可选，现有流程已经生成音频时可直接复用。

因此，把本目录以后移动到其他仓库不会破坏上传和字幕消除逻辑，也不会形成源码级耦合。

### 本项目负责

- 云端音频准备和可选声伴分离；
- 中文 ASR；
- 多模态人物归因；
- DeepSeek 字幕翻译；
- Azure 多角色 TTS；
- 音画对齐；
- 最终合成和结果交付。

## 五分钟目标

输入视频最长 300 秒。目标是**尽可能在任务提交后 300 秒内完成**，但 300 秒不是强制截止时间：

- 预测超过 300 秒仍然接受任务；
- 没有空闲槽位时进入队列，不返回 429；
- Worker 冷启动会产生性能警告，不会仅因此拒绝；
- 运行超过 300 秒后继续处理；
- 成功任务仍然是 `succeeded`，并通过 `target_exceeded=true` 标记目标未达到；
- 只有真实的输入错误、费用超限或 Provider 无可用路径时才拒绝/失败。

上传耗时不计入该性能目标，因为公网带宽不受本服务控制。

## Provider 顺序

除已明确保留的 DeepSeek 和 Azure TTS 外，部署时遵循：

```text
阿里云 API / Worker 适配器
        ↓ 不满足功能、效果或速度
火山引擎 API / Worker 适配器
        ↓ 仍不满足
自建云端 Worker
```

`MEDIA/ASR/SPEAKER_WORKER_URL` 是首选端点；对应的 `SECONDARY_*` 端点是后备端点。控制平面会先调用首选端点，失败后再调用后备端点。

## 已完成的云 ASR

当前已经实现真实的独立 ASR Worker，而不是只有接口占位：

### 首选：阿里云 Fun-ASR

- 直接提交 `source_audio_url`、`clean_video_url` 或 `input_url`；
- 异步提交、轮询和结果下载；
- 句级时间戳；
- 词级时间戳；
- `speaker_id`；
- 可选 `speaker_count` 提示；
- 返回实际计费语音秒数和任务 ID。

### 后备：火山引擎豆包语音大模型极速版

- Worker 内下载签名 URL；
- FFmpeg 转为 16kHz、单声道、64kbps MP3；
- 新版 API Key 或旧版 AppID + Access Token；
- 短重试；
- 句级时间戳与说话人标签；
- 保留 trace ID。

统一输出：

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

## 翻译：默认且仅使用 DeepSeek

当前版本不做按语言自动切换，统一先使用 DeepSeek：

```text
provider: deepseek
model: deepseek-v4-flash
base URL: https://api.deepseek.com/v1
max completion tokens: 65536
thinking: disabled
```

这组默认值与现有 pyVideoTrans 的 DeepSeek 通道保持一致，但代码是本项目自己的独立 HTTP 客户端，不会 import 父项目实现。

翻译请求使用 JSON Output，并要求：

- 固定 `line_id`；
- 固定行数和顺序；
- 不合并、不拆分字幕；
- 带角色名和术语表；
- 根据原时间槽生成适合配音的简洁口语；
- 普通翻译默认关闭思考模式以降低延迟。

## 配音：保留 Azure TTS

- 支持 `character_voices`，同一目标语言内每个角色使用不同 Azure 音色；
- `target_voice` 是没有单独角色配置时的后备；
- 支持主区域和备用区域；
- 支持并发、429/5xx 短重试；
- SSML 只使用一层 `<prosody>`。

## 说话人方案

不再把 ali_CAM 作为唯一决策模型。CineFusion Worker 汇总：

1. 阿里云/火山 ASR 返回的说话人标签；
2. ASR 不返回标签时的 pyannote Community-1 后备分段；
3. Light-ASD/LR-ASD 主动说话人分数；
4. ArcFace 等跨镜头人脸身份；
5. 画外音、重叠说话和音画同步状态；
6. DeepSeek 低置信度文本软证据；
7. 动态权重和 Viterbi 式序列解码。

默认 `CINEFLOW_SPEAKER_AUDIO_BACKEND=auto`：优先使用云 ASR `speaker_id`，只有云 ASR 没有返回人物标签时才运行 pyannote。这样减少 GPU 音频模型开销，同时保留质量兜底和 A/B 基线。

Light-ASD/LR-ASD 模型本体不复制进本仓库，部署时按许可证挂载，并通过统一 JSON 命令适配器连接。

## 当前包含

- FastAPI 控制平面；
- `/v1/admission` 耗时和费用预估；
- 软 300 秒性能目标；
- 有界并发和等待队列；
- `/readyz` 与 Worker 健康检查；
- SSE 任务进度；
- 阿里云 Fun-ASR Worker；
- 火山引擎 ASR 后备 Worker；
- 多模态融合与序列解码；
- 独立 DeepSeek JSON 翻译客户端；
- Azure Speech REST TTS；
- 可部署的 CineFusion GPU Worker；
- Docker、Kubernetes 示例和性能基准脚本。

> 当前已经落地真实 ASR 适配器。正式上线仍需完成 Media Worker、对象存储结果写入、主动说话人模型部署，并用真实凭据和代表性素材做 p50/p95 与人物准确率测试。

## 本地运行

```bash
cp .env.example .env
python -m pip install -e '.[dev]'
python -m pytest -q
uvicorn cineflow.api:app --host 0.0.0.0 --port 8080
```

默认 `CINEFLOW_MODE=demo`，不会调用付费 API。

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

或：

```bash
docker compose --profile asr up -d asr-aliyun asr-volcengine
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

返回：

- `predicted_seconds`；
- `target_seconds`；
- `likely_within_target`；
- `estimated_cost_cny`；
- `warnings`。

即使 `likely_within_target=false`，任务也可以正常提交并继续执行。

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

CINEFLOW_MEDIA_WORKER_URL=https://aliyun-media.internal
CINEFLOW_SECONDARY_MEDIA_WORKER_URL=https://volc-media.internal
CINEFLOW_ASR_WORKER_URL=https://aliyun-asr.internal
CINEFLOW_SECONDARY_ASR_WORKER_URL=https://volc-asr.internal
CINEFLOW_SPEAKER_WORKER_URL=https://speaker.internal

CINEFLOW_DEEPSEEK_API_KEY=...
CINEFLOW_DEEPSEEK_MODEL=deepseek-v4-flash
CINEFLOW_DEEPSEEK_THINKING=false

CINEFLOW_AZURE_SPEECH_KEY=...
CINEFLOW_AZURE_SPEECH_REGION=eastasia
CINEFLOW_AZURE_SPEECH_SECONDARY_KEY=...
CINEFLOW_AZURE_SPEECH_SECONDARY_REGION=southeastasia
```

详细内容见：

- `docs/architecture.md`
- `docs/asr-worker.md`
- `docs/sla.md`
- `docs/speaker-worker.md`
- `docs/deployment.md`
