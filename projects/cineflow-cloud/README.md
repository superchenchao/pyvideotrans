# CineFlow Cloud

面向中文短剧、影视片段和多角色内容的独立云端翻译配音项目。

当前代码暂存在 `projects/cineflow-cloud`，但该目录可以直接整体移动为一个新仓库：

- 不导入 `videotrans`；
- 不读取父项目配置、缓存或数据库；
- 有自己的依赖、CLI、FastAPI 服务、Dockerfile、Kubernetes、测试、文档和 CI；
- OSS 上传、字幕消除、ASR、人物归因、翻译、TTS、音画对齐和合成都在本目录内实现。

## 完整流程

```text
原视频
  ↓
Upload Worker：STS + OSS 分片上传 + 断点续传 + 对象校验
  ↓
Subtitle Worker：阿里云 VideoDetext / Caca / 服务端本地模型
  ↓
Media Worker：音频抽取 + 可选 MusicDemix
  ↓
ASR Worker：阿里云 Fun-ASR，火山引擎后备
  ↓
CineFusion：云 ASR 说话人 + 主动说话人 + 人脸 + 文本软证据
  ↓
DeepSeek 字幕翻译
  ↓
Azure 多角色 TTS + 真实 WAV 时长测量 + 有界语速拟合
  ↓
阿里云 ICE Timeline 对齐、混音、字幕和最终合成
  ↓
私有 OSS 成片与 SRT
```

## 五分钟目标

输入视频最长 300 秒时，目标是**尽可能在任务提交后 300 秒内完成**，但 300 秒不是硬超时：

- 预测超过 300 秒仍然接收；
- 无空闲执行槽时排队，不返回 429；
- Worker 冷启动只产生性能警告；
- 处理超过 300 秒后继续完成；
- 成功任务仍为 `succeeded`，并用 `target_exceeded=true` 标记；
- 只有输入错误、费用超限或必要 Provider 完全不可用时才拒绝或失败。

原视频上传耗时不计入服务端 300 秒目标，因为公网带宽不受服务端控制。字幕消除和后续云任务会记录各阶段实际耗时，用真实素材优化 p50/p95。

## Provider 原则

除已经确定保留的 DeepSeek 和 Azure TTS 外：

```text
阿里云 API / Worker
        ↓ 不满足功能、效果或速度
火山引擎 API / Worker
        ↓ 仍不满足
自建云端 Worker
```

当前实际实现：

| 阶段 | 首选 | 后备 |
|---|---|---|
| 原视频上传 | 阿里云 STS + OSS | 可替换同契约对象存储 |
| 原字幕消除 | 阿里云 VideoDetext | Caca、服务端本地模型 |
| ASR | 阿里云 Fun-ASR | 火山引擎 BigModel Flash |
| 人物归因 | 云 ASR 标签 + CineFusion | pyannote + 外部主动说话人模型 |
| 翻译 | DeepSeek | 当前不自动切换 |
| 配音 | Azure TTS | Azure 第二地域 |
| 媒体处理与合成 | 阿里云 ICE | 控制平面保留后备 Worker 契约 |

## 1. 原视频上传

项目已包含：

```text
cineflow.upload_worker
cineflow.upload_client
```

能力包括：

- 服务端 `AssumeRole` 签发最小权限、短时效 STS；
- 客户端直接分片上传 OSS，不经过 API 服务转发大文件；
- 多线程上传；
- 本地 JSON 检查点；
- 网络中断后恢复已完成分片；
- STS 过期刷新；
- 服务端保存 multipart upload ID；
- 上传完成后 HEAD 校验大小、Content-Type、SHA-256 元数据和 ETag；
- 中止未完成分片；
- 清理旧会话和残留对象。

桌面端命令：

```bash
python -m pip install -e '.[aliyun]'

cineflow-upload "E:/video/episode-01.mp4" \
  --worker http://127.0.0.1:8094 \
  --project-id series-a \
  --part-size-mib 8 \
  --threads 4
```

同一命令再次执行会读取 `.cineflow-upload/` 检查点并续传。详见 `docs/upload-worker.md`。

## 2. 字幕消除

`cineflow.subtitle_worker` 支持：

```text
auto
aliyun
caca
local
```

默认自动顺序：

```text
aliyun,caca,local
```

能力包括：

- 阿里云 ICE `VideoDetext`；
- 归一化字幕区域和时间段；
- Caca 可配置提交、查询、取消路径及鉴权；
- 服务端本地模型命令适配；
- 云 JobId 持久化；
- Worker 重启后恢复未完成任务；
- 外部结果归一化复制到项目 OSS；
- `ffprobe` 校验时长、宽高、FPS、视频流和音轨；
- 失败结果清理；
- 成功输出和原片按策略清理。

创建任务示例：

```bash
curl -X POST http://127.0.0.1:8095/v1/subtitles/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "input_url": "https://signed-source-url",
    "provider": "auto",
    "regions": [{"x":0.05,"y":0.78,"width":0.90,"height":0.18}],
    "expected_duration_seconds": 300,
    "expected_width": 1920,
    "expected_height": 1080,
    "expected_fps": 25
  }'
```

详见 `docs/subtitle-worker.md`。

## 3. 云 ASR

### 阿里云 Fun-ASR

