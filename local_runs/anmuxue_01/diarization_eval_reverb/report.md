# Diarization Eval Report

- Subtitle lines: 101
- CSV: `E:/web/pyvideotrans/local_runs/anmuxue_01/diarization_eval_reverb/line_speaker_compare.csv`

## Summary

| Model | Lines | Speakers | Transitions | Seconds | Distribution |
|---|---:|---:|---:|---:|---|
| existing:01-fen-mp4 | 101/101 | 5 | 26 |  | spk0:20, spk1:33, spk2:11, spk3:14, spk4:23 |

## Failures

- `reverb`: 'NoneType' object is not callableTraceback (most recent call last):
  File "E:\web\pyvideotrans\videotrans\process\prepare_audio.py", line 503, in reverb_speakers
    diarizations = _get_diariz()
  File "E:\web\pyvideotrans\videotrans\process\prepare_audio.py", line 476, in _get_diariz
    diarization = pipeline({"waveform": waveform, "sample_rate": sample_rate})
TypeError: 'NoneType' object is not callable

