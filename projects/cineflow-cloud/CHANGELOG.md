# Changelog

## 0.3.0

- Added a standalone Alibaba Cloud Fun-ASR worker with asynchronous task polling, sentence timestamps, word timestamps, speaker labels, task metadata, and speech-duration usage.
- Added a standalone Volcengine BigModel Flash fallback worker with signed-URL download, FFmpeg audio preparation, retry handling, speaker labels, and trace metadata.
- Extended the normalized transcript contract with provider, task, usage, speaker, and word-timing fields.
- Changed CineFusion audio evidence to prefer cloud ASR speaker labels and use pyannote only as a fallback or explicit A/B backend.
- Added Docker Compose and Kubernetes deployment examples for separate Alibaba and Volcengine ASR services.
- Kept OSS upload and burned-subtitle removal outside this standalone project.
- Kept DeepSeek as the only/default translator and Azure Speech as the dubbing provider.
- Kept 300 seconds as a soft performance target rather than a hard cancellation deadline.
