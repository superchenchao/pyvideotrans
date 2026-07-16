from __future__ import annotations

import argparse
import gc
import importlib
import json
import os
from pathlib import Path


EVENT_PREFIX = "PYVT_OCR_EVENT "


def emit(event_type: str, **payload) -> None:
    print(
        EVENT_PREFIX + json.dumps({"type": event_type, **payload}, ensure_ascii=False),
        flush=True,
    )


def load_ocr_dependencies(import_module=importlib.import_module):
    """Load shared Torch DLLs before Paddle to avoid Windows DLL conflicts."""
    torch_preloaded = False
    try:
        import_module("torch")
        torch_preloaded = True
    except ModuleNotFoundError as error:
        # Torch is optional for OCR-only environments. Do not hide a missing
        # transitive dependency from an installed Torch package.
        if error.name != "torch":
            raise

    cv2 = import_module("cv2")
    paddle = import_module("paddle")
    paddle_ocr = import_module("paddleocr").PaddleOCR
    return cv2, paddle, paddle_ocr, torch_preloaded


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract burned-in Chinese subtitles")
    parser.add_argument("--video", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--engine-root", required=True)
    parser.add_argument("--sample-ms", type=int, default=250)
    parser.add_argument("--roi-top", type=float, default=0.55)
    parser.add_argument("--roi-bottom", type=float, default=0.90)
    parser.add_argument("--roi-left", type=float, default=0.05)
    parser.add_argument("--roi-right", type=float, default=0.95)
    return parser.parse_args()


def chinese_count(text: str) -> int:
    return sum("\u3400" <= char <= "\u9fff" for char in text)


def latin_count(text: str) -> int:
    return sum(("a" <= char.lower() <= "z") for char in text)


def read_ocr_result(result, roi_width: int, roi_height: int):
    payload = result.json if hasattr(result, "json") else result
    if isinstance(payload, dict) and "res" in payload:
        payload = payload["res"]
    texts = list(payload.get("rec_texts", []) or [])
    scores = list(payload.get("rec_scores", []) or [])
    boxes = list(payload.get("rec_boxes", []) or [])
    accepted = []
    for text, score, box in zip(texts, scores, boxes):
        text = str(text).strip()
        score = float(score or 0.0)
        if not text or score < 0.65 or chinese_count(text) < max(1, latin_count(text)):
            continue
        x1, y1, x2, y2 = [int(value) for value in box]
        width, height = x2 - x1, y2 - y1
        center_x = (x1 + x2) / 2
        if width < 8 or height < 10:
            continue
        if center_x < roi_width * 0.10 or center_x > roi_width * 0.90:
            continue
        accepted.append({
            "text": text,
            "score": score,
            "box": [x1, y1, x2, y2],
            "center_y": (y1 + y2) / 2,
            "height": height,
        })
    if not accepted:
        return None

    accepted.sort(key=lambda item: (item["center_y"], item["box"][0]))
    rows = []
    for item in accepted:
        if not rows:
            rows.append([item])
            continue
        row_center = sum(entry["center_y"] for entry in rows[-1]) / len(rows[-1])
        tolerance = max(item["height"], *(entry["height"] for entry in rows[-1])) * 0.65
        if abs(item["center_y"] - row_center) <= tolerance:
            rows[-1].append(item)
        else:
            rows.append([item])

    row_texts = []
    row_scores = []
    all_boxes = []
    for row in rows:
        row.sort(key=lambda item: item["box"][0])
        row_texts.append("".join(item["text"] for item in row))
        row_scores.extend(item["score"] for item in row)
        all_boxes.extend(item["box"] for item in row)
    text = "\n".join(row_texts).strip()
    if not text:
        return None
    return {
        "text": text,
        "score": sum(row_scores) / len(row_scores),
        "box": [
            min(box[0] for box in all_boxes),
            min(box[1] for box in all_boxes),
            max(box[2] for box in all_boxes),
            max(box[3] for box in all_boxes),
        ],
    }


def _error_summary(error: Exception) -> str:
    message = " ".join(str(error).split())
    return message[:300] or error.__class__.__name__


def preferred_ocr_device(paddle) -> tuple[str, str]:
    """Choose GPU 0 only when the installed Paddle runtime can see CUDA."""
    try:
        if not paddle.is_compiled_with_cuda():
            return "cpu", "Paddle is not CUDA-enabled"
        if paddle.device.cuda.device_count() < 1:
            return "cpu", "Paddle cannot find an available CUDA device"
    except Exception as error:
        return "cpu", f"CUDA capability check failed: {_error_summary(error)}"
    return "gpu:0", "CUDA device 0 is available"


class OcrEngine:
    """Run OCR on GPU when possible and retry safely on CPU after GPU errors."""

    def __init__(self, paddle_ocr, paddle, detector_dir: Path):
        self._paddle_ocr = paddle_ocr
        self._paddle = paddle
        self._detector_dir = detector_dir
        self._ocr = None
        preferred_device, reason = preferred_ocr_device(paddle)
        if preferred_device == "cpu":
            emit("log", message=f"OCR device: CPU ({reason})")
            self._load("cpu")
            return

        try:
            self._load(preferred_device)
            emit("log", message="OCR device: GPU 0")
        except Exception as error:
            emit(
                "log",
                message=(
                    "GPU OCR initialization failed; falling back to CPU: "
                    f"{_error_summary(error)}"
                ),
            )
            self._release_gpu()
            self._load("cpu")
            emit("log", message="OCR device: CPU (GPU initialization fallback)")

    @property
    def device(self) -> str:
        return self._device

    def _load(self, device: str) -> None:
        self._ocr = self._paddle_ocr(
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_detection_model_dir=str(self._detector_dir),
            text_recognition_model_name="PP-OCRv5_mobile_rec",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            device=device,
        )
        self._device = device

    def _release_gpu(self) -> None:
        self._ocr = None
        gc.collect()
        try:
            self._paddle.device.cuda.empty_cache()
        except Exception:
            pass

    def predict(self, image):
        try:
            # Materialize the iterator here so lazy GPU inference errors are caught.
            return list(self._ocr.predict(image))
        except Exception as error:
            if self._device != "gpu:0":
                raise
            emit(
                "log",
                message=(
                    "GPU OCR inference failed; falling back to CPU and retrying "
                    f"the current frame: {_error_summary(error)}"
                ),
            )
            self._release_gpu()
            self._load("cpu")
            emit("log", message="OCR device: CPU (GPU inference fallback)")
            return list(self._ocr.predict(image))


def main() -> int:
    args = parse_args()
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

    cv2, paddle, PaddleOCR, torch_preloaded = load_ocr_dependencies()
    if torch_preloaded:
        emit("log", message="Preloaded shared Torch runtime before Paddle OCR")

    engine_root = Path(args.engine_root).resolve()
    detector_dir = engine_root / "backend" / "models" / "V5" / "ch_det_fast"
    ocr = OcrEngine(PaddleOCR, paddle, detector_dir)

    capture = cv2.VideoCapture(str(Path(args.video).resolve()))
    if not capture.isOpened():
        raise RuntimeError(f"Cannot open video: {args.video}")
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if fps <= 0 or frame_count <= 0:
        raise RuntimeError("Video FPS or frame count is invalid")

    sample_ms = max(100, int(args.sample_ms))
    sample_frames = max(1, round(fps * sample_ms / 1000))
    observations = []
    frame_index = 0
    last_progress = -1
    emit("log", message=f"OCR sample interval: {sample_ms}ms")

    def recognize_frame(frame, timestamp_ms):
        height, width = frame.shape[:2]
        left = max(0, min(width - 1, round(width * args.roi_left)))
        right = max(left + 1, min(width, round(width * args.roi_right)))
        top = max(0, min(height - 1, round(height * args.roi_top)))
        bottom = max(top + 1, min(height, round(height * args.roi_bottom)))
        roi = frame[top:bottom, left:right]
        for page in ocr.predict(roi):
            result = read_ocr_result(page, right - left, bottom - top)
            if result:
                result["time_ms"] = timestamp_ms
                return result
        return None

    # Avoid scanning an entire ordinary video that has no burned-in subtitles.
    probe_positions = sorted(set(
        round(frame_count * index / 25) for index in range(1, 25)
    ))
    probe_observations = []
    for position in probe_positions:
        capture.set(cv2.CAP_PROP_POS_FRAMES, position)
        readable, frame = capture.read()
        if not readable:
            continue
        result = recognize_frame(frame, round(position * 1000 / fps))
        if result:
            probe_observations.append(result)
    if len(probe_observations) < 2:
        output = {
            "video": str(Path(args.video).resolve()),
            "fps": fps,
            "sample_ms": round(sample_frames * 1000 / fps),
            "device": ocr.device,
            "observations": probe_observations,
        }
        output_path = Path(args.output).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
        capture.release()
        emit("progress", value=100)
        emit("log", message="No stable burned-in subtitle track detected; keep ASR")
        return 0
    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
    emit("log", message=f"OCR preflight detected {len(probe_observations)} subtitle samples")
    try:
        while capture.isOpened():
            readable, frame = capture.read()
            if not readable:
                break
            if frame_index % sample_frames == 0:
                result = recognize_frame(frame, round(frame_index * 1000 / fps))
                if result:
                    observations.append(result)

            progress = min(99, int(frame_index * 100 / frame_count))
            if progress // 5 != last_progress // 5:
                emit("progress", value=progress)
                last_progress = progress
            frame_index += 1
    finally:
        capture.release()

    output = {
        "video": str(Path(args.video).resolve()),
        "fps": fps,
        "sample_ms": round(sample_frames * 1000 / fps),
        "device": ocr.device,
        "observations": observations,
    }
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
    emit("progress", value=100)
    emit("log", message=f"OCR observations: {len(observations)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
