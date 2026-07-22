# 字幕消除 Worker

`cineflow.subtitle_worker` 把原项目中的三类字幕消除能力纳入独立新项目：

1. 阿里云 ICE `VideoDetext`；
2. Caca API；
3. 服务端本地字幕消除命令。

任务、云 JobId、输出对象和校验结果都会持久化。Worker 重启后会扫描未完成任务并继续查询或重新执行，不要求桌面程序一直在线。

## 自动路由

请求中的 `provider` 支持：

```text
auto
aliyun
caca
local
```

`auto` 默认按以下顺序查找可用后端：

```text
aliyun,caca,local
```

可通过 `CINEFLOW_SUBTITLE_PROVIDER_ORDER` 修改。指定某个 provider 时，如果该 provider 未配置，任务会在产生付费调用前被拒绝，不会静默换成另一个后端。

## 创建任务

```http
POST /v1/subtitles/jobs
```

```json
{
  "input_url": "https://signed-source-video-url",
  "provider": "auto",
  "regions": [
    {"x": 0.05, "y": 0.78, "width": 0.90, "height": 0.18}
  ],
  "time_ranges": [
    {"start_seconds": 0, "end_seconds": 300}
  ],
  "expected_duration_seconds": 299.8,
  "expected_width": 1920,
  "expected_height": 1080,
  "expected_fps": 25,
  "validate_output": true,
  "cleanup_input": false
}
```

`regions` 使用 0～1 归一化坐标，不依赖输入分辨率。可以传多块区域和多个时间段。

任务立即返回 `202` 与 `job_id`，后台继续处理：

```text
accepted
→ submitting
→ running
→ validating
→ succeeded / degraded / failed
```

查询：

```http
GET /v1/subtitles/jobs/{job_id}
```

恢复：

```http
POST /v1/subtitles/jobs/{job_id}/resume
POST /v1/subtitles/recover
```

取消和清理：

```http
DELETE /v1/subtitles/jobs/{job_id}?delete_output=true&delete_input=false
POST /v1/subtitles/cleanup?older_than_seconds=604800
```

默认定期清理不会删除已成功的输出。只有显式设置 `delete_successful_outputs=true` 才会删除成功结果。

## 阿里云 VideoDetext

阿里云后端通过独立的 ICE Client 提交智能生产任务：

```text
FunctionName=VideoDetext
```

支持：

- `LimitRegion`：由归一化 `regions` 转换；
- `Time`：由 `time_ranges` 转换；
- 可配置 `ModelId`；
- 云任务轮询；
- 持久化外部 JobId；
- 输出统一写入 CineFlow 的字幕消除 OSS 前缀；
- Worker 重启后继续查询同一个 JobId。

默认模型配置：

```text
CINEFLOW_SUBTITLE_ALIYUN_DEFAULT_MODEL_ID=algo-video-detext-new
```

正式账号中如果模型 ID 或接口参数与地域版本不同，应通过环境变量覆盖，并先做一条短视频契约测试。

## Caca API

Caca 的部署接口可能不完全一致，因此适配器不把路径写死。以下均可配置：

```text
CINEFLOW_SUBTITLE_CACA_BASE_URL
CINEFLOW_SUBTITLE_CACA_API_KEY
CINEFLOW_SUBTITLE_CACA_SUBMIT_PATH
CINEFLOW_SUBTITLE_CACA_STATUS_PATH
CINEFLOW_SUBTITLE_CACA_CANCEL_PATH
CINEFLOW_SUBTITLE_CACA_AUTH_HEADER
CINEFLOW_SUBTITLE_CACA_AUTH_SCHEME
```

适配器接受常见的嵌套字段变体：

- `task_id`、`taskId`、`job_id`、`id`；
- `status`、`state`、`job_status`；
- `output_url`、`result_url`、`video_url`、`download_url`。

Caca 返回的外部结果会复制到新项目自己的 OSS 结果前缀，再统一校验和签名。上线前必须使用当前账号的真实提交、查询、取消响应做契约测试，确认字段和鉴权方式。

## 本地字幕消除

本地后端运行在**服务端 Worker**，不是用户电脑。配置示例：

```text
CINEFLOW_SUBTITLE_LOCAL_COMMAND=python /opt/subtitle-models/run.py --input {input} --output {output} --regions {regions} --time-ranges {time_ranges}
```

可用占位符：

```text
{input}
{output}
{regions}
{time_ranges}
{workdir}
```

命令模板只允许由受信任的服务端运维人员配置，API 调用方不能传入命令。Worker 会下载签名输入、生成区域和时间段 JSON、运行命令、检查输出文件并上传 OSS。

本地任务中断后无法从模型内部进度继续，只能重新执行该任务；阿里云和 Caca 异步任务则会复用已保存的外部 JobId。

## 输出校验

启用 `validate_output=true` 时，Worker 使用 `ffprobe` 检查：

- 输出非空；
- 存在视频流；
- 时长与原片一致；
- 宽高一致；
- FPS 在容差范围内；
- 音轨是否存在；
- 视频编码信息。

严重不一致会标记失败，并可自动删除无效输出。没有音轨目前作为警告记录；是否将其升级为失败可以在后续按素材类型配置。

成功返回：

- 私有 OSS 签名输出 URL；
- 无查询参数的规范 URL；
- provider 和外部 JobId；
- 完整验证数据；
- 警告与元数据。

## 持久化和恢复

默认使用 `FileStateStore`：

```text
CINEFLOW_SUBTITLE_STATE_DIR=/var/lib/cineflow/subtitles
```

写入采用临时文件、`fsync` 和原子替换。单副本部署挂载持久卷后，容器重启可恢复任务。多副本生产环境应把同一接口替换为 Redis/PostgreSQL，并使用分布式任务锁，避免两个副本同时接管同一 JobId。

## 启动

```bash
python -m pip install -e '.[aliyun]'
uvicorn cineflow.subtitle_worker:app --host 0.0.0.0 --port 8095
```

Docker Compose：

```bash
docker compose --profile ingest up -d subtitle-removal
```

## 300 秒目标

字幕消除是全流程中最可能超出实时比的步骤之一。当前策略为：

- 尽量通过云端并行和预热在 300 秒内完成五分钟素材；
- 300 秒不是硬超时；
- 阿里云/Caca 云任务超过目标后继续轮询；
- 单个 provider 的 `job_timeout_seconds` 只是异常保护；
- 最终耗时通过任务指标记录，不因超过目标丢弃有效结果。
