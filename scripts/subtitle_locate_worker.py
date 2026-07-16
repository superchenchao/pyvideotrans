from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


EVENT_PREFIX = "PYVT_LOCATE_EVENT "


def emit(event_type: str, **payload) -> None:
    print(
        EVENT_PREFIX + json.dumps({"type": event_type, **payload}, ensure_ascii=False),
        flush=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Locate the first burned-in subtitle frame")
    parser.add_argument("--video", required=True)
    parser.add_argument("--engine-root", required=True)
    parser.add_argument("--sample-ms", type=int, default=500)
    parser.add_argument("--max-ms", type=int, default=180000)
    parser.add_argument("--frame-output", default="")
    return parser.parse_args()


def has_subtitle_box(result, roi_width: int, roi_height: int) -> bool:
    payload = result.json if hasattr(result, "json") else result
    if isinstance(payload, dict) and "res" in payload:
        payload = payload["res"]
    if not isinstance(payload, dict):
        return False
    texts = list(payload.get("rec_texts", []) or [])
    scores = list(payload.get("rec_scores", []) or [])
    boxes = list(payload.get("rec_boxes", []) or [])
    for text, score, box in zip(texts, scores, boxes):
        chinese_count = sum("\u3400" <= char <= "\u9fff" for char in str(text))
        if chinese_count < 1 or float(score or 0.0) < 0.85:
            continue
        x1, y1, x2, y2 = [int(value) for value in box]
        width, height = x2 - x1, y2 - y1
        center_x = (x1 + x2) / 2
        if width >= max(24, roi_width * 0.06) and height >= max(10, roi_height * 0.035):
            if roi_width * 0.08 <= center_x <= roi_width * 0.92:
                return True
    return False


def main() -> int:
    args = parse_args()
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

    import cv2
    from paddleocr import PaddleOCR

    engine_root = Path(args.engine_root).resolve()
    detector = PaddleOCR(
        text_detection_model_name="PP-OCRv5_mobile_det",
        text_detection_model_dir=str(engine_root / "backend" / "models" / "V5" / "ch_det_fast"),
        text_recognition_model_name="PP-OCRv5_mobile_rec",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        device="cpu",
    )
    capture = cv2.VideoCapture(str(Path(args.video).resolve()))
    if not capture.isOpened():
        raise RuntimeError(f"Cannot open video: {args.video}")
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if fps <= 0 or frame_count <= 0:
        raise RuntimeError("Video FPS or frame count is invalid")

    sample_frames = max(1, round(fps * max(100, args.sample_ms) / 1000))
    max_frame = min(frame_count, round(fps * max(1000, args.max_ms) / 1000))
    frame_index = 0
    try:
        while capture.isOpened() and frame_index < max_frame:
            readable, frame = capture.read()
            if not readable:
                break
            if frame_index % sample_frames == 0:
                height, width = frame.shape[:2]
                left, right = round(width * 0.04), round(width * 0.96)
                top, bottom = round(height * 0.52), round(height * 0.92)
                roi = frame[top:bottom, left:right]
                # 定位阶段只需判断当前帧是否有字幕，缩小图像可显著减少首次扫描耗时。
                if roi.shape[1] > 640:
                    scale = 640 / roi.shape[1]
                    roi = cv2.resize(
                        roi,
                        (640, max(1, round(roi.shape[0] * scale))),
                        interpolation=cv2.INTER_AREA,
                    )
                for result in detector.predict(roi):
                    if has_subtitle_box(result, roi.shape[1], roi.shape[0]):
                        frame_output = ""
                        if args.frame_output:
                            output_path = Path(args.frame_output).resolve()
                            output_path.parent.mkdir(parents=True, exist_ok=True)
                            if cv2.imwrite(str(output_path), frame):
                                frame_output = str(output_path)
                        emit(
                            "found",
                            time_ms=round(frame_index * 1000 / fps),
                            frame_file=frame_output,
                        )
                        return 0
            frame_index += 1
    finally:
        capture.release()
    emit("not_found", time_ms=0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
