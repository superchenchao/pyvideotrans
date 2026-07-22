# CineFlow Cloud

面向中文短剧、影视片段和多角色内容的云端翻译配音系统。普通电脑只负责把视频上传到对象存储、提交任务、查看进度并下载成片；语音识别、多模态人物归因、DeepSeek 翻译、Azure TTS、音画对齐和合成都在云端执行。

## 产品硬约束

- 单条输入视频最长 **300 秒**；
- 严格 SLA 从“文件已上传且 `POST /v1/jobs` 返回 202”开始；
- 已接受任务最多 **300 秒**进入成功、降级成功、失败或超时终态；
- 准入预测必须保留至少 20 秒安全余量，默认五分钟输入 p95 预测为 256 秒；
- 没有空闲预热槽位、GPU 未预热、必要 Worker 不健康或预计费用超预算时直接拒绝，不让已接受任务排队；
- 默认单目标语言、1080p 以内、输入文件不超过 512MB；
- 上传耗时不计入处理 SLA，因为公网带宽不受服务端控制。

这套机制不是口头承诺。代码中同时存在：输入限制、p95 预测、实时健康检查、无排队容量门、分阶段截止点、总截止时间和费用硬门禁。

## 说话人方案

不再把 ali_CAM 作为唯一决策模型。CineFusion Worker 将以下证据统一到逐行字幕：

1. pyannote Community-1 音频说话人分段；
2. Light-ASD 或 LR-ASD 主动说话人得分；
3. ArcFace 等跨镜头人脸身份聚类；
4. 画外音、重叠说话和音画同步状态；
5. 可选的文本证据；
6. 控制平面的动态权重与 Viterbi 式序列解码。

GPU Worker 已包含 pyannote 预热、音频说话人与人脸身份关联、逐字幕证据生成和外部主动说话人命令适配器。Light-ASD/LR-ASD 模型本体不复制进本仓库，部署时按其许可证挂载并通过命令输出统一 JSON。

## 翻译与配音

- 主翻译：DeepSeek，使用 JSON Output、固定行号、固定顺序、术语表和角色名上下文；
- 默认关闭思考模式，避免普通字幕翻译增加延迟；
- 配音：Azure Speech REST TTS；
- 支持 `character_voices`，同一目标语言内每个角色使用不同 Azure 音色；
- `target_voice` 作为未单独配置角色的后备音色；
- Azure 支持主区域和第二区域故障切换、并发上限、429/5xx 短重试；
- SSML 只使用一层 `<prosody>`，避免重复嵌套。

## 当前仓库包含

- FastAPI 控制平面；
- `/v1/admission` 运行前耗时和费用预估；
- 五分钟 SLA 准入与无排队容量槽；
- `/readyz` Worker 健康与预热检查；
- SSE 任务进度流；
- 多模态动态融合与序列解码；
- DeepSeek JSON 字幕翻译客户端；
- Azure Speech REST TTS、角色音色映射和区域容灾；
- Media、ASR、CineFusion Worker HTTP 契约；
- 可部署的 CineFusion GPU Worker；
- Demo Provider 和自动化测试；
- Docker、GPU Docker、Kubernetes 示例和 SLA 验收脚本。

> 这是可运行的控制平面与 CineFusion Worker MVP。生产上线仍需要 Media Worker、ASR Worker、对象存储和真实 Light-ASD/LR-ASD 推理命令。只有真实素材在目标云实例上的 p95 小于 280 秒后，才应开启生产流量。

## 本地运行

```bash
cp .env.example .env
python -m pip install -e '.[dev]'
python -m pytest -q
uvicorn cineflow.api:app --host 0.0.0.0 --port 8080
```

默认 `CINEFLOW_MODE=demo`，不会调用付费 API。

### 先做准入检查

```bash
curl -X POST http://127.0.0.1:8080/v1/admission \
  -H 'Content-Type: application/json' \
  -d '{
    "input_url": "https://example.com/already-uploaded.mp4",
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
    "max_cost_cny": 5,
    "subtitle_mode": "soft",
    "multi_speaker": true,
    "strict_sla": true,
    "max_cost_cny": 5.0
  }'
```

