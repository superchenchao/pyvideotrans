from __future__ import annotations

import argparse
import json
import multiprocessing
import sys
import time
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def emit(event: str, **payload) -> None:
    print(
        "BENCH_EVENT "
        + json.dumps({"event": event, **payload}, ensure_ascii=False),
        flush=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    source = Path(args.input).resolve()
    run_root = Path(args.run_root).resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    result_file = run_root / "benchmark-result.json"

    from videotrans.configure import config

    config.init_run()

    from videotrans.configure.config import app_cfg, settings
    from videotrans.task.taskcfg import TaskCfgVTT
    from videotrans.task.trans_create import TransCreate
    from videotrans.util.help_ffmpeg import format_video
    from videotrans.util.tools import get_subtitle_from_srt

    settings["countdown_sec"] = 0
    app_cfg.exit_soft = False
    app_cfg.exec_mode = "cli"
    app_cfg.current_status = "ing"

    file_cfg = asdict(format_video(source.as_posix()))
    file_cfg.update(
        {
            "target_dir": (run_root / "01-mp4").as_posix(),
            "cache_folder": (run_root / "cache").as_posix(),
            "series_video_paths": [source.as_posix()],
            "is_cuda": True,
            "source_language_code": "zh-cn",
            "target_language_code": "en",
            "recogn_type": 0,
            "model_name": "large-v3-turbo",
            "remove_noise": False,
            "enable_diariz": True,
            "nums_diariz": 0,
            "rephrase": 0,
            "fix_punc": False,
            "tts_type": 0,
            "voice_role": "Yan(Female/HK)",
            "voice_rate": "+0%",
            "volume": "+0%",
            "pitch": "+0Hz",
            "voice_autorate": True,
            "video_autorate": False,
            "remove_silent_mid": False,
            "align_sub_audio": True,
            "translate_type": 4,
            "is_separate": True,
            "embed_bgm": False,
            "clear_cache": not args.resume,
            "background_music": "",
            "subtitle_type": 1,
            "only_out_mp4": False,
            "recogn2pass": False,
            "output_srt": 0,
            "copysrt_rawvideo": False,
            "loop_backaudio": 1,
            "backaudio_volume": 0.8,
            "burned_subtitle_ocr": True,
            "remove_burned_subtitles": True,
            "subtitle_removal_rect": [
                0.007407407407407408,
                0.63125,
                0.9925925925925926,
                0.07135416666666666,
            ],
            "subtitle_removal_aspect_ratio": 0.5625,
        }
    )

    if args.resume and result_file.is_file():
        result = json.loads(result_file.read_text(encoding="utf-8"))
        result.setdefault("attempts", []).append(
            {
                "resumed_at": datetime.now(timezone.utc).isoformat(),
                "previous_status": result.get("status"),
                "previous_error": result.get("error", ""),
            }
        )
        result.pop("error", None)
        result.pop("traceback", None)
        result.pop("finished_at", None)
        result["status"] = "running"
    else:
        result = {
            "status": "running",
            "input": source.as_posix(),
            "run_root": run_root.as_posix(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "stages": {},
        }
    result_file.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    task = None
    total_started = time.perf_counter()

    def run_stage(name, callback):
        emit("stage_started", stage=name)
        started = time.perf_counter()
        callback()
        elapsed = time.perf_counter() - started
        result["stages"][name] = round(elapsed, 3)
        result_file.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        emit("stage_completed", stage=name, seconds=round(elapsed, 3))

    try:
        task = TransCreate(cfg=TaskCfgVTT(**file_cfg))
        # Recognition keeps its subprocess log under config.TEMP_DIR/uuid,
        # independently from cfg.cache_folder. GUI/CLI normally use the same
        # tree for both, while this benchmark deliberately keeps its cache on
        # the roomy system disk.
        (Path(config.TEMP_DIR) / task.uuid).mkdir(parents=True, exist_ok=True)
        run_stage("resume_prepare" if args.resume else "prepare", task.prepare)
        run_stage("recogn", task.recogn)
        run_stage("diariz", task.diariz)
        if task.should_trans:
            run_stage("translate", task.trans)
        if task.should_dubbing:
            run_stage(
                "prepare_roles",
                lambda: task._prepare_line_roles(
                    get_subtitle_from_srt(task.cfg.source_sub, is_file=True)
                ),
            )
            run_stage("dubbing", task.dubbing)
        run_stage("align", task.align)
        run_stage("recogn2pass", task.recogn2pass)
        run_stage("assembling", task.assembling)
        run_stage("task_done", task.task_done)

        result["status"] = "completed"
        result["current_attempt_seconds"] = round(
            time.perf_counter() - total_started, 3
        )
        result["total_seconds"] = round(
            sum(
                seconds
                for name, seconds in result["stages"].items()
                if name != "resume_prepare"
            ),
            3,
        )
        result["output"] = Path(task.cfg.targetdir_mp4).resolve().as_posix()
        output = Path(result["output"])
        result["output_exists"] = output.is_file()
        result["output_size"] = output.stat().st_size if output.is_file() else 0
        emit(
            "completed",
            seconds=result["total_seconds"],
            output=result["output"],
            output_size=result["output_size"],
        )
        return 0
    except Exception as error:
        result["status"] = "failed"
        result["current_attempt_seconds"] = round(
            time.perf_counter() - total_started, 3
        )
        result["total_seconds"] = round(
            sum(
                seconds
                for name, seconds in result["stages"].items()
                if name != "resume_prepare"
            ),
            3,
        )
        result["error"] = str(error)
        result["traceback"] = traceback.format_exc()
        emit("failed", seconds=result["total_seconds"], error=str(error))
        return 1
    finally:
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        result_file.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        multiprocessing.set_start_method("spawn", force=True)
    except RuntimeError:
        pass
    raise SystemExit(main())
