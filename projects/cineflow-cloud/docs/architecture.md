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
Upload Worker
  ├─ 持久化 session / multipart upload ID
  ├─ HEAD 校验大小、Content-Type、SHA-256 元数据
  ├─ 中止遗留分片
  └─ 返回规范 URL 与签名 URL
          │
          ▼
Subtitle Removal Worker
  ├─ 阿里云 ICE VideoDetext
  ├─ Caca API
  └─ 服务端本地字幕消除命令
          │
          ├─ 持久化外部 JobId
          ├─ Worker 重启恢复
          ├─ 结果复制到项目 OSS
          └─ ffprobe 校验时长/画面/FPS/音轨
          │
          ▼
CineFlow Control Plane
  ├─ Media Worker：音频抽取、MusicDemix
  ├─ ASR Worker：阿里云 Fun-ASR，火山后备
  └─ ASR 完成后并行
       ├─ CineFusion：speaker_id、主动说话人、人脸、上下文
       └─ DeepSeek：字幕翻译
              │
              ▼
       Azure 多角色 TTS
       ├─ RIFF PCM WAV
       ├─ 真实时长测量
       └─ 有界 SSML 语速拟合
              │
              ▼
       Alibaba ICE Timeline
       ├─ 原音轨静音
       ├─ 背景声混合
       ├─ 按真实时长放置多角色配音
       ├─ 硬字幕或独立 SRT
       └─ 私有 OSS 成片
```

## 服务边界

| 服务 | 默认端口 | 责任 |
|---|---:|---|
| Control Plane | 8080 | 费用预估、队列、编排、SSE、结果状态 |
| CineFusion Worker | 8090 | 音频与视觉人物证据、多模态融合 |
| ASR Worker | 8091/8092 | 阿里云 Fun-ASR 与火山后备 |
| Media Worker | 8093 | 音频准备、MusicDemix、Azure 片段存储、最终合成 |
| Upload Worker | 8094 | STS、上传会话、验证、中止、清理 |
| Subtitle Worker | 8095 | VideoDetext、Caca、本地模型、恢复和输出校验 |

每个 Worker 通过 HTTP 契约解耦，后续可独立扩缩容，也可以把文件状态存储替换为 Redis/PostgreSQL，而不改业务请求模型。

## 上传架构

### 为什么原片不经过控制平面

原片直接从桌面端上传 OSS：

- 避免控制平面双倍占用公网带宽；
- 避免大文件落到 API 节点临时磁盘；
- 允许 `oss2` 原生 multipart、并发和断点续传；
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

### 自动路由

默认：

```text
aliyun → caca → local
```

- `provider=auto`：选择第一个已配置后端；
- 指定 provider：不可用时直接报错，不静默换服务；
- Alibaba/Caca 外部 JobId 会写入状态文件；
- Worker 启动时扫描非终态任务并恢复；
- 本地命令无法从模型内部进度恢复，崩溃后会重新执行；
- 所有输出最终归一化到新项目自己的 OSS 前缀并统一校验。

## 云 Provider 优先级

除 DeepSeek 与 Azure TTS 是产品明确保留的例外外：

1. 阿里云 API 或 Worker；
2. 阿里能力、效果或速度不满足时使用火山引擎；
3. 两边都不合适时使用自建云端 Worker。

当前落地情况：

- ASR：阿里云 Fun-ASR 主路径，火山 BigModel Flash 后备；
- 字幕消除：阿里 VideoDetext 主路径，Caca 和本地后备；
- Media：阿里 ICE 已实现，火山 Media 后备仍待实现；
- Speaker：优先云 ASR speaker 标签，视觉主动说话人仍由可替换外部模型提供；
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

### 翻译流水线 Worker

- `POST /v1/prepare`
- `POST /v1/transcribe`
- `POST /v1/analyze`
- `POST /v1/artifacts/base64`
- `POST /v1/assemble`

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
- 视频已上传并创建处理任务后开始统计；
- 无执行槽时排队，不拒绝；
- 预测慢或 Worker 冷启动只产生警告；
- 超过 300 秒继续执行；
- 用阶段检查点、`target_exceeded` 和 p50/p90/p95衡量效果。

## 持久化策略

当前：

- Upload 和 Subtitle Worker 使用原子 JSON 文件，可挂载持久卷；
- 控制平面任务、事件和队列仍是进程内 MVP；
- Media/ASR 云 JobId 的跨进程恢复仍需继续完善。

生产多副本：

- PostgreSQL 保存任务、会话、外部 JobId、成本和结果；
- Redis Streams 或云消息队列承载任务事件；
- 分布式锁确保一个外部 JobId 只由一个 Worker 接管；
- OSS 生命周期只作为兜底，业务清理仍由任务状态驱动。
