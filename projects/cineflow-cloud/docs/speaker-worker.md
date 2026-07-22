# CineFusion Worker 接口

## 音频侧来源

CineFusion 不再默认每条任务都运行 pyannote。`CINEFLOW_SPEAKER_AUDIO_BACKEND` 支持：

```text
auto       优先使用云 ASR speaker_id，缺失时回退 pyannote
asr        强制使用云 ASR speaker_id，没有标签就报错
pyannote   强制使用 pyannote，供质量 A/B 和兜底
```

默认：

```text
CINEFLOW_SPEAKER_AUDIO_BACKEND=auto
CINEFLOW_SPEAKER_ASR_TURN_CONFIDENCE=0.90
```

阿里云 Fun-ASR 或火山 ASR 返回的每条字幕如果包含 `speaker_id`，Worker 会直接转换为：

```json
{
  "start_ms": 1200,
  "end_ms": 2850,
  "speaker_id": "spk1",
  "confidence": 0.9
}
```

随后和主动说话人轨迹进行一对一身份关联。这样可以减少额外音频 GPU 推理，并让 ASR、字幕时间轴和说话人标签保持同源。

当 `auto` 模式下 ASR 没有返回任何 `speaker_id` 时：

1. 优先复用 `source_audio_url`；
2. 没有音频 URL 时，从 `clean_video_url` 或 `input_url` 抽取 16kHz 单声道 WAV；
3. 调用 pyannote Community-1；
4. 再和视觉轨迹关联。

## 主动说话人命令

环境变量 `CINEFLOW_SPEAKER_ACTIVE_SPEAKER_COMMAND` 是一个可信的服务端命令模板，支持：

- `{input}`：已下载的输入视频，优先为 `clean_video_url`；
- `{output}`：命令必须写入的 JSON；
- `{workdir}`：本次任务临时目录。

示例：

```text
python /models/light_asd/run.py --input {input} --output {output}
```

输出可以是数组，也可以是 `{ "tracks": [...] }`：

```json
{
  "tracks": [
    {
      "start_ms": 1200,
      "end_ms": 2850,
      "face_id": "character_001",
      "score": 0.94,
      "av_sync_confidence": 0.91
    }
  ]
}
```

`face_id` 必须是跨镜头身份聚类后的稳定 ID，不能是单帧检测框编号。推荐在主动说话人程序内部组合：

```text
SCRFD/RetinaFace → ArcFace → ByteTrack/轨迹合并 → Light-ASD/LR-ASD
```

## 关联逻辑

Worker 对每组音频 speaker 和视觉 face 计算：

```text
重叠毫秒 × 音频置信度 × 主动说话分数 × 音画同步置信度
```

再按总分做单集内一对一贪心关联。输出到每条字幕的证据包括：

```text
audio[]
visual[]
offscreen
overlap_speech
av_sync_confidence
```

控制平面随后进行动态权重融合和序列解码，而不是让单一音频模型直接决定最终人物。

## 画外音与重叠说话

- 没有视觉候选，或最高视觉得分低于阈值时，标记 `offscreen=true`；
- 同一字幕窗内有多个音频 speaker 且累计重叠明显超过字幕长度时，标记 `overlap_speech=true`；
- 画外音时控制平面提高音频权重；
- 重叠说话时降低单一 speaker 的确定性，并进入低置信度复核。

## 健康与预热

生产模式至少需要主动说话人命令可用。

- `audio_backend=asr`：不需要加载 pyannote；
- `audio_backend=auto`：没有 HF Token 时仍可使用 ASR 标签，但没有 pyannote 兜底；
- `audio_backend=auto` 且配置 HF Token：ASR 标签优先，pyannote 已预热待命；
- `audio_backend=pyannote`：必须配置 HF Token 并成功加载模型。

健康检查：

```json
{
  "ok": true,
  "warm": true,
  "detail": "cloud ASR diarization preferred; pyannote and active-speaker fallback ready",
  "backend": "auto+external-active-speaker"
}
```

300 秒现在是软性能目标。冷启动会产生性能风险，但不会因为达到 300 秒而主动取消正在执行的任务。

## 生产建议

- 用同一批人工标注视频分别测试 `asr`、`pyannote` 和 `auto`；
- 分开统计正脸、反打、画外音、电话音、喊叫、耳语、重叠说话；
- 记录 ASR provider、speaker 数量、主动人脸数量、关联矩阵和最终置信度；
- 低置信度行保留人脸缩略图和短音频供人工复核；
- 跨集角色身份需要独立角色库，不能把每次 ASR 返回的 `spk0` 当成永久人物 ID。
