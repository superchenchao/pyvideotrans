# Changelog

## 0.5.0

- Changed Azure Speech output to RIFF 24kHz 16-bit mono PCM so every generated line has a measurable duration.
- Added bounded per-line subtitle-slot fitting with a second Azure synthesis request using SSML prosody rate when the first result is too long.
- Added configurable maximum rate, duration tolerance, fit-attempt count, and request timeout.
- Preserved regional failover, bounded concurrency, 429/5xx retries, and the compatibility method that returns raw audio bytes.
- Extended `DubbingClip` with actual duration, target duration, applied rate, residual overflow, fit state, output format, and provider metadata.
- Changed generated Azure artifacts from misleading `.mp3` names to `.wav` names.
- Made ICE Timeline overlap allocation use the measured Azure WAV duration.
- Added `timing_warning` events and degraded-success reporting when a line still exceeds its slot after the configured rate limit; the audio is preserved instead of clipped.
- Added WAV parsing, SSML escaping, fit retry, residual-overflow, regional failover, provider-integration, and orchestrator-warning tests.

## 0.4.0

- Added a standalone Alibaba ICE Media Worker with `/v1/prepare`, `/v1/artifacts/base64`, and `/v1/assemble`.
- Added cloud audio extraction through `SubmitMediaProducingJob` when the upstream client does not provide `source_audio_url`.
- Added Alibaba `MusicDemix` submission, polling, output discovery, vocal/background classification, and task metadata.
- Added a private OSS store for CineFlow-generated artifacts and final outputs without replacing the existing desktop OSS upload implementation.
- Added ICE Timeline assembly that mutes the original audio, mixes a configurable background track, schedules Azure TTS clips, allocates overlapping dialogue to multiple tracks, and supports hard subtitle rendering.
- Added separate SRT delivery for soft-subtitle mode and signed OSS URLs for final artifacts.
- Added Docker Compose and Kubernetes deployment examples for the Alibaba Media Worker.
- Extended media and output contracts with provider, task IDs, vocal URL, metadata, and degradation reporting.
- Added Media Worker contract, timeline, demix, output-normalization, artifact, SRT, and standalone-deployment tests.
- Replaced dynamic asyncio importing with direct `asyncio.to_thread` calls and made intelligent-production output parsing tolerate list, mapping, and JSON-string forms.
- Kept source OSS upload and original-subtitle removal outside this project.
- Validated package installation, Ruff, and the full pytest suite in GitHub Actions.
- Documented that persistent cloud job recovery, a real Volcengine Media fallback, and production p50/p95 measurements remain before launch.

## 0.3.0

- Added a standalone Alibaba Cloud Fun-ASR worker with asynchronous task polling, sentence timestamps, word timestamps, speaker labels, task metadata, and speech-duration usage.
- Added a standalone Volcengine BigModel Flash fallback worker with signed-URL download, FFmpeg audio preparation, retry handling, speaker labels, and trace metadata.
- Extended the normalized transcript contract with provider, task, usage, speaker, and word-timing fields.
- Changed CineFusion audio evidence to prefer cloud ASR speaker labels and use pyannote only as a fallback or explicit A/B backend.
- Added Docker Compose and Kubernetes deployment examples for separate Alibaba and Volcengine ASR services.
- Kept OSS upload and burned-subtitle removal outside this standalone project.
- Kept DeepSeek as the only/default translator and Azure Speech as the dubbing provider.
- Kept 300 seconds as a soft performance target rather than a hard cancellation deadline.
