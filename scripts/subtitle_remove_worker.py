from __future__ import annotations

import argparse
import importlib.abc
import importlib.machinery
import importlib.util
import json
import math
import os
import sys
import traceback
from pathlib import Path

EVENT_PREFIX = "PYVT_EVENT "


class _STTNAutoCompatibilityLoader(importlib.machinery.SourceFileLoader):
    def get_data(self, path: str) -> bytes:
        source = super().get_data(path)
        if path.endswith("sttn_auto_inpaint.py"):
            source = source.replace(
                b"tqdm.write(f'Processing: {start_f + 1} - {end_f} / Total: {frame_info['len']}')",
                b'tqdm.write(f"Processing: {start_f + 1} - {end_f} / Total: {frame_info[\'len\']}")',
            )
        return source


class _STTNAutoCompatibilityFinder(importlib.abc.MetaPathFinder):
    def __init__(self, engine_root: Path):
        self.source_path = engine_root / "backend" / "inpaint" / "sttn_auto_inpaint.py"

    def find_spec(self, fullname: str, path=None, target=None):
        if fullname != "backend.inpaint.sttn_auto_inpaint" or not self.source_path.is_file():
            return None
        loader = _STTNAutoCompatibilityLoader(fullname, str(self.source_path))
        return importlib.util.spec_from_file_location(fullname, self.source_path, loader=loader)


