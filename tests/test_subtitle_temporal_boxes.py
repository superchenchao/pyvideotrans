from videotrans.subtitle_removal.temporal_boxes import merge_boxes, merge_temporal_frame_boxes


def test_temporal_merge_expands_detection_to_neighbor_frames():
    result = merge_temporal_frame_boxes(
        {10: [(100, 200, 300, 340)]},
        lookbehind=2,
        lookahead=3,
        spatial_padding=0,
    )

    assert list(result) == [8, 9, 10, 11, 12, 13]
    assert result[8] == [(100, 200, 300, 340)]


def test_temporal_merge_unions_partial_ocr_boxes_across_frames():
    result = merge_temporal_frame_boxes(
        {
            20: [(100, 180, 300, 340)],
            21: [(160, 280, 302, 342)],
        },
        lookbehind=1,
        lookahead=1,
        spatial_padding=0,
    )

    assert result[20] == [(100, 280, 300, 342)]
    assert result[21] == [(100, 280, 300, 342)]


def test_merge_boxes_keeps_separate_subtitle_lines():
    result = merge_boxes(
        [
            (100, 200, 300, 340),
            (210, 320, 302, 342),
            (120, 300, 370, 410),
        ]
    )

    assert result == [(100, 320, 300, 342), (120, 300, 370, 410)]


def test_temporal_merge_clips_padding_to_frame():
    result = merge_temporal_frame_boxes(
        {1: [(1, 99, 2, 49)]},
        lookbehind=4,
        lookahead=0,
        last_frame=10,
        frame_size=(100, 50),
        spatial_padding=8,
    )

    assert result == {1: [(0, 100, 0, 50)]}
