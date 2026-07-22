# 架构

CineFlow Cloud 是可独立移动的控制平面和 Worker 集合。它不导入父项目，也不在用户电脑运行模型或整片编码。

## 上游边界

现有客户端继续负责：

```text
原视频
  ↓
现有 OSS 上传、STS、断点续传、对象校验
  ↓
可选：现有本地 / Caca / 阿里云 IMS 字幕消除
  ↓
input_url + clean_video_url + 可选 source_audio_url
  ↓
CineFlow Cloud
```

本项目只认识 URL 和 JSON，不认识父项目的类、配置文件、缓存目录或数据库。整个 `projects/cineflow-cloud` 目录可以直接移动到新仓库。

## 云端流水线

```text
已上传的 OSS URL → CineFlow API
                        ├─ Media Worker：音频准备、可选声伴分离
                        ├─ 首选 ASR：阿里云 Fun-ASR
                        │      └─ 句级/词级时间戳 + speaker_id
                        └─ 后备 ASR：火山引擎大模型极速版
                               └─ utterances + speaker 标签
                                      ↓
                                ASR 完成后并行
                             ┌────────┴─────────┐
                             │                  │
                      CineFusion Worker     DeepSeek 翻译
                             │
              ┌──────────────┼─────────────────┐
              │              │                 │
        ASR speaker_id   Light-ASD/LR-ASD   人脸身份/上下文
              │              │                 │
              └──────────────┴─────────────────┘
                                      ↓
                           动态融合 + 序列解码
                                      ↓
                           Azure TTS 并发配音
                                      ↓
                           Media Worker 对齐合成 → OSS
```

`clean_video_url` 存在时，人物视觉分析和最终合成优先使用现有字幕消除流程产生的干净视频；不存在时使用 `input_url`。ASR 按 `source_audio_url → clean_video_url → input_url` 选择输入。

## Provider 优先级

Media、ASR 和 Speaker 均采用首选/后备契约：

1. `*_WORKER_URL`：首选，优先连接阿里云 API 适配器；
2. `SECONDARY_*_WORKER_URL`：后备，连接火山引擎适配器；
3. 两者都不满足时，同一 HTTP 契约可以指向自建云端 Worker。

控制平面依次调用，不把厂商 SDK 写死到任务编排层。

翻译是明确的例外：当前版本固定使用独立 DeepSeek 客户端。配音固定保留 Azure TTS。

## ASR Worker

`cineflow.asr_worker` 是独立进程，环境变量决定后端：

```text
CINEFLOW_ASR_BACKEND=aliyun
CINEFLOW_ASR_BACKEND=volcengine
```

两套适配器都输出相同的 `Transcript`：

```text
language
provider
task_id
usage_seconds
metadata
lines[]
  ├─ line_id
  ├─ start_ms / end_ms
  ├─ text
  ├─ speaker_id
  └─ words[]
```

### 阿里云首选路径

```text
OSS 签名 URL
  ↓
提交 Fun-ASR 异步任务
  ↓
轮询 task_id
  ↓
下载 transcription_url
  ↓
归一化句子、词和 speaker_id
```

该路径无需在 CineFlow 控制平面下载原视频。

### 火山后备路径

```text
OSS 签名 URL
  ↓
ASR Worker 下载
  ↓
FFmpeg 16kHz / mono / 64kbps MP3
  ↓
豆包语音大模型极速版
  ↓
归一化 utterances 和 speaker
```

火山路径需要媒体下载和轻量转码，因此默认作为后备。

## 五分钟目标和队列

300 秒是性能目标而不是硬截止：

- 忙碌任务进入队列；
- 预测超时不拒绝；
- 冷 Worker 不因速度目标被拒绝；
- 超过 300 秒继续运行；
- 通过阶段检查点和 `target_exceeded` 记录性能。

预热 Worker 仍然重要，因为它能提高 300 秒内成功率，但它不再是接单的硬前提。

## 外部 Worker 契约

- `POST /v1/prepare`：输入 `JobRequest`，返回 `MediaArtifacts`；
- `POST /v1/transcribe`：输入 `JobRequest`，返回 `Transcript`；
- `POST /v1/analyze`：输入任务和字幕，返回逐行 `LineEvidence`；
- `POST /v1/artifacts/base64`：保存 Azure TTS 片段并返回 URL；
- `POST /v1/assemble`：完成配音对齐、背景音混合和视频合成。

## 当前 CineFusion Worker

说话人 Worker 返回证据，不直接决定人物。控制平面的 `fusion.py` 根据画内/画外、音画同步、重叠说话等状态动态调整权重，再用序列解码抑制短句误切。

`CINEFLOW_SPEAKER_AUDIO_BACKEND` 支持：

- `auto`：优先 ASR `speaker_id`，缺失时用 pyannote；
- `asr`：强制要求 ASR 提供人物标签；
- `pyannote`：强制使用 pyannote，供 A/B 和质量兜底。

当前实现包括：

- 云 ASR speaker 标签到音频时间段的转换；
- pyannote Community-1 后备；
- 外部 Light-ASD/LR-ASD/人脸跟踪命令适配；
- 音频 speaker 与主动人脸的一对一关联；
- 按字幕时间窗生成 audio/visual/offscreen/overlap 证据；
- `clean_video_url` 视觉输入优先；
- `source_audio_url` 音频复用。

主动说话人模型保持为外部命令，方便在 Light-ASD、LR-ASD、TalkNet 或后续模型之间做 A/B，而不修改控制平面。

## DeepSeek 低置信度复核

CineFusion Worker 先返回音频和视觉证据。控制平面做初步融合，只把低置信度台词批量交给 DeepSeek。DeepSeek 只能在该行已有候选角色中分配软概率，不能创造新角色；调用失败时保留音视频证据，不阻断交付。

字幕翻译同样由本项目自己的 DeepSeek HTTP 客户端完成，默认 `deepseek-v4-flash`、关闭思考模式、JSON Output。

## 角色音色

任务可传入：

```json
{
  "target_voice": "en-US-AvaMultilingualNeural",
  "character_voices": {
    "character_001": "en-US-AvaMultilingualNeural",
    "character_002": "en-US-AndrewMultilingualNeural"
  }
}
```

`character_voices` 优先，`target_voice` 是后备。跨集角色库可在独立角色服务或调用方维护，再随任务传入。
