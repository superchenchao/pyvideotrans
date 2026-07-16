from videotrans.configure.config import _preserve_saved_subtitle_region


def test_stale_empty_settings_do_not_erase_saved_subtitle_region():
    current = {
        "subtitle_removal_last_rect": "",
        "subtitle_removal_last_aspect_ratio": 0.0,
    }
    persisted = {
        "subtitle_removal_last_rect": "[0.05, 0.68, 0.9, 0.08]",
        "subtitle_removal_last_aspect_ratio": 0.5625,
    }

    result = _preserve_saved_subtitle_region(current, persisted)

    assert result["subtitle_removal_last_rect"] == persisted[
        "subtitle_removal_last_rect"
    ]
    assert result["subtitle_removal_last_aspect_ratio"] == 0.5625


def test_current_subtitle_region_is_not_replaced_by_older_disk_value():
    current = {
        "subtitle_removal_last_rect": "[0.1, 0.7, 0.8, 0.06]",
        "subtitle_removal_last_aspect_ratio": 0.5625,
    }
    persisted = {
        "subtitle_removal_last_rect": "[0.05, 0.68, 0.9, 0.08]",
        "subtitle_removal_last_aspect_ratio": 0.5625,
    }

    result = _preserve_saved_subtitle_region(current, persisted)

    assert result["subtitle_removal_last_rect"] == current[
        "subtitle_removal_last_rect"
    ]