def emit(event_type: str, **payload) -> None:
    print(EVENT_PREFIX + json.dumps({"type": event_type, **payload}, ensure_ascii=False), flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="pyVideoTrans subtitle removal worker")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--engine-root", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode", choices=("force", "auto"), required=True)
    parser.add_argument("--rect", nargs=4, type=int, metavar=("X", "Y", "WIDTH", "HEIGHT"))
    parser.add_argument("--start-ms", type=int, default=0)
    parser.add_argument("--end-ms", type=int, default=0)
    parser.add_argument("--lookbehind", type=int, default=4)
    parser.add_argument("--lookahead", type=int, default=4)
    parser.add_argument(
        "--inpaint-engine",
        choices=("auto", "sttn", "propainter"),
        default="auto",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    project_root = Path(args.project_root).resolve()
    engine_root = Path(args.engine_root).resolve()
    sys.path.insert(0, str(project_root))
    sys.path.insert(0, str(engine_root))
    sys.meta_path.insert(0, _STTNAutoCompatibilityFinder(engine_root))
    os.chdir(engine_root)

    import cv2
    from videotrans.subtitle_removal.roi_detection import (
        detect_boxes_in_areas,
        interpolate_sampled_boxes,
    )
    from videotrans.subtitle_removal.temporal_boxes import merge_temporal_frame_boxes
    from videotrans.subtitle_removal.blending import (
        composite_repaired_frames,
        repair_is_suspiciously_black,
    )
    from videotrans.subtitle_removal.strategy import (
        GPUProfile,
        PROPAINTER_BACKEND,
        propainter_models_ready,
        select_inpaint_backend,
    )
    import backend.inpaint.propainter_inpaint as propainter_module
    from backend.config import config, tr
    from backend.inpaint.propainter_inpaint import PropainterInpaint
    from backend.inpaint.sttn_det_inpaint import STTNDetInpaint
    from backend.main import SubtitleRemover
    from backend.tools.common_tools import get_readable_path
    from backend.tools.constant import InpaintMode, SubtitleDetectMode
    from backend.tools.subtitle_detect import SubtitleDetect

    config.set(config.interface, "en")
    if args.mode == "auto":
        config.set(config.subtitleDetectMode, SubtitleDetectMode.PP_OCRv5_MOBILE)
    translation_file = engine_root / "backend" / "interface" / "en.ini"
    tr.read(str(translation_file), encoding="utf-8")

    remover = SubtitleRemover(args.input)
    remover.video_out_path = str(Path(args.output).resolve())
    Path(remover.video_out_path).parent.mkdir(parents=True, exist_ok=True)

    start_frame = max(0, math.floor(args.start_ms * remover.fps / 1000))
    end_ms = args.end_ms if args.end_ms > 0 else math.ceil(remover.frame_count * 1000 / remover.fps)
    end_frame = min(remover.frame_count, max(start_frame + 1, math.ceil(end_ms * remover.fps / 1000)))
    remover.ab_sections = [range(start_frame, end_frame)]

    # Preserve original high-resolution pixels outside the detected text mask.
    # The bundled STTN detector otherwise writes its resized crop back over the
    # whole horizontal band, which creates visible blur around hands and clothes.
    original_sttn_call = STTNDetInpaint.__call__

    def sttn_call_with_soft_composite(model, input_frames, input_mask):
        repaired = original_sttn_call(model, input_frames, input_mask)
        return composite_repaired_frames(input_frames, repaired, input_mask)

    STTNDetInpaint.__call__ = sttn_call_with_soft_composite

    original_propainter_call = PropainterInpaint.__call__

    def propainter_call_with_guard(model, input_frames, input_mask):
        repaired = original_propainter_call(model, input_frames, input_mask)
        if repair_is_suspiciously_black(input_frames, repaired, input_mask):
            raise RuntimeError("ProPainter produced an invalid black repair region")
        return composite_repaired_frames(input_frames, repaired, input_mask)

    PropainterInpaint.__call__ = propainter_call_with_guard

    if args.rect:
        x, y, width, height = args.rect
        x = max(0, min(x, remover.frame_width - 1))
        y = max(0, min(y, remover.frame_height - 1))
        right = max(x + 1, min(x + width, remover.frame_width))
        bottom = max(y + 1, min(y + height, remover.frame_height))
        remover.sub_areas = [(y, bottom, x, right)]
    elif args.mode == "force":
        raise ValueError("Force removal requires a rectangle")

    progress_state = {"ocr_complete": args.mode != "auto"}
    if args.mode == "auto":
        original_detect = SubtitleDetect.detect_subtitle

        def detect_in_selected_areas(detector, image):
            areas = list(detector.sub_areas or [])
            if not areas:
                return original_detect(detector, image)

            def detect_crop(crop):
                detector.sub_areas = []
                try:
                    return original_detect(detector, crop)
                finally:
                    detector.sub_areas = areas

            return detect_boxes_in_areas(image, areas, detect_crop)

        def find_with_temporal_merge(detector, sub_remover=None):
            video_cap = cv2.VideoCapture(get_readable_path(detector.video_path))
            video_cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
            current_frame_no = start_frame
            frame_total = max(1, end_frame - start_frame)
            sampled_results = {}
            last_progress = -1
            if sub_remover:
                sub_remover.append_output(tr["Main"]["ProcessingStartFindingSubtitles"])
            try:
                while current_frame_no < end_frame and video_cap.isOpened():
                    readable, frame = video_cap.read()
                    if not readable:
                        break
                    current_frame_no += 1
                    if (current_frame_no - 1) % detector.SAMPLE_STEP == 0:
                        boxes = detector.detect_subtitle(frame)
                        if boxes:
                            sampled_results[current_frame_no] = boxes
                    progress = min(
                        50,
                        int(50 * (current_frame_no - start_frame) / frame_total),
                    )
                    if progress != last_progress:
                        emit("progress", value=progress, finished=False)
                        last_progress = progress
            finally:
                video_cap.release()

            raw_boxes = interpolate_sampled_boxes(
                sampled_results,
                sample_step=detector.SAMPLE_STEP,
            )
            raw_boxes = detector.unify_regions(raw_boxes)
            raw_boxes = {frame: boxes for frame, boxes in raw_boxes.items() if boxes}
            if sub_remover:
                sub_remover.append_output(tr["Main"]["FinishedFindingSubtitles"])
            progress_state["ocr_complete"] = True
            emit("progress", value=50, finished=False)
            merged = merge_temporal_frame_boxes(
                raw_boxes,
                lookbehind=args.lookbehind,
                lookahead=args.lookahead,
                first_frame=1,
                last_frame=remover.frame_count,
                frame_size=(remover.frame_width, remover.frame_height),
            )
            emit(
                "log",
                message=(
                    f"OCR temporal merge: {len(raw_boxes)} detected frames -> "
                    f"{len(merged)} masked frames"
                ),
            )
            return merged

        SubtitleDetect.detect_subtitle = detect_in_selected_areas
        SubtitleDetect.find_subtitle_frame_no = find_with_temporal_merge
        if remover.sub_areas:
            ymin, ymax, xmin, xmax = remover.sub_areas[0]
            roi_pixels = (xmax - xmin) * (ymax - ymin)
            frame_pixels = remover.frame_width * remover.frame_height
            emit(
                "log",
                message=(
                    f"OCR ROI acceleration: {xmax - xmin}x{ymax - ymin}, "
                    f"{roi_pixels / frame_pixels:.1%} of the full frame"
                ),
            )
        import torch

        if torch.cuda.is_available():
            free_bytes, total_bytes = torch.cuda.mem_get_info()
            gpu_profile = GPUProfile(
                name=torch.cuda.get_device_name(0),
                total_vram_mb=int(total_bytes / (1024 * 1024)),
                free_vram_mb=int(free_bytes / (1024 * 1024)),
                cuda_available=True,
            )
        else:
            gpu_profile = GPUProfile()
        selection = select_inpaint_backend(
            requested=args.inpaint_engine,
            gpu=gpu_profile,
            propainter_ready=propainter_models_ready(engine_root),
        )
        emit(
            "backend_selected",
            backend=selection.backend,
            reason=selection.reason,
            gpu=gpu_profile.name or "CPU",
            total_vram_mb=gpu_profile.total_vram_mb,
            free_vram_mb=gpu_profile.free_vram_mb,
        )
        if selection.backend == PROPAINTER_BACKEND:
            # Crops near 100 px high made the bundled ProPainter return NaNs,
            # which were converted to black pixels. A 240 px minimum matches
            # the model's practical inference floor and stays cheap for a
            # subtitle-only horizontal crop.
            original_area_finder = propainter_module.get_inpaint_area_by_mask

            def propainter_area_finder(width, height, crop_height, mask, multiple=1):
                safe_height = min(height, max(240, crop_height))
                return original_area_finder(
                    width, height, safe_height, mask, multiple=multiple,
                )

            propainter_module.get_inpaint_area_by_mask = propainter_area_finder
            config.propainterMaxLoadNum.value = selection.propainter_batch_size
            config.inpaintMode.value = InpaintMode.PROPAINTER
            emit(
                "log",
                message=(
                    f"Auto inpaint: ProPainter on {gpu_profile.name}, "
                    f"batch={selection.propainter_batch_size}, "
                    f"free VRAM={gpu_profile.free_vram_mb} MB"
                ),
            )
        else:
            config.inpaintMode.value = InpaintMode.STTN_DET
            emit(
                "log",
                message=f"Auto inpaint: STTN ({selection.reason})",
            )
    else:
        config.inpaintMode.value = InpaintMode.STTN_AUTO

    remover.append_output = lambda *items: emit("log", message=" ".join(str(item) for item in items))

    def report_removal_progress(progress, finished):
        value = int(progress)
        if args.mode == "auto" and progress_state["ocr_complete"]:
            value = 50 + value // 2
        emit("progress", value=min(100, value), finished=bool(finished))

    remover.add_progress_listener(report_removal_progress)
    emit("started", fps=remover.fps, frames=remover.frame_count)
    remover.run()
    if not Path(remover.video_out_path).is_file():
        raise RuntimeError("Subtitle remover did not create the output video")
    emit("completed", output=remover.video_out_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as error:
        emit("error", message=str(error), traceback=traceback.format_exc())
        raise SystemExit(1)
