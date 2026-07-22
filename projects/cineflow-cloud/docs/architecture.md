# 架构

CineFlow Cloud 是可以整体移动到独立仓库的完整视频翻译系统。它不导入父项目，不读取父项目配置、缓存或数据库；普通电脑只负责选择文件、上传、查看进度和下载结果。

## 完整数据流

```text
桌面端/CLI
  │
  ├─ 计算 SHA-256
  ├─ 向 Upload Worker 申请对象级 STS
  └─ 直接并发分片上传 OSS，可断点续传
          │
          ▼
      原视频 input_url
          │
   ┌──────┼──────────────┐
   │      │              │
   ▼      ▼              ▼
字幕消除  云 OCR          云 ASR
VideoDetext/Caca  CaptionExtraction  Fun-ASR/火山
   │      │              │
   │      └──────┬───────┘
   │             ▼
   │      OCR/ASR 字幕融合
   │      - OCR 可见文字和显示时间
   │      - ASR speaker_id 和词时间
   │      - ASR-only 旁白/画外音
   │             │
   │      ┌──────┴────────┐
   │      ▼               ▼
   │  CineFusion       DeepSeek 翻译
   │      │               │
   │      └──────┬────────┘
   │             ▼
   │       Azure 多角色 TTS
   │             │
   └─────────────┼──────────────┐
                 ▼              │
          Alibaba ICE Timeline  │
                 │              │
                 └────→ 私有 OSS 成片
```

## 服务边界

| 服务 | 默认端口 | 责任 |
|---|---:|---|
| Control Plane | 8080 | 费用预估、队列、编排、SSE、融合、结果状态 |
| CineFusion Worker | 8090 | 音频与视觉人物证据、多模态融合 |
| ASR Worker | 8091/8092 | 阿里云 Fun-ASR 与火山后备 |
| Media Worker | 8093 | 音频准备、MusicDemix、Azure 片段存储、最终合成 |
| Upload Worker | 8094 | STS、上传会话、验证、中止、清理 |
| Subtitle Worker | 8095 | VideoDetext、Caca、可选服务端移除器、恢复和校验 |
| Caption Worker | 8096 | 阿里云 CaptionExtraction 云 OCR、SRT 归一化 |

每个 Worker 通过 HTTP 契约解耦，后续可独立扩缩容，也可以把文件状态存储替换为 Redis/PostgreSQL，而不改业务请求模型。

## 上传架构

原片直接从桌面端上传 OSS：

- 避免控制平面双倍占用公网带宽；
- 避免大文件落到 API 节点临时磁盘；
- 允许 `oss2` multipart、并发和断点续传；
- 桌面端只持有短期、单对象权限；
- 上传会话和 upload ID 由服务端持久化，方便中止与清理。

客户端检查点保存：

```text
本地文件路径/大小/mtime
SHA-256
session_id
object_key
multipart upload_id
已完成 part_number → ETag
```

检查点不保存永久云密钥。STS 过期后，客户端刷新凭据并继续相同 `object_key` 和 multipart upload。

## 字幕消除架构

Subtitle Worker 使用统一 `SubtitleRemovalProvider` 契约：

```text
submit(request, job_id, output_object_key)
wait(submission)
cancel(submission)
```

推荐默认路由：

```text
aliyun → caca
```

`local` 仍可显式启用为服务端字幕移除命令，但不是 OCR 识别器，也不参与默认云路径。

- Alibaba/Caca 外部 JobId 写入状态文件；
- Worker 启动时扫描非终态任务并恢复；
- 所有输出归一化到新项目自己的 OSS 前缀；
- `ffprobe` 校验时长、画面、FPS 和音轨。

## API-only 字幕识别

默认：

```text
subtitle_recognition_mode=hybrid
```

识别任务在上传完成后与字幕消除并行：

```text
input_url        → Alibaba CaptionExtraction OCR
source_audio_url → Alibaba Fun-ASR / Volcengine ASR
clean_video_url  → 后续人物视觉分析和最终合成
```

OCR 必须读取原视频，因为 `clean_video_url` 已移除可见字幕。

识别阶段不会在客户端或控制平面运行：

- Whisper；
- Tesseract；
- PaddleOCR；
- OpenCV 全片抽帧 OCR；
- 本地视觉识别模型。

