# Changelog

## 0.7.0

- Added `subtitle_recognition_mode=hybrid|asr|ocr`, with hybrid cloud ASR and cloud OCR as the default.
- Added an Alibaba ICE `CaptionExtraction` worker that submits the original OSS video URL and converts the returned SRT into the normalized `Transcript` contract.
- Added normalized OCR region, frame rate, language, track, and required/degradable behavior to `JobRequest`.
- Added deterministic ASR/OCR fusion: visible text and display timing come from OCR, speaker IDs and word timing come from overlapping ASR, and ASR-only narration/offscreen speech is retained.
- Added conflict metadata when OCR and ASR disagree instead of silently pretending both recognition paths match.
- Added preferred and secondary Caption Worker routing, health checks, timeouts, Docker, Kubernetes, configuration, and documentation.
- Explicitly excluded Whisper, Tesseract, PaddleOCR, OpenCV frame OCR, and local visual-recognition models from the default recognition pipeline.
- Changed the recommended subtitle-removal provider order to cloud-only `aliyun,caca`; the trusted server-side local remover remains an explicit opt-in and is not an OCR engine.
- Added configurable cloud OCR cost guardrails and recognition-aware provider-health checks.
- Added SRT parsing, Alibaba CaptionExtraction contract, Caption Worker API, OCR/ASR fusion, cost, health, and standalone-deployment tests.
- Kept OCR, ASR, and subtitle removal parallel where possible to improve the chance of finishing a five-minute video within the soft 300-second target.

## 0.6.0

- Added a standalone Alibaba STS broker that issues short-lived, object-scoped OSS credentials without exposing permanent AccessKeys to desktop clients.
- Added a resumable multipart OSS upload client with concurrent parts, local checkpoints, STS refresh, server-side multipart registration, abort, and resume.
- Added upload-session persistence, object-size and SHA-256 metadata validation, signed delivery URLs, stale-session cleanup, and partial-upload cleanup.
- Added a durable subtitle-removal worker with automatic Alibaba VideoDetext, Caca, and trusted server-side local-command routing.
- Added normalized subtitle regions and time ranges, Alibaba `LimitRegion`/`Time` conversion, configurable VideoDetext model ID, external JobId persistence, and restart recovery.
- Added a configurable Caca adapter with flexible task, status, output, authentication, and endpoint fields; real account contract validation remains required.
- Added a trusted local subtitle-removal command adapter with signed-input download, region/time JSON files, bounded execution, and OSS result upload.
- Added `ffprobe` output validation for non-empty video, duration, resolution, FPS, codec, and audio presence.
- Added cancellation, invalid-result deletion, input/output cleanup, and successful-output retention by default.
- Added atomic file-backed worker state, Dockerfiles, Docker Compose services, Kubernetes manifests, documentation, and tests for upload and subtitle-removal flows.
- Corrected the standalone product boundary: source upload, STS, multipart resume, object validation, local/Caca/Alibaba subtitle removal, recovery, and cleanup now belong to this project.
- Kept DeepSeek as the default/only translator, Azure Speech as multilingual role TTS, and 300 seconds as a soft processing target rather than a cancellation deadline.

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
- Added cloud audio extraction through `SubmitMediaProducingJob` when `source_audio_url` is absent.
- Added Alibaba `MusicDemix` submission, polling, output discovery, vocal/background classification, and task metadata.
- Added a private OSS store for generated artifacts and final outputs.
- Added ICE Timeline assembly that mutes original audio, mixes background, schedules Azure clips, allocates overlapping dialogue to multiple tracks, and renders hard subtitles.
- Added separate SRT delivery for soft-subtitle mode and signed OSS URLs for final artifacts.
- Added Docker Compose and Kubernetes deployment examples for the Alibaba Media Worker.
- Extended media and output contracts with provider, task IDs, vocal URL, metadata, and degradation reporting.
- Added Media Worker contract, timeline, demix, output-normalization, artifact, SRT, and standalone-deployment tests.

## 0.3.0

- Added a standalone Alibaba Cloud Fun-ASR worker with asynchronous polling, sentence timestamps, word timestamps, speaker labels, task metadata, and speech-duration usage.
- Added a standalone Volcengine BigModel Flash fallback with signed-URL download, FFmpeg audio preparation, retries, speaker labels, and trace metadata.
- Extended the normalized transcript contract with provider, task, usage, speaker, and word-timing fields.
- Changed CineFusion audio evidence to prefer cloud ASR speaker labels and use pyannote only as a fallback or explicit A/B backend.
- Added Docker Compose and Kubernetes deployment examples for separate Alibaba and Volcengine ASR services.
- Kept DeepSeek as the only/default translator and Azure Speech as the dubbing provider.
- Kept 300 seconds as a soft performance target rather than a hard cancellation deadline.
