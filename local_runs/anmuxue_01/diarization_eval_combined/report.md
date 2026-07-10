# Diarization Eval Report

- Subtitle lines: 101
- CSV: `E:/web/pyvideotrans/local_runs/anmuxue_01/diarization_eval_combined/line_speaker_compare.csv`

## Summary

| Model | Lines | Speakers | Transitions | Seconds | Distribution |
|---|---:|---:|---:|---:|---|
| existing:01-fen-mp4 | 101/101 | 5 | 26 |  | spk0:20, spk1:33, spk2:11, spk3:14, spk4:23 |
| existing:diarization_eval | 101/101 | 28 | 35 |  | spk0:5, spk1:2, spk10:11, spk11:2, spk12:3, spk13:2, spk14:2, spk15:1, spk16:2, spk17:3, spk18:4, spk19:1, spk2:4, spk20:1, spk21:1, spk22:1, spk23:2, spk24:1, spk25:2, spk26:5, spk28:3, spk29:4, spk3:11, spk5:11, spk6:8, spk7:4, spk8:4, spk9:1 |
| existing:diarization_eval_ali_cam | 101/101 | 5 | 34 |  | spk0:19, spk1:36, spk2:15, spk3:30, spk4:1 |
| existing:diarization_eval_pyannote | 101/101 | 7 | 26 |  | spk0:18, spk1:23, spk2:12, spk3:12, spk4:14, spk5:5, spk6:17 |

## Pairwise Disagreement

| Pair | Different / Compared | Ratio |
|---|---:|---:|
| existing:01-fen-mp4 vs existing:diarization_eval | 99/101 | 98.02% |
| existing:01-fen-mp4 vs existing:diarization_eval_ali_cam | 67/101 | 66.34% |
| existing:01-fen-mp4 vs existing:diarization_eval_pyannote | 60/101 | 59.41% |
| existing:diarization_eval vs existing:diarization_eval_ali_cam | 96/101 | 95.05% |
| existing:diarization_eval vs existing:diarization_eval_pyannote | 80/101 | 79.21% |
| existing:diarization_eval_ali_cam vs existing:diarization_eval_pyannote | 82/101 | 81.19% |
