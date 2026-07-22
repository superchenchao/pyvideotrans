# 部署与容量

## 独立部署边界

本目录以后可直接作为新仓库根目录。生产服务只接收现有客户端生成的 OSS URL，不部署或导入 pyVideoTrans 的上传、字幕消除、GUI 和缓存代码。

## 推荐最小拓扑

- 控制平面：2 个 CPU 副本；
- Media Worker：首选阿里云 API 适配器，后备火山引擎适配器；
- 阿里云 ASR Worker：至少 2 个 CPU 副本，负责 Fun-ASR 提交、轮询和归一化；
- 火山 ASR Worker：至少 1 个 CPU 副本，安装 FFmpeg，作为故障和质量后备；
- CineFusion Worker：优先复用云 ASR speaker 标签；视觉主动说话人仍需要 GPU 或合适云能力；
- DeepSeek：默认字幕翻译和低置信度文本证据；
- Azure Speech：多角色、多语言 TTS；
- 对象存储：调用方已有 OSS 以及本服务生成的中间结果/成片；
- Redis Streams/PostgreSQL：替换当前进程内事件、队列和任务存储。

## ASR 拓扑

```text
CineFlow API
   │
   ├─ preferred  → cineflow-asr-aliyun:8091
   │                   └─ Alibaba Fun-ASR async API
   │
   └─ fallback   → cineflow-asr-volcengine:8091
                       └─ download + FFmpeg + Volcengine Flash API
```

控制平面配置：

```text
CINEFLOW_ASR_WORKER_URL=http://cineflow-asr-aliyun:8091
CINEFLOW_SECONDARY_ASR_WORKER_URL=http://cineflow-asr-volcengine:8091
CINEFLOW_ASR_WORKER_TIMEOUT_SECONDS=300
```

Worker 凭据配置见 `.env.example` 和 `docs/asr-worker.md`。

### Docker Compose

```bash
docker compose --profile asr up -d asr-aliyun asr-volcengine
```

### Kubernetes

```bash
kubectl apply -f deploy/kubernetes/asr-worker.yaml
```

镜像应由 `Dockerfile.asr` 构建。该镜像安装 FFmpeg，因此两套后端可以共用；阿里云路径通常不会使用 FFmpeg，火山路径会使用。

## 容量原则

`max_inflight_jobs` 是同时执行数，不是接单上限。超过该数的有效任务进入队列。

例如：

```text
Media 2 槽、ASR 4 槽、Speaker GPU 1 槽、Azure 并发足够
=> CINEFLOW_MAX_INFLIGHT_JOBS=1
=> 第 2 条任务显示 queued，第一条释放槽位后继续执行
```

提高全局并发前，必须确认最慢阶段能承受同等并发，否则只会增加 p95 和 API 限流。

ASR Worker 内部另有：

```text
CINEFLOW_ASR_MAX_CONCURRENCY=4
```

它限制单个 Worker 进程同时调用云 ASR 的任务数。副本数和厂商配额必须一起规划。

## 300 秒目标

预热和容量规划用于提高 300 秒内成功率，但：

- 不因无空闲槽拒绝；
- 不因预测超过目标拒绝；
- 不在 300 秒时取消；
- 超时后继续执行并记录 `target_exceeded=true`。

`ASR_WORKER_TIMEOUT_SECONDS` 是单次控制平面 HTTP 等待上限，不是整个视频任务的总截止时间。生产环境应把它设得足以覆盖厂商排队波动，并通过 p95 监控调整。

## Provider 部署顺序

首选地址和后备地址对应：

```text
CINEFLOW_MEDIA_WORKER_URL                 # 阿里云优先
CINEFLOW_SECONDARY_MEDIA_WORKER_URL       # 火山引擎后备
CINEFLOW_ASR_WORKER_URL                   # 阿里云 Fun-ASR
CINEFLOW_SECONDARY_ASR_WORKER_URL         # 火山大模型极速版
CINEFLOW_SPEAKER_WORKER_URL               # 云能力或自建主 Worker
CINEFLOW_SECONDARY_SPEAKER_WORKER_URL     # 其他后备
```

DeepSeek 与 Azure TTS 不参与上述替换顺序，按产品决策保留。

## 安全

- 阿里云和火山凭据只注入各自 Worker，不放进客户端；
- Worker 日志不得输出签名 URL 查询参数、API Key、Token 或音频 Base64；
- 内部 Worker 接口通过私网和 `CINEFLOW_WORKER_BEARER_TOKEN` 保护；
- OSS 签名 URL 的有效期必须覆盖排队、处理和重试时间；
- 使用 Kubernetes Secret 或云密钥服务管理凭据。

## 可观测性

每次 ASR 至少记录：

- `provider`；
- `task_id`；
- 阿里 `request_id` 或火山 `trace_id`；
- 总媒体秒数；
- 计费语音秒数；
- 字幕行数；
- speaker 数量；
- 提交、排队、轮询、下载、解析各阶段耗时；
- 是否发生首选到后备切换。

## 性能矩阵

至少测试：

- 30 秒、2 分钟、5 分钟；
- 720p、1080p、4K 输入；
- 纯音频 URL、干净视频 URL、原视频 URL；
- 2 人、4 人、8 人；
- 正脸对白、反打镜头、画外音、重叠说话；
- 中译英、中译日、中译西；
- 软字幕和硬字幕；
- 保留背景音与不保留背景音；
- Worker 预热、冷启动和阿里到火山故障切换。

记录 p50/p90/p95、队列等待、各阶段耗时、Azure 字符数、GPU 活跃秒数、`target_exceeded`、降级状态和说话人准确率。
