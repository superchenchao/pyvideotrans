# 部署与容量

## 独立部署边界

本目录可以直接作为新仓库根目录。生产部署包含原视频上传、STS、字幕消除、翻译配音和最终合成，不依赖 pyVideoTrans 源码、配置或缓存。

普通电脑只需要：

- 上传 CLI 或后续桌面 UI；
- 查看任务进度；
- 人物与音色确认；
- 下载成片。

## 推荐最小拓扑

| 组件 | 最小建议 | 说明 |
|---|---:|---|
| API 控制平面 | 2 CPU 副本 | 编排、费用、SSE；生产需外置状态 |
| Upload Worker | 1 CPU 副本 + PVC | STS、上传会话、验证和清理 |
| Subtitle Worker | 1 CPU 副本 + PVC | VideoDetext/Caca 路由；本地模型档按需加 GPU |
| Alibaba Media Worker | 2 CPU 副本 | ICE 提交、MusicDemix、OSS 片段、最终合成 |
| Alibaba ASR Worker | 2 CPU 副本 | Fun-ASR 提交、轮询和归一化 |
| Volcengine ASR Worker | 1 CPU 副本 | 安装 FFmpeg，作为后备 |
| CineFusion Worker | 1 个预热 GPU 槽 | 主动说话人、人脸；云 ASR 标签可减少音频 GPU |
| PostgreSQL/Redis | 高可用 | 替换进程内队列与文件状态，支持多副本 |
| OSS | 私有 Bucket | source、subtitle-removal、generated 使用独立前缀 |

单副本 MVP 可以继续使用文件状态与持久卷；多副本前必须迁移到共享数据库与分布式锁。

## OSS 前缀规划

建议同一私有 Bucket 使用独立前缀：

```text
cineflow/sources/              原视频上传
cineflow/subtitle-removal/     去字幕结果
cineflow/generated/            音频、TTS、SRT、成片
```

分别授予最小 RAM 权限。Upload Worker 的 STS 只允许桌面端访问一个具体 `sources/.../filename` 对象；桌面端不获得列举整个 Bucket 或删除其他对象的权限。

## Upload Worker

拓扑：

```text
Desktop/CLI ── create session ──> Upload Worker
     │                                │
     │                         AssumeRole + durable state
     │                                │
     └──── direct multipart OSS <─────┘
```

配置：

```text
CINEFLOW_UPLOAD_ALIYUN_REGION=cn-beijing
CINEFLOW_UPLOAD_ALIYUN_ROLE_ARN=acs:ram::ACCOUNT:role/CineFlowUploader
CINEFLOW_UPLOAD_OSS_ENDPOINT=https://oss-cn-beijing.aliyuncs.com
CINEFLOW_UPLOAD_OSS_BUCKET=private-bucket
CINEFLOW_UPLOAD_OSS_SOURCE_PREFIX=cineflow/sources
CINEFLOW_UPLOAD_STATE_DIR=/var/lib/cineflow/uploads
CINEFLOW_UPLOAD_BEARER_TOKEN=...
```

Docker Compose：

```bash
docker compose --profile ingest up -d upload-aliyun
```

Kubernetes：

```bash
kubectl apply -f deploy/kubernetes/upload-worker.yaml
```

Upload Worker 默认一个副本和 `ReadWriteOnce` PVC。多副本时将会话状态放入 PostgreSQL，并使用共享幂等约束；不要让多个文件卷副本独立管理同一个 session。

## Subtitle Worker

默认 provider 顺序：

```text
aliyun,caca,local
```

配置：

```text
CINEFLOW_SUBTITLE_PROVIDER_ORDER=aliyun,caca,local
CINEFLOW_SUBTITLE_STATE_DIR=/var/lib/cineflow/subtitles
CINEFLOW_SUBTITLE_OSS_BUCKET=private-bucket
CINEFLOW_SUBTITLE_OSS_PREFIX=cineflow/subtitle-removal
CINEFLOW_SUBTITLE_BEARER_TOKEN=...
```

阿里云：

```text
CINEFLOW_SUBTITLE_ALIYUN_REGION=cn-beijing
CINEFLOW_SUBTITLE_ALIYUN_DEFAULT_MODEL_ID=algo-video-detext-new
```

Caca：

```text
CINEFLOW_SUBTITLE_CACA_BASE_URL=...
CINEFLOW_SUBTITLE_CACA_API_KEY=...
CINEFLOW_SUBTITLE_CACA_SUBMIT_PATH=...
CINEFLOW_SUBTITLE_CACA_STATUS_PATH=...
```

本地模型：

```text
CINEFLOW_SUBTITLE_LOCAL_COMMAND=python /opt/subtitle-models/run.py --input {input} --output {output} --regions {regions} --time-ranges {time_ranges}
```

Docker Compose：

```bash
docker compose --profile ingest up -d subtitle-removal
```

Kubernetes：

```bash
kubectl apply -f deploy/kubernetes/subtitle-worker.yaml
```

