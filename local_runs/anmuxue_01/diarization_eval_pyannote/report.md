# Diarization Eval Report

- Subtitle lines: 101
- CSV: `E:/web/pyvideotrans/local_runs/anmuxue_01/diarization_eval_pyannote/line_speaker_compare.csv`

## Summary

| Model | Lines | Speakers | Transitions | Seconds | Distribution |
|---|---:|---:|---:|---:|---|
| existing:01-fen-mp4 | 101/101 | 5 | 26 |  | spk0:20, spk1:33, spk2:11, spk3:14, spk4:23 |
| pyannote | 101/101 | 7 | 26 | 354.45 | spk0:18, spk1:23, spk2:12, spk3:12, spk4:14, spk5:5, spk6:17 |

## Pairwise Disagreement

| Pair | Different / Compared | Ratio |
|---|---:|---:|
| existing:01-fen-mp4 vs pyannote | 60/101 | 59.41% |