- 直接提交 OSS 或签名 URL；
- 异步提交、轮询和结果下载；
- 句级和词级时间戳；
- `speaker_id`；
- 可选说话人数提示；
- 返回任务 ID 和实际计费语音秒数。

### 火山引擎后备

- Worker 下载签名 URL；
- FFmpeg 转成 16kHz、单声道、64kbps MP3；
- 支持新版 API Key 和旧版 AppID + Access Token；
- 短重试；
- 句级时间戳、说话人标签和 trace ID。

默认：

```text
CINEFLOW_ASR_WORKER_URL=http://asr-aliyun:8091
CINEFLOW_SECONDARY_ASR_WORKER_URL=http://asr-volcengine:8091
```

详见 `docs/asr-worker.md`。

## 4. 人物归因

不再让 ali_CAM 单独决定角色。CineFusion 汇总：

1. 阿里云或火山 ASR 返回的说话人标签；
2. 标签缺失时的 pyannote Community-1 后备；
3. Light-ASD/LR-ASD 等主动说话人分数；
4. ArcFace 等跨镜头人脸身份；
5. 画外音、重叠说话和音画同步；
6. DeepSeek 低置信度文本软证据；
7. 动态权重和序列解码。

默认 `CINEFLOW_SPEAKER_AUDIO_BACKEND=auto`：优先使用云 ASR `speaker_id`，缺失时才运行 pyannote。主动说话人模型按许可证挂载，通过统一 JSON 命令适配器接入。

## 5. 翻译

当前默认且仅使用 DeepSeek：

```text
provider: deepseek
model: deepseek-v4-flash
base URL: https://api.deepseek.com/v1
max completion tokens: 65536
thinking: disabled
```

翻译客户端完全独立，不导入父项目。请求使用 JSON Output，并强制：

- 固定 `line_id`、行数和顺序；
- 不合并、不拆分字幕；
- 携带角色名和术语表；
- 根据原时间槽生成适合配音的简洁口语；
- 普通翻译关闭思考模式。

## 6. Azure 多角色 TTS

- `character_voices` 为角色指定目标语言音色；
- `target_voice` 是后备音色；
- 主区域和第二地域容灾；
- 有界并发和 429/5xx 短重试；
- 单层 SSML `<prosody>`；
- 输出 RIFF PCM WAV；
- 读取 WAV 帧数获得真实时长；
- 过长时按目标时间槽计算 SSML 语速并有界重试；
- 不裁掉句尾；
- 仍超时时保留完整音频、记录溢出并标记 `degraded`。

## 7. 阿里云 Media Worker

`cineflow.media_worker` 已实现：

- 没有 `source_audio_url` 时云端抽取音频；
- `MusicDemix`；
- Azure WAV 片段写入私有 OSS；
- 原视频音轨静音；
- 背景音混入；
- 按真实 TTS 时长放置台词；
- 重叠台词分配到多条音频轨；
- ICE 硬字幕；
- 软字幕单独交付 SRT；
- 成片写入私有 OSS并返回签名 URL、JobId 和元数据。

详见 `docs/media-worker.md`。

## 本地开发

```bash
cp .env.example .env
python -m pip install -e '.[dev]'
pytest -q
ruff check .
uvicorn cineflow.api:app --host 0.0.0.0 --port 8080
```

默认 `CINEFLOW_MODE=demo`，不会调用付费 API。

## Docker Compose

启动上传、字幕消除、Media 和 ASR：

```bash
docker compose --profile ingest --profile media --profile asr up -d \
  upload-aliyun subtitle-removal media-aliyun asr-aliyun asr-volcengine
```

需要自建 CineFusion GPU 时：

```bash
docker compose --profile gpu up -d speaker-worker
```

## 服务端口

| 服务 | 端口 |
|---|---:|
| 控制平面 | 8080 |
| CineFusion | 8090 |
| ASR | 8091 / 8092 |
| Media | 8093 |
| Upload | 8094 |
| Subtitle Removal | 8095 |

## 验证状态

自动化测试覆盖：

- 独立项目边界；
- STS 对象级策略；
- 上传会话、恢复状态、对象校验、中止和清理；
- 字幕 provider 路由、云任务恢复、输出验证和清理；
- 阿里云/火山 ASR 归一化；
- 多模态人物融合；
- DeepSeek 行号约束；
- Azure WAV 时长和语速拟合；
- ICE Timeline、MusicDemix、SRT 和最终输出；
- 软 300 秒目标和队列行为。

测试通过只证明本地契约与编排逻辑成立，不代表真实云账号已完成生产验收。

## 上线前仍需完成

- 用正式阿里云账号验证 STS、OSS、Fun-ASR、VideoDetext、MusicDemix 和 ICE Timeline 返回结构；
- 用当前 Caca 账号确认真实路径、鉴权和响应字段；
- 部署并评测 Light-ASD/LR-ASD 与人脸跟踪；
- 多副本部署时把 JSON 状态替换为 Redis/PostgreSQL 和分布式锁；
- 完成 DeepSeek 缩句后 Azure 再合成的最终闭环；
- 需要时增加 MP4 内嵌软字幕；
- 实现真实火山 Media 后备；
- 用 30 秒、2 分钟、5 分钟真实素材测量 p50/p95、人物准确率和实际账单。