文件状态模式下保持一个副本。若本地字幕模型需要 GPU，应为 local provider 单独部署 GPU Worker，而不是让阿里/Caca 轮询进程占用 GPU 节点。

## Media 与 ASR 拓扑

```text
CineFlow API
   ├─ Media  → cineflow-media-aliyun:8093 → Alibaba ICE/OSS
   ├─ ASR 主 → cineflow-asr-aliyun:8091  → Fun-ASR
   └─ ASR 备 → cineflow-asr-volcengine:8091
                                      └─ download + FFmpeg + Volcengine Flash
```

控制平面配置：

```text
CINEFLOW_MEDIA_WORKER_URL=http://cineflow-media-aliyun:8093
CINEFLOW_MEDIA_WORKER_TIMEOUT_SECONDS=900
CINEFLOW_ASR_WORKER_URL=http://cineflow-asr-aliyun:8091
CINEFLOW_SECONDARY_ASR_WORKER_URL=http://cineflow-asr-volcengine:8091
CINEFLOW_ASR_WORKER_TIMEOUT_SECONDS=300
```

Docker Compose：

```bash
docker compose --profile media --profile asr up -d \
  media-aliyun asr-aliyun asr-volcengine
```

Kubernetes：

```bash
kubectl apply -f deploy/kubernetes/media-worker.yaml
kubectl apply -f deploy/kubernetes/asr-worker.yaml
```

## 一次启动 MVP

```bash
cp .env.example .env
# 填写 STS、OSS、VideoDetext、Fun-ASR、DeepSeek、Azure 等凭据

docker compose \
  --profile ingest \
  --profile media \
  --profile asr \
  up -d upload-aliyun subtitle-removal media-aliyun asr-aliyun asr-volcengine api
```

需要自建人物视觉模型时再启动：

```bash
docker compose --profile gpu up -d speaker-worker
```

## 容量原则

`CINEFLOW_MAX_INFLIGHT_JOBS` 控制翻译流水线同时执行数，不是接单上限；额外任务进入队列。

各 Worker 还有独立并发：

```text
CINEFLOW_UPLOAD_*            上传主要受 OSS 与客户端线程数限制
CINEFLOW_SUBTITLE_MAX_CONCURRENCY=2
CINEFLOW_MEDIA_MAX_CONCURRENCY=4
CINEFLOW_ASR_MAX_CONCURRENCY=4
CINEFLOW_AZURE_TTS_CONCURRENCY=8
```

容量应按最慢阶段规划。例如：

```text
Subtitle 2 槽、Media 2 槽、ASR 4 槽、Speaker GPU 1 槽
=> 同时完整处理任务先设为 1
```

提高并发前，应测试厂商配额、429/限流、GPU 显存、ICE Timeline 排队和 OSS 公网/内网带宽。

## 300 秒目标

预热和并行用于提高 300 秒内成功率，但：

- 上传时间单独统计；
- 不因槽位繁忙拒绝有效任务；
- 不因预测超过目标拒绝；
- 不在 300 秒时取消；
- 有效云任务超过目标后继续等待；
- 最终记录 `target_exceeded=true` 和阶段 p50/p90/p95。

Worker 的 HTTP timeout 和 provider job timeout 是异常保护，不是全流程硬截止。

## 安全

- 桌面端只持有单对象、短时效 STS；
- 服务端凭据使用实例 RAM 角色或密钥管理服务；
- 不使用阿里云主账号 AccessKey；
- Upload/Subtitle/Media/ASR/Speaker 接口只暴露在私网或受认证网关后；
- 服务间使用 mTLS、工作负载身份或高熵 bearer token；
- 日志不得输出 API Key、SecurityToken、签名 URL 查询参数或音频 Base64；
- Caca 返回文件必须复制到受控 OSS并重新验证；
- 本地命令模板只能由部署配置提供，不能由用户请求提供；
- OSS 生命周期规则只是兜底，业务清理必须依据持久化任务状态。

## 可观测性

每个任务至少记录：

- upload session、multipart parts、重试和上传字节；
- subtitle provider、外部 JobId、轮询时长、校验和清理；
- ASR provider、task ID、计费语音秒数和 speaker 数；
- DeepSeek Token；
- Azure 字符数、生成次数、应用语速和溢出；
- ICE/MusicDemix JobId 与实际处理时长；
- GPU 活跃秒数；
- 队列等待、每阶段耗时和首选/后备切换；
- 预计成本、实际成本和 `target_exceeded`。

## 性能矩阵

至少覆盖：

- 30 秒、2 分钟、5 分钟；
- 720p、1080p、4K 输入；
- 上传中断、STS 过期、重复完成、分片中止；
- local/Caca/VideoDetext 三种字幕后端；
- 纯音频、干净视频和原视频输入；
- 2 人、4 人、8 人；
- 正脸对白、反打镜头、画外音、重叠说话；
- 中译英、中译日、中译西；
- 软字幕和硬字幕；
- Worker 预热、冷启动和主备故障切换。

输出 p50/p90/p95、300 秒内完成率、人物准确率、实际账单和失败恢复率。
