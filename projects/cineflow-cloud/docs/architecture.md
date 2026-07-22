# 架构

CineFlow Cloud 是可独立移动的控制平面和 Worker 契约集合。它不导入父项目，也不在用户电脑运行模型或整片编码。

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
                        ├─ Media Worker：探测、音频准备、可选声伴分离
                        ├─ ASR Worker：中文字幕时间轴
                        └─ ASR 完成后并行
                            ├─ CineFusion Worker
                            │   ├─ pyannote/其他音频分离
                            │   ├─ 人脸检测与跨镜头跟踪
                            │   ├─ Light-ASD 主动说话人
                            │   ├─ 人脸/声纹角色库
                            │   └─ 低置信度文本证据
                            └─ DeepSeek 翻译
                                  ↓
                            多模态序列融合
                                  ↓
                            Azure TTS 并发配音
                                  ↓
                            Media Worker 对齐与合成 → OSS
```

`clean_video_url` 存在时，媒体和最终合成优先使用现有字幕消除流程产生的干净视频；不存在时使用 `input_url`。

## Provider 优先级

Media、ASR 和 Speaker 均采用相同的首选/后备契约：

1. `*_WORKER_URL`：首选，部署时优先连接阿里云 API 适配器；
2. `SECONDARY_*_WORKER_URL`：后备，阿里云不满足时连接火山引擎适配器；
3. 两者都不满足时，首选地址可指向自建云端 Worker。

控制平面依次调用，不把云厂商 SDK 写死到业务编排层。

翻译是明确的例外：当前版本固定使用独立 DeepSeek 客户端。配音固定保留 Azure TTS。

## 五分钟目标和队列

300 秒是性能目标而不是硬截止：

- 忙碌任务进入队列；
- 预测超时不拒绝；
- 冷 Worker 不拒绝；
- 超过 300 秒继续运行；
- 通过阶段检查点和 `target_exceeded` 记录性能。

预热 Worker 仍然重要，因为它能提高 300 秒内成功率，但它不再是接单的硬前提。

## 外部 Worker 契约

- `POST /v1/prepare`：输入 `JobRequest`，返回 `MediaArtifacts`；
- `POST /v1/transcribe`：输入 `JobRequest`，返回 `Transcript`；
- `POST /v1/analyze`：输入任务和字幕，返回逐行 `LineEvidence`；
- `POST /v1/artifacts/base64`：保存 Azure TTS 片段并返回 URL；
- `POST /v1/assemble`：完成配音对齐、背景音混合和视频合成。

说话人 Worker 返回证据，不直接决定人物。控制平面的 `fusion.py` 根据画内/画外、音画同步、重叠说话等状态动态调整权重，再用序列解码抑制短句误切。

## 当前 CineFusion Worker

`cineflow.speaker_worker` 已实现：

- pyannote Community-1 预热与说话人时间段提取；
- 16kHz 单声道音频抽取；
- 外部 Light-ASD/人脸跟踪命令适配；
- 音频 speaker 与主动人脸的一对一关联；
- 按字幕时间窗生成 audio/visual/offscreen/overlap 证据。

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
