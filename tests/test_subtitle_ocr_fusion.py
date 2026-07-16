from videotrans.subtitle_ocr.fusion import (
    _merge_continuous_ocr_fragments,
    _recover_repeated_asr_text,
    build_ocr_subtitles,
    fuse_ocr_with_asr,
)
from videotrans.task.taskcfg import SrtItem


def item(text, start, end):
    return SrtItem(text=text, start_time=start, end_time=end)


def test_build_ocr_subtitles_merges_stable_samples_and_rejects_lyrics():
    observations = [
        {"time_ms": 1000, "text": "我不是你老公", "score": 0.99},
        {"time_ms": 1250, "text": "我不是你老公", "score": 0.98},
        {"time_ms": 1500, "text": "我不是你老公", "score": 0.99},
        {"time_ms": 2000, "text": "you're mine", "score": 0.99},
        {"time_ms": 2250, "text": "you're mine", "score": 0.99},
        {"time_ms": 3000, "text": "乔乔", "score": 0.97},
        {"time_ms": 3250, "text": "乔乔", "score": 0.96},
    ]

    result = build_ocr_subtitles(observations, sample_ms=250)

    assert [entry["text"] for entry in result] == ["我不是你老公", "乔乔"]
    assert result[0]["start_time"] == 875
    assert result[0]["end_time"] == 1625


def test_build_ocr_subtitles_rejects_unstable_single_character_noise():
    result = build_ocr_subtitles([
        {"time_ms": 1000, "text": "一", "score": 0.99},
    ], sample_ms=250)

    assert result == []


def test_build_ocr_subtitles_keeps_adjacent_caption_contained_in_previous_text():
    observations = [
        {"time_ms": 174480, "text": "爸爸妈妈没吵架", "score": 0.99},
        {"time_ms": 174720, "text": "爸爸妈妈没吵架", "score": 0.99},
        {"time_ms": 174960, "text": "爸爸妈妈没吵架", "score": 0.99},
        {"time_ms": 175200, "text": "爸爸妈妈没吵架", "score": 0.99},
        {"time_ms": 175440, "text": "爸爸妈妈没吵架", "score": 0.99},
        {"time_ms": 175680, "text": "爸爸妈妈没吵架", "score": 0.99},
        {"time_ms": 176640, "text": "妈妈", "score": 1.0},
        {"time_ms": 176880, "text": "妈妈", "score": 1.0},
        {"time_ms": 177120, "text": "妈妈", "score": 1.0},
        {"time_ms": 177360, "text": "妈妈", "score": 1.0},
        {"time_ms": 177600, "text": "妈妈", "score": 1.0},
        {"time_ms": 177840, "text": "妈妈", "score": 1.0},
    ]

    result = build_ocr_subtitles(observations, sample_ms=240)

    assert [entry["text"] for entry in result] == ["爸爸妈妈没吵架", "妈妈"]
    assert result[0]["end_time"] < result[1]["start_time"]


def test_build_ocr_subtitles_keeps_identical_captions_separated_by_blank_frames():
    observations = [
        {"time_ms": time_ms, "text": "怎么可以", "score": 0.99}
        for time_ms in (204720, 204960, 205200, 205440, 205680, 205920)
    ] + [
        {"time_ms": time_ms, "text": "怎么可以", "score": 0.99}
        for time_ms in (206880, 207120, 207360, 207600, 207840, 208080)
    ]

    result = build_ocr_subtitles(observations, sample_ms=240)

    assert [entry["text"] for entry in result] == ["怎么可以", "怎么可以"]
    assert result[0]["end_time"] < result[1]["start_time"]


def test_build_ocr_subtitles_normalizes_traditional_ocr_variants():
    result = build_ocr_subtitles([
        {"time_ms": 1000, "text": "我沒有乱伦的癖好", "score": 0.99},
        {"time_ms": 1250, "text": "我沒有乱伦的癖好", "score": 0.99},
    ], sample_ms=250)

    assert result[0]["text"] == "我没有乱伦的癖好"