Caption Worker 直接提交 OSS URL 到阿里云 ICE，并在健康信息中返回 `local_ocr=false`。控制平面只解析服务商返回的 SRT/JSON。

### OCR/ASR 融合

`subtitle_recognition.py` 执行确定性融合：

1. 按时间重叠匹配 OCR 行与 ASR 行；
2. OCR 行保留画面文字和显示时间；
3. 从最佳 ASR 行继承 `speaker_id` 与词级时间；
4. 未被 OCR 覆盖的 ASR 行作为旁白或画外音加入；
5. 文字相似度过低时记录冲突行 ID；
6. 按时间排序并重建连续 `line_id`。

混合模式下：

- OCR 失败、ASR 成功：降级到 ASR；
- ASR 失败、OCR 成功：降级到 OCR；
- 两者都失败：任务失败；
- `ocr_required=true` 且 OCR 失败：任务失败。

## 云 Provider 优先级

除 DeepSeek 与 Azure TTS 是产品明确保留的例外外：

1. 阿里云 API 或 Worker；
2. 阿里能力、效果或速度不满足时使用火山引擎；
3. 两边都不合适时使用自建云端 Worker。

当前落地情况：

- OCR：阿里云 CaptionExtraction 主路径，保留 Secondary Caption Worker 契约；
- ASR：阿里云 Fun-ASR 主路径，火山 BigModel Flash 后备；
- 字幕消除：阿里 VideoDetext 主路径，Caca 后备；
- Media：阿里 ICE 已实现；
- Speaker：优先云 ASR speaker 标签，视觉主动说话人由可替换外部模型提供；
- 翻译：固定 DeepSeek；
- TTS：固定 Azure Speech，支持第二地域容灾。

## 标准契约

### Upload Worker

- `POST /v1/uploads/sessions`
- `POST /v1/uploads/{id}/credentials`
- `POST /v1/uploads/{id}/multipart`
- `POST /v1/uploads/{id}/validate`
- `POST /v1/uploads/{id}/complete`
- `DELETE /v1/uploads/{id}`
- `POST /v1/uploads/cleanup`

### Subtitle Worker

- `POST /v1/subtitles/jobs`
- `GET /v1/subtitles/jobs/{id}`
- `POST /v1/subtitles/jobs/{id}/resume`
- `DELETE /v1/subtitles/jobs/{id}`
- `POST /v1/subtitles/recover`
- `POST /v1/subtitles/cleanup`

### Recognition and translation Workers

- `POST /v1/extract`：云 OCR；
- `POST /v1/transcribe`：云 ASR；
- `POST /v1/analyze`：人物证据；
- `POST /v1/prepare`：媒体准备；
- `POST /v1/artifacts/base64`：保存 Azure 音频；
- `POST /v1/assemble`：最终合成。

## CineFusion

人物归因不是单一声纹模型的输出，而是证据融合：

- 云 ASR `speaker_id`；
- pyannote 后备音频分段；
- Light-ASD/LR-ASD/TalkNet 等主动说话人；
- 跨镜头人脸身份；
- 画外音、重叠说话和 AV 同步状态；
- 仅对低置信度句子调用 DeepSeek 文本软证据；
- 序列解码抑制短句误切。

DeepSeek 只能在已有候选角色中排序，不能创造角色 ID。

## 五分钟性能目标

300 秒是优化目标而不是截止时间：

- 上传耗时单独统计，不计入服务端处理目标；
- OCR、ASR 和字幕消除并行；
- 无执行槽时排队，不拒绝；
- 预测慢或 Worker 冷启动只产生警告；
- 超过 300 秒继续执行；
- 用阶段检查点、`target_exceeded` 和 p50/p90/p95 衡量效果。

## 持久化策略

当前：

- Upload 和 Subtitle Worker 使用原子 JSON 文件，可挂载持久卷；
- 控制平面任务、事件和队列仍是进程内 MVP；
- Media、ASR、Caption 云 JobId 的跨进程恢复仍需继续完善。

生产多副本：

- PostgreSQL 保存任务、会话、外部 JobId、成本和结果；
- Redis Streams 或云消息队列承载任务事件；
- 分布式锁确保一个外部 JobId 只由一个 Worker 接管；
- OSS 生命周期只作为兜底，业务清理仍由任务状态驱动。
