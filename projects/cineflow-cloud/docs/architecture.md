# 架构

CineFlow Cloud 是控制平面，不在用户电脑执行模型或整片编码。客户端先把视频直传 OSS，上传完成后才创建任务。

```text
客户端 → OSS → API admission gate
                   ├─ Media Worker：探测、去硬字幕、声伴分离（并行）
                   ├─ ASR Worker：中文时间轴
                   └─ ASR 完成后
                       ├─ CineFusion Worker
                       │   ├─ pyannote/其他音频分离
                       │   ├─ 人脸检测与跨镜头跟踪
                       │   ├─ Light-ASD 主动说话人
                       │   ├─ 人脸/声纹角色库
                       │   └─ 低置信度文本推理
                       └─ DeepSeek 翻译
                             ↓
                       多模态序列融合
                             ↓
                       Azure TTS 并发配音
                             ↓
                       Media Worker 对齐与合成 → OSS
```

## 为什么拆成预热 Worker

严格 SLA 不能等待 Serverless 冷启动、模型下载或共享队列。生产环境必须维持固定的预热容量。API 使用“无排队准入”：没有空闲槽位时直接返回 429，而不是接受后让任务等待。

## 外部 Worker 契约

- `POST /v1/prepare`：输入 `JobRequest`，返回 `MediaArtifacts`。
- `POST /v1/transcribe`：输入 `JobRequest`，返回 `Transcript`。
- `POST /v1/analyze`：输入任务和字幕，返回逐行 `LineEvidence`。
- `POST /v1/artifacts/base64`：保存 Azure TTS 生成的片段并返回 URL。
- `POST /v1/assemble`：完成配音对齐、背景音混合和视频合成。

说话人 Worker 返回的是证据，不直接决定人物。控制平面的 `fusion.py` 根据画内/画外、音画同步、重叠说话等状态动态调整权重，再用序列解码抑制短句误切。


## 当前 CineFusion Worker

`cineflow.speaker_worker` 已实现：

- pyannote Community-1 的预热与说话人时间段提取；
- 16kHz 单声道音频抽取；
- 外部 Light-ASD/人脸跟踪命令适配；
- 音频 speaker 与主动人脸的一对一关联；
- 按字幕时间窗生成 audio/visual/offscreen/overlap 证据。

主动说话人实现保持为外部命令，是为了可以在 Light-ASD、TalkNet 或后续模型之间做 A/B，而不改控制平面。

## 低置信度文本复核

CineFusion Worker 先返回音频和视觉证据。控制平面做一次初步融合，只把低置信度台词批量交给 DeepSeek。DeepSeek 只能在该行已有候选角色中分配软概率，不能创造新角色；调用失败时直接保留音视频证据，不阻断交付。

## 角色音色

任务可同时传入：

```json
{
  "target_voice": "en-US-AvaMultilingualNeural",
  "character_voices": {
    "character_001": "en-US-AvaMultilingualNeural",
    "character_002": "en-US-AndrewMultilingualNeural"
  }
}
```

`character_voices` 优先，`target_voice` 是后备。跨集角色库可在客户端或独立角色服务中维护，再随任务传入。