def test_fusion_uses_ocr_text_and_asr_timing_in_dominant_mode():
    asr = [
        item("许是卫一的继承人", 1000, 2500),
        item("许家木认真植物人", 3000, 4500),
        item("想一想不想", 9000, 10500),
    ]
    ocr = [
        item("许氏唯一的继承人", 1100, 2400),
        item("许嘉木变成植物人", 3100, 4400),
        item("股票一定会动荡的", 5000, 6200),
        item("让陆璟年戴上面具", 6500, 7600),
        item("和你结婚", 7800, 8600),
    ]

    result, stats = fuse_ocr_with_asr(asr, ocr)

    assert stats["mode"] == "ocr_dominant"
    assert [entry["text"] for entry in result] == [entry["text"] for entry in ocr]
    assert result[0]["start_time"] == 1000
    assert result[0]["end_time"] < result[1]["start_time"]
    assert "想一想不想" not in [entry["text"] for entry in result]
    assert stats["dropped_asr_only"] == 1


def test_fusion_falls_back_to_asr_when_ocr_track_is_too_sparse():
    asr = [item("旁白内容", 1000, 2000)]
    ocr = [item("标题", 0, 500)]

    result, stats = fuse_ocr_with_asr(asr, ocr)

    assert stats["mode"] == "asr"
    assert [entry["text"] for entry in result] == ["旁白内容"]


def test_fusion_rejects_unrelated_lower_third_titles():
    asr = [item("人物对白", 1000, 2000)]
    ocr = [
        item("第一章", 10000, 11000),
        item("第二章", 20000, 21000),
        item("第三章", 30000, 31000),
        item("第四章", 40000, 41000),
        item("第五章", 50000, 51000),
    ]

    result, stats = fuse_ocr_with_asr(asr, ocr)

    assert stats["mode"] == "asr"
    assert [entry["text"] for entry in result] == ["人物对白"]


def test_fusion_recovers_repeated_call_when_asr_cues_are_strongly_covered():
    ocr_item = item("悠悠", 209080, 210280)
    matching_asr = [
        item("悠悠", 209000, 209840),
        item("悠悠", 209840, 210320),
    ]

    assert _recover_repeated_asr_text(ocr_item, matching_asr) == "悠悠悠悠"


def test_fusion_does_not_repeat_partially_overlapping_asr_splits():
    ocr_item = item("悠悠", 199480, 200200)
    matching_asr = [
        item("悠悠", 199160, 199780),
        item("悠悠", 199780, 200800),
    ]

    assert _recover_repeated_asr_text(ocr_item, matching_asr) == "悠悠"


def test_fusion_preserves_expected_ending_captions():
    asr = [
        item("可以先看看悠悠的礼物吗", 195000, 197340),
        item("走开", 197340, 198020),
        item("悠悠", 199160, 199780),
        item("悠悠", 199780, 200800),
        item("悠悠", 209000, 209840),
        item("悠悠", 209840, 210320),
    ]
    ocr = [
        item("可以先看看悠悠的礼物吗", 195000, 197340),
        item("走开", 197340, 198020),
        item("悠悠", 199480, 200200),
        item("悠悠送给妈妈的生日礼物", 201160, 204280),
        item("怎么可以", 204600, 206080),
        item("怎么可以", 206760, 208120),
        item("悠悠", 209080, 210280),
    ]

    result, stats = fuse_ocr_with_asr(asr, ocr)

    assert stats["mode"] == "ocr_dominant"
    assert [entry["text"] for entry in result] == [
        "可以先看看悠悠的礼物吗",
        "走开",
        "悠悠",
        "悠悠送给妈妈的生日礼物",
        "怎么可以",
        "怎么可以",
        "悠悠悠悠",
    ]


def test_fusion_joins_continuous_ocr_fragments_with_narrow_evidence():
    ocr = [
        item("妈妈", 4680, 5400),
        item("可以先看看", 5400, 6600),
        item("悠悠的礼物吗", 6600, 7560),
        item("走开", 7560, 8520),
        item("悠悠送给妈妈的", 11160, 12600),
        item("生日礼物", 12600, 14280),
    ]
    asr = [
        item("妈妈", 4680, 5400),
        item("可以先看看悠悠的礼物吗", 5300, 7500),
        item("走开", 7560, 8520),
    ]

    result = _merge_continuous_ocr_fragments(ocr, asr)

    assert [entry["text"] for entry in result] == [
        "妈妈",
        "可以先看看悠悠的礼物吗",
        "走开",
        "悠悠送给妈妈的生日礼物",
    ]
