# 云端字幕识别：ASR + OCR

CineFlow 默认使用 `hybrid` 字幕识别：

```text
原视频 input_url ──→ 阿里云 CaptionExtraction OCR ──┐
                                                    ├─→ 时间轴/文本融合
音频/视频 URL ─────→ 阿里云 Fun-ASR / 火山 ASR ────┘
```

整个识别流程都在云端完成：

- 不在桌面运行 Whisper；
- 不在桌面或控制平面运行 Tesseract、PaddleOCR、OpenCV 抽帧 OCR；
- OCR Worker 直接把原视频 OSS URL 提交给阿里云 ICE；
- ASR Worker 直接提交 URL，或在火山后备路径的 Worker 内做轻量音频规范化；
- 控制平面只解析服务商返回的 SRT/JSON，并执行确定性的字幕融合。

## 为什么同时使用 ASR 和 OCR

OCR 与 ASR 提供的信息不同：

- OCR 更适合取得画面中已经烧录的准确字幕文字和显示时间；
- ASR 可以补充没有硬字幕的旁白、画外音和漏字；
- ASR 的 `speaker_id` 可以赋给同时间段的 OCR 字幕；
- 两者冲突时保留 OCR 可见文本，并在元数据中记录 ASR 文本和相似度，供后续复核。

默认融合策略：

1. 以 OCR 的文字和时间轴作为可见字幕主结果；
2. 从重叠度最高的 ASR 行继承 `speaker_id` 和词级时间；
3. 与任何 OCR 行覆盖不足的 ASR 行作为“仅音频台词”加入结果；
4. OCR 与 ASR 文本明显冲突时记录 `text_conflict_ocr_line_ids`；
5. 不调用本地识别模型解决冲突。

## JobRequest 参数

```json
{
  "subtitle_recognition_mode": "hybrid",
  "ocr_region": {
    "x": 0.05,
    "y": 0.72,
    "width": 0.90,
    "height": 0.22
  },
  "ocr_fps": 5,
  "ocr_language": "ch_ml",
  "ocr_track": "main",
  "ocr_required": false
}
```

模式：

- `hybrid`：默认，同时调用云 ASR 和云 OCR；
- `asr`：只使用云 ASR；
- `ocr`：只使用云 OCR；
- `ocr_required=true`：混合模式下 OCR 失败时任务失败，不降级到 ASR。

`ocr_region` 使用 0～1 的归一化坐标，并且必须针对**原视频**指定。OCR 必须发生在字幕消除之前，因此 OCR Worker 始终读取 `input_url`，不会读取 `clean_video_url`。

## 阿里云 Caption Worker

启动：

```bash
python -m pip install -e '.[aliyun]'
uvicorn cineflow.caption_worker:app --host 0.0.0.0 --port 8096
```

接口：

```text
GET  /healthz
POST /v1/extract
```

`POST /v1/extract` 接收完整 `JobRequest`，返回标准 `Transcript`。

Worker 调用：

```text
SubmitIProductionJob
FunctionName=CaptionExtraction
        ↓
QueryIProductionJob
        ↓
OutputUrls / OutputFiles
        ↓
下载 SRT 并转换为 Transcript
```

提交参数包括：

- `fps`：2～10；
- `lang`：`ch`、`en` 或 `ch_ml`；
- `roi`：由归一化 `ocr_region` 转换；
- `track`：默认 `main`；
- `sep=false`：输出标准字幕文本。

Worker 健康信息会明确返回：

```json
{
  "backend": "aliyun_caption_extraction_api",
  "local_ocr": false
}
```

## 控制平面配置

```text
CINEFLOW_CAPTION_WORKER_URL=http://caption-aliyun:8096
CINEFLOW_SECONDARY_CAPTION_WORKER_URL=
CINEFLOW_CAPTION_WORKER_TIMEOUT_SECONDS=300
```

首选 Worker 是阿里云实现。`SECONDARY_CAPTION_WORKER_URL` 使用相同接口契约，可在完成真实账号契约测试后指向火山引擎 OCR 字幕提取 Worker。控制平面本身不绑定厂商 SDK。

## Docker Compose

```bash
docker compose --profile recognition up -d \
  caption-aliyun asr-aliyun asr-volcengine
```

## Kubernetes

```bash
kubectl apply -f deploy/kubernetes/caption-worker.yaml
```

## 降级行为

混合模式下：

- OCR 失败、ASR 成功：继续使用 ASR，任务标记为降级；
- ASR 失败、OCR 成功：继续使用 OCR，人物识别可由 Speaker Worker 的其他音频后端补充；
- 两者都失败：任务失败；
- `ocr_required=true` 且 OCR 失败：任务失败；
- 超过 300 秒：继续运行，不因性能目标取消有效云任务。

## 费用

控制平面使用 `CINEFLOW_COST_OCR_PER_MINUTE` 做预算门禁。该值只是可配置估价，正式结算应读取云厂商账单和成功处理时长。
