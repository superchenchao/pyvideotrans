from cineflow.models import SubtitleLine, Transcript
from cineflow.subtitle_recognition import fuse_asr_and_ocr, parse_srt


def test_parse_cloud_ocr_srt_preserves_timing_and_multiline_text():
    transcript = parse_srt(
        """1
00:00:00,120 --> 00:00:01,450
你怎么来了？

2
00:00:02.000 --> 00:00:03.250
第一行
第二行
""",
        language="zh-CN",
        provider="aliyun_caption_extraction",
        task_id="ocr-job",
    )

    assert transcript.task_id == "ocr-job"
    assert transcript.lines[0].start_ms == 120
    assert transcript.lines[0].end_ms == 1450
    assert transcript.lines[1].text == "第一行\n第二行"
    assert all(line.source == "ocr" for line in transcript.lines)


def test_hybrid_prefers_visible_ocr_text_and_inherits_asr_speaker():
    asr = Transcript(
        language="zh-CN",
        provider="aliyun_fun_asr",
        task_id="asr-job",
        usage_seconds=3.2,
        lines=[
            SubtitleLine(
                line_id=1,
                start_ms=0,
                end_ms=1200,
                text="你怎么来啦",
                speaker_id="spk1",
                source="asr",
            ),
            SubtitleLine(
                line_id=2,
                start_ms=1600,
                end_ms=2300,
                text="画外音没有硬字幕",
                speaker_id="spk2",
                source="asr",
            ),
        ],
    )
    ocr = Transcript(
        language="zh-CN",
        provider="aliyun_caption_extraction",
        task_id="ocr-job",
        lines=[
            SubtitleLine(
                line_id=1,
                start_ms=80,
                end_ms=1260,
                text="你怎么来了？",
                source="ocr",
            )
        ],
    )

    fused = fuse_asr_and_ocr(asr, ocr)

    assert fused.provider == "hybrid_cloud_ocr_asr"
    assert fused.lines[0].text == "你怎么来了？"
    assert fused.lines[0].speaker_id == "spk1"
    assert fused.lines[0].source == "ocr"
    assert fused.lines[1].text == "画外音没有硬字幕"
    assert fused.lines[1].speaker_id == "spk2"
    assert fused.lines[1].source == "asr"
    assert fused.metadata["asr_only_line_count"] == 1


def test_hybrid_records_text_conflicts_without_using_a_local_model():
    asr = Transcript(
        language="zh-CN",
        lines=[SubtitleLine(line_id=1, start_ms=0, end_ms=1000, text="完全不同")],
    )
    ocr = Transcript(
        language="zh-CN",
        lines=[SubtitleLine(line_id=1, start_ms=0, end_ms=1000, text="字幕内容")],
    )

    fused = fuse_asr_and_ocr(asr, ocr)

    assert fused.metadata["text_conflict_ocr_line_ids"] == [1]
    assert fused.lines[0].metadata["recognition_source"] == "ocr"
