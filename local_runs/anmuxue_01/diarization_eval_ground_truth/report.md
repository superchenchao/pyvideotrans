# Diarization Eval Report

- Subtitle lines: 101
- CSV: `E:/web/pyvideotrans/local_runs/anmuxue_01/diarization_eval_ground_truth/line_speaker_compare.csv`

## Summary

| Model | Lines | Speakers | Transitions | Seconds | Distribution |
|---|---:|---:|---:|---:|---|
| ground_truth | 101/101 | 4 | 29 |  | chimuxue:35, qinyang:36, xuefan:11, youyou:18 |
| existing:diarization_eval | 101/101 | 28 | 35 |  | spk0:5, spk1:2, spk10:11, spk11:2, spk12:3, spk13:2, spk14:2, spk15:1, spk16:2, spk17:3, spk18:4, spk19:1, spk2:4, spk20:1, spk21:1, spk22:1, spk23:2, spk24:1, spk25:2, spk26:5, spk28:3, spk29:4, spk3:11, spk5:11, spk6:8, spk7:4, spk8:4, spk9:1 |
| existing:diarization_eval_ali_cam | 101/101 | 5 | 34 |  | spk0:19, spk1:36, spk2:15, spk3:30, spk4:1 |
| existing:diarization_eval_pyannote | 101/101 | 7 | 26 |  | spk0:18, spk1:23, spk2:12, spk3:12, spk4:14, spk5:5, spk6:17 |
| existing:diarization_eval_combined_with_aliyun | 101/101 | 8 | 32 |  | spk0:12, spk1:1, spk2:17, spk3:35, spk4:10, spk5:17, spk6:5, spk7:3 |

## Pairwise Label-Invariant Similarity

ARI and NMI ignore arbitrary speaker ID names. Higher is more similar; they do not prove accuracy without ground truth.

| Pair | Compared | ARI | NMI |
|---|---:|---:|---:|
| ground_truth vs existing:diarization_eval | 100 | 0.174 | 0.480 |
| ground_truth vs existing:diarization_eval_ali_cam | 100 | 0.708 | 0.718 |
| ground_truth vs existing:diarization_eval_pyannote | 100 | 0.426 | 0.584 |
| ground_truth vs existing:diarization_eval_combined_with_aliyun | 99 | 0.666 | 0.728 |
| existing:diarization_eval vs existing:diarization_eval_ali_cam | 101 | 0.185 | 0.492 |
| existing:diarization_eval vs existing:diarization_eval_pyannote | 101 | 0.371 | 0.696 |
| existing:diarization_eval vs existing:diarization_eval_combined_with_aliyun | 100 | 0.237 | 0.591 |
| existing:diarization_eval_ali_cam vs existing:diarization_eval_pyannote | 101 | 0.378 | 0.532 |
| existing:diarization_eval_ali_cam vs existing:diarization_eval_combined_with_aliyun | 100 | 0.529 | 0.592 |
| existing:diarization_eval_pyannote vs existing:diarization_eval_combined_with_aliyun | 100 | 0.430 | 0.569 |
