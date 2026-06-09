import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from videotrans.configure import config
from videotrans.configure.config import app_cfg
from videotrans.task.dubbing import DubbingSrt
from videotrans.task.taskcfg import TaskCfgTTS
from videotrans.util import tools


DEFAULT_ROLE_MAP = {
    "spk0": "Ana(Female/US)",
    "spk1": "Guy(Male/US)",
    "spk2": "Jenny(Female/US)",
    "spk3": "Roger(Male/US)",
    "spk4": "Aria(Female/US)",
}

SPEAKER_RE = re.compile(r"^\s*\[((?:spk|speaker|speaker_|\w{1,10})\s*\d*)\]\s*[:：]?\s*", re.I)


def parse_role_map(raw_items):
    role_map = dict(DEFAULT_ROLE_MAP)
    for item in raw_items:
        if "=" not in item:
            raise ValueError(f"Invalid --role-map item: {item!r}, expected spk0=Voice(Name)")
        speaker, role = item.split("=", 1)
        role_map[speaker.strip()] = role.strip()
    return role_map


def write_srt(subs, path):
    blocks = []
    for sub in subs:
        blocks.append(
            f"{sub['line']}\n{sub['startraw']} --> {sub['endraw']}\n{sub['text'].strip()}"
        )
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True)
    parser.add_argument("--tagged-srt", required=True)
    parser.add_argument("--speaker-json")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--role-map", action="append", default=[])
    parser.add_argument("--default-role", default="Steffan(Male/US)")
    parser.add_argument("--target-language-code", default="en")
    parser.add_argument("--voice-rate", default="+0%")
    parser.add_argument("--volume", default="+0%")
    parser.add_argument("--pitch", default="+0Hz")
    parser.add_argument("--no-autorate", action="store_true")
    args = parser.parse_args()

    config.init_run()

    video = Path(args.video).resolve()
    tagged_srt = Path(args.tagged_srt).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    role_map = parse_role_map(args.role_map)
    subs = tools.get_subtitle_from_srt(tagged_srt.as_posix())
    speakers = None
    if args.speaker_json:
        import json

        speakers = json.loads(Path(args.speaker_json).read_text(encoding="utf-8"))
    stripped_subs = []
    app_cfg.dubbing_role.clear()

    speaker_counts = {}
    for sub in subs:
        text = sub["text"].strip()
        match = SPEAKER_RE.match(text)
        speaker = speakers[int(sub["line"]) - 1] if speakers else (match.group(1).strip() if match else None)
        if speaker and match:
            text = text[match.end():].strip()
        if speaker:
            speaker_counts[speaker] = speaker_counts.get(speaker, 0) + 1
            if speaker in role_map:
                app_cfg.dubbing_role[int(sub["line"])] = role_map[speaker]
        stripped = {key: sub[key] for key in sub}
        stripped["text"] = text
        stripped_subs.append(stripped)

    clean_srt = output_dir / "en-multirole-clean.srt"
    write_srt(stripped_subs, clean_srt)

    video_obj = tools.format_video(clean_srt.as_posix(), None)
    cache_dir = output_dir / "cache"
    cfg = {
        "voice_role": args.default_role,
        "cache_folder": cache_dir.as_posix(),
        "target_language_code": args.target_language_code,
        "target_dir": output_dir.as_posix(),
        "voice_rate": args.voice_rate,
        "volume": args.volume,
        "uuid": video_obj["uuid"],
        "pitch": args.pitch,
        "tts_type": 0,
        "voice_autorate": not args.no_autorate,
        "remove_silent_mid": False,
        "align_sub_audio": False,
        "is_cuda": False,
    }

    trk = DubbingSrt(
        cfg=TaskCfgTTS(**(video_obj | cfg)),
        subs=stripped_subs,
        is_multi_role=True,
        out_ext="m4a",
    )
    trk.dubbing()
    trk.align()
    trk.task_done()

    audio = output_dir / f"{clean_srt.stem}.m4a"
    if not audio.exists():
        raise FileNotFoundError(f"Expected dubbed audio was not created: {audio}")

    final_video = output_dir / "01-multirole.mp4"
    subtitle_filter = clean_srt.as_posix().replace("\\", "/").replace(":", "\\:")
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        video.as_posix(),
        "-i",
        audio.as_posix(),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-vf",
        f"subtitles='{subtitle_filter}'",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        final_video.as_posix(),
    ]
    subprocess.run(cmd, check=True)

    print("speaker_counts:", speaker_counts)
    print("role_map:", role_map)
    print("audio:", audio.as_posix())
    print("video:", final_video.as_posix())


if __name__ == "__main__":
    main()
