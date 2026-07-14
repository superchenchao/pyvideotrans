import numpy as np

from videotrans.subtitle_removal.roi_detection import detect_boxes_in_areas, interpolate_sampled_boxes


def test_interpolate_sampled_boxes_fills_nearby_sample_gaps():
    result = interpolate_sampled_boxes(
        {1: [(10, 30, 40, 60)], 4: [(12, 32, 40, 60)]},
        sample_step=3,
    )

    assert list(result) == [1, 2, 3, 4]
    assert result[2] == [(10, 30, 40, 60)]


def test_interpolate_sampled_boxes_does_not_fill_large_gaps():
    result = interpolate_sampled_boxes(
        {1: [(10, 30, 40, 60)], 10: [(12, 32, 40, 60)]},
        sample_step=3,
    )

    assert list(result) == [1, 10]


def test_detect_boxes_in_areas_crops_and_maps_coordinates():
    image = np.zeros((100, 200, 3), dtype=np.uint8)
    received_shapes = []

    def detect(crop):
        received_shapes.append(crop.shape)
        return [(1, crop.shape[1] - 1, 2, crop.shape[0] - 2)]

    result = detect_boxes_in_areas(image, [(10, 30, 50, 90)], detect)

    assert received_shapes == [(20, 40, 3)]
    assert result == [(51, 89, 12, 28)]


def test_detect_boxes_in_areas_clips_area_to_frame():
    image = np.zeros((80, 120, 3), dtype=np.uint8)

    result = detect_boxes_in_areas(
        image,
        [(-20, 100, 100, 160)],
        lambda crop: [(0, crop.shape[1], 0, crop.shape[0])],
    )

    assert result == [(100, 120, 0, 80)]


def test_detect_boxes_in_areas_uses_full_frame_without_area():
    image = np.zeros((60, 90, 3), dtype=np.uint8)
    received_shapes = []

    result = detect_boxes_in_areas(
        image,
        [],
        lambda frame: received_shapes.append(frame.shape) or [(2, 20, 3, 30)],
    )

    assert received_shapes == [(60, 90, 3)]
    assert result == [(2, 20, 3, 30)]