返回中包含 `predicted_seconds`、`estimated_cost_cny` 和费用分项。

### 提交任务

把同一 JSON 提交到：

```bash
curl -X POST http://127.0.0.1:8080/v1/jobs \
  -H 'Content-Type: application/json' \
  --data @job.json
```

查询状态：

```bash
curl http://127.0.0.1:8080/v1/jobs/JOB_ID
```

订阅进度：

```bash
curl -N http://127.0.0.1:8080/v1/jobs/JOB_ID/events
```

## API

- `GET /healthz`：控制平面存活；
- `GET /readyz`：必要 Worker、模型预热和空闲槽位；
- `GET /v1/capacity`：当前执行槽位；
- `POST /v1/admission`：不占槽、不计费的准入预估；
- `POST /v1/jobs`：提交任务；
- `GET /v1/jobs/{job_id}`：任务状态；
- `GET /v1/jobs/{job_id}/events`：SSE 事件流；
- Swagger：`/docs`。

## 生产配置

```text
CINEFLOW_MODE=production
CINEFLOW_MEDIA_WORKER_URL=https://media-worker.internal
CINEFLOW_ASR_WORKER_URL=https://asr-worker.internal
CINEFLOW_SECONDARY_ASR_WORKER_URL=https://backup-asr.internal
CINEFLOW_SPEAKER_WORKER_URL=https://speaker-worker.internal
CINEFLOW_WORKER_BEARER_TOKEN=...

CINEFLOW_DEEPSEEK_API_KEY=...
CINEFLOW_DEEPSEEK_MODEL=deepseek-v4-flash

CINEFLOW_AZURE_SPEECH_KEY=...
CINEFLOW_AZURE_SPEECH_REGION=eastasia
CINEFLOW_AZURE_SPEECH_SECONDARY_KEY=...
CINEFLOW_AZURE_SPEECH_SECONDARY_REGION=southeastasia
```

CineFusion Worker：

```text
CINEFLOW_SPEAKER_MODE=production
CINEFLOW_SPEAKER_HF_TOKEN=hf_...
CINEFLOW_SPEAKER_PYANNOTE_MODEL=pyannote/speaker-diarization-community-1
CINEFLOW_SPEAKER_DEVICE=cuda
CINEFLOW_SPEAKER_ACTIVE_SPEAKER_COMMAND=python /opt/asd/infer_json.py --input {input} --output {output}
```

生产环境不得在接单后下载模型。Worker 启动时完成加载，`/healthz` 只有在 pyannote 和主动说话人命令都准备好时才返回 `warm=true`。

## 验收

```bash
python scripts/benchmark_sla.py \
  --api http://127.0.0.1:8080 \
  --input-url https://example.com/five-minutes.mp4 \
  --duration 300 \
  --input-bytes 180000000 \
  --target-language en-US \
  --voice en-US-AvaMultilingualNeural
```

上线门槛不是单次跑进 300 秒，而是代表性视频矩阵的 p95 小于 280 秒，同时说话人准确率达到既定基线。

更多内容见：

- `docs/architecture.md`
- `docs/sla.md`
- `docs/speaker-worker.md`
- `docs/deployment.md`


## CineFusion Worker

Demo：

```bash
uvicorn cineflow.speaker_worker:app --port 8090
```

生产模式需要预先下载 pyannote 模型，并设置：

```text
CINEFLOW_SPEAKER_MODE=production
CINEFLOW_SPEAKER_HF_TOKEN=...
CINEFLOW_SPEAKER_DEVICE=cuda
CINEFLOW_SPEAKER_ACTIVE_SPEAKER_COMMAND=python /models/light_asd/run.py --input {input} --output {output}
```

主动说话人命令必须输出 `docs/speaker-worker.md` 定义的 JSON。模型在容器启动时预热，任务期间不允许下载。


## SLA 基准脚本

上传真实测试视频后运行：

```bash
python scripts/benchmark_sla.py \
  --input-url 'https://...' \
  --duration 300 \
  --input-bytes 200000000 \
  --target-language en-US \
  --voice en-US-AvaMultilingualNeural
```

脚本先调用免费准入预检，再从任务接受时刻计时；超出 300 秒或返回失败终态会以非零状态退出。
