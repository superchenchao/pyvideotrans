from scripts.subtitle_locate_worker import (
    has_stable_subtitle,
    has_subtitle_box,
    subtitle_text_candidates,
)


def _ocr_result(text, box, score=0.96):
    return {
        "rec_texts": [text],
        "rec_scores": [score],
        "rec_boxes": [box],
    }


def test_locator_accepts_horizontal_dialogue_subtitle():
    result = _ocr_result("你终于来了", [120, 80, 460, 130])

    assert has_subtitle_box(result, roi_width=640, roi_height=384)
    assert subtitle_text_candidates(result, 640, 384) == {"你终于来了"}


def test_locator_rejects_vertical_character_introduction():
    result = _ocr_result("陆淮舟", [280, 20, 330, 230])

    assert not has_subtitle_box(result, roi_width=640, roi_height=384)


def test_locator_rejects_low_confidence_false_positive():
    result = _ocr_result("系统", [120, 80, 460, 130], score=0.50)

    assert not has_subtitle_box(result, roi_width=640, roi_height=384)


def test_locator_requires_same_text_in_adjacent_samples():
    assert not has_stable_subtitle(set(), {"你终于来了"})
    assert not has_stable_subtitle({"人物介绍"}, {"你终于来了"})
    assert has_stable_subtitle({"你终于来了"}, {"你终于来了"})
