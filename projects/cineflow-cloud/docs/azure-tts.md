# Azure TTS 配音时长拟合

CineFlow 保留 Azure Speech TTS 作为多语言、多角色配音服务。当前实现不再仅生成音频后按字幕开始时间盲放，而是测量每条语音的真实时长，并在必要时进行一次有界的 Azure SSML 语速重试。

## 输出格式

Azure REST 请求固定使用：

```text
X-Microsoft-OutputFormat: riff-24khz-16bit-mono-pcm
```

因此每条结果是标准 RIFF PCM WAV。CineFlow 读取 WAV 头中的采样率和帧数，计算真实 `duration_ms`，不使用字符数或文件大小估算时长。

生成文件使用 `.wav` 后缀，并通过阿里云 Media Worker 的 `/v1/artifacts/base64` 保存到私有 OSS。

## 拟合过程

对每条翻译字幕：

```text
target_duration_ms = end_ms - start_ms
```

流程：

1. 使用 `rate=+0%` 调用 Azure；
2. 读取实际 WAV 时长；
3. 若在容差内，直接使用；
4. 若过长，根据 `actual / target` 计算需要的加速比例；
5. 将比例限制在配置的最大值内，用单层 `<prosody rate="+N%">` 再调用一次 Azure；
6. 再次测量；
7. 仍然过长时保留完整音频，不裁切，写入溢出元数据并继续合成。

这不是离线 time-stretch。它优先让 Azure 自己以更快的韵律重新生成，通常比对生成后音频做极端拉伸更自然。

## 配置

```text
CINEFLOW_AZURE_TTS_CONCURRENCY=8
CINEFLOW_AZURE_TTS_MAX_FIT_RATE_PERCENT=55
CINEFLOW_AZURE_TTS_DURATION_TOLERANCE_RATIO=1.08
CINEFLOW_AZURE_TTS_MAX_FIT_ATTEMPTS=2
CINEFLOW_AZURE_TTS_REQUEST_TIMEOUT_SECONDS=25
```

含义：

- `MAX_FIT_RATE_PERCENT`：允许的最大 Azure `prosody rate`，默认 `+55%`；
- `DURATION_TOLERANCE_RATIO`：目标槽位容差，默认 1.08，即实际时长不超过槽位的 108% 时视为可接受；
- `MAX_FIT_ATTEMPTS`：包括首次正常语速在内的最大生成次数，默认 2；
- `REQUEST_TIMEOUT_SECONDS`：单次 Azure 请求超时；
- `CONCURRENCY`：同一控制平面进程中的 Azure 并发上限。

不建议把最大语速直接设到 100%。应先用英语、日语、韩语、西班牙语、德语和法语的真实素材评估自然度。

## `DubbingClip` 时序字段

每条生成结果保存：

```json
{
  "line_id": 17,
  "character_id": "character_002",
  "audio_url": "https://.../line-17.wav",
  "duration_ms": 1620,
  "target_duration_ms": 1500,
  "rate_percent": 35,
  "timing_overflow_ms": 120,
  "within_target": true,
  "output_format": "riff-24khz-16bit-mono-pcm",
  "metadata": {
    "attempts": 2,
    "voice": "en-US-AvaMultilingualNeural"
  }
}
```

`within_target=true` 使用配置容差判断，因此可能仍有少量正溢出；`timing_overflow_ms` 始终按严格目标槽位计算。

## 无法拟合时

当达到最大语速或最大尝试次数后仍然超出容差：

- `within_target=false`；
- 记录真实 `duration_ms` 和 `timing_overflow_ms`；
- 触发 `timing_warning` SSE 事件；
- 最终任务可以继续，并以 `degraded` 成功状态交付；
- 音频不会被裁掉；
- ICE Timeline 根据真实时长分配重叠音频轨，避免后一条音频覆盖前一条。

后续仍可加入第三层处理：

1. 只对超时句调用 DeepSeek 进行更短的二次翻译；
2. 再次 Azure 合成；
3. 最后才使用质量受控的 post-TTS time-stretch。

当前 0.5.0 尚未自动执行这第三层。

## 区域容灾

Azure 主区域失败时，客户端会使用第二配置区域。每个区域对 429 和常见 5xx 状态最多做一次短重试；永久 4xx 错误直接切换到下一区域。

配置：

```text
CINEFLOW_AZURE_SPEECH_KEY=...
CINEFLOW_AZURE_SPEECH_REGION=eastasia
CINEFLOW_AZURE_SPEECH_SECONDARY_KEY=...
CINEFLOW_AZURE_SPEECH_SECONDARY_REGION=southeastasia
```

## 已测试范围

自动化测试覆盖：

- RIFF PCM WAV 时长解析；
- SSML 文本、音色、语言和语速转义；
- 首次音频过长后的第二次有界语速生成；
- 达到最大语速后保留完整音频和溢出信息；
- Azure 主/备区域切换；
- `.wav` OSS artifact 上传；
- `DubbingClip` 时序元数据；
- 未解决溢出的 `timing_warning` 和 `degraded` 状态。

真实上线仍需按目标语言和音色执行听感盲测，并统计：

- 首次即适配比例；
- 二次生成成功比例；
- 各语言平均加速率；
- 仍需缩句或 time-stretch 的字幕比例；
- Azure 字符费用和额外重试费用；
- 对 300 秒软目标的影响。
