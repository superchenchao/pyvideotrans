from __future__ import annotations

from cineflow.models import (
    DubbingArtifact,
    DubbingClip,
    JobRequest,
    MediaArtifacts,
    SubtitleLine,
    Transcript,
    VideoProbe,
)
from cineflow.providers.aliyun_media import (
    AliyunMediaConfig,
    AliyunMediaService,
    build_assembly_timeline,
    classify_demix_outputs,
    transcript_to_srt,
)


class FakeStore:
    configured = True

    def __init__(self):
        self.uploads = {}

    def key(self, *parts):
        return "cineflow/" + "/".join(str(part).strip("/") for part in parts)

    def canonical_url(self, object_key):
        return f"https://bucket.oss-cn-beijing.aliyuncs.com/{object_key}"

    def oss_uri(self, object_key):
        return f"oss://bucket/{object_key}"

    def signed_url(self, object_key):
        return f"https://signed.example/{object_key}?signature=ok"

    def sign_if_owned(self, value):
        if "bucket.oss-cn-beijing.aliyuncs.com/" in value:
            key = value.split(".com/", 1)[1]
            return self.signed_url(key)
        return value

    def put_bytes(self, object_key, content, *, content_type=""):
        self.uploads[object_key] = (content, content_type)

    @staticmethod
    def canonicalize_oss_input(value):
        return str(value).split("?", 1)[0]


class FakeICE:
    configured = True

    def __init__(self):
        self.demix_submissions = []
        self.timeline_submissions = []

    async def submit_i_production(self, **kwargs):
        self.demix_submissions.append(kwargs)
        return "demix-job"

    async def wait_i_production(self, job_id):
        assert job_id == "demix-job"
        return {
            "Status": "Success",
            "OutputUrls": [
                "https://bucket.oss-cn-beijing.aliyuncs.com/out/demix-vocal.wav",
                "https://bucket.oss-cn-beijing.aliyuncs.com/out/demix-accompaniment.wav",
            ],
        }

    async def submit_media_producing(self, **kwargs):
        self.timeline_submissions.append(kwargs)
        return f"timeline-job-{len(self.timeline_submissions)}"

    async def wait_media_producing(self, job_id):
        submission = self.timeline_submissions[int(job_id.rsplit("-", 1)[1]) - 1]
        return {
            "Status": "Success",
            "MediaURL": submission["output_media_url"],
            "Duration": 3.0,
        }


def request_for(**updates):
    values = {
        "input_url": "https://source.oss-cn-beijing.aliyuncs.com/input.mp4?sig=1",
        "clean_video_url": "https://source.oss-cn-beijing.aliyuncs.com/clean.mp4?sig=2",
        "source_audio_url": "https://source.oss-cn-beijing.aliyuncs.com/source.wav?sig=3",
        "probe": VideoProbe(
            duration_seconds=3,
            input_bytes=3_000_000,
            width=1920,
            height=1080,
        ),
        "source_language": "zh-CN",
        "target_language": "en-US",
        "target_voice": "en-US-AvaMultilingualNeural",
    }
    values.update(updates)
    return JobRequest(**values)


def transcript():
    return Transcript(
        language="en-US",
        lines=[
            SubtitleLine(
                line_id=1,
                start_ms=0,
                end_ms=1200,
                text="Hello.",
            ),
            SubtitleLine(
                line_id=2,
                start_ms=700,
                end_ms=2200,
                text="Who are you?",
            ),
        ],
    )


def dubbing():
    return DubbingArtifact(
        clips=[
            DubbingClip(
                line_id=1,
                character_id="character_001",
                audio_url="https://bucket.oss-cn-beijing.aliyuncs.com/tts/1.mp3",
                duration_ms=1400,
            ),
            DubbingClip(
                line_id=2,
                character_id="character_002",
                audio_url="https://bucket.oss-cn-beijing.aliyuncs.com/tts/2.mp3",
                duration_ms=1500,
            ),
        ]
    )


def test_timeline_mutes_source_adds_background_dialogue_and_hard_subtitles():
    timeline = build_assembly_timeline(
        video_url="https://bucket.oss-cn-beijing.aliyuncs.com/clean.mp4",
        translated=transcript(),
        dubbing=dubbing(),
        background_url="https://bucket.oss-cn-beijing.aliyuncs.com/background.wav",
        subtitle_mode="hard",
        width=1920,
        height=1080,
        config=AliyunMediaConfig(background_gain=0.2),
    )

    video = timeline["VideoTracks"][0]["VideoTrackClips"][0]
    assert video["Effects"] == [{"Type": "Volume", "Gain": 0}]
    assert timeline["AudioTracks"][0]["AudioTrackClips"][0]["Effects"] == [
        {"Type": "Volume", "Gain": 0.2}
    ]
    # The two generated lines overlap, so they are placed on separate dialogue tracks.
    assert len(timeline["AudioTracks"]) == 3
    assert timeline["SubtitleTracks"][0]["SubtitleTrackClips"][1]["TimelineIn"] == 0.7
    assert timeline["SubtitleTracks"][0]["SubtitleTrackClips"][1]["Content"] == "Who are you?"


def test_demix_classifier_uses_result_type_names():
    vocal, background = classify_demix_outputs(
        [
            ("Result/vocal", "https://example.com/a.wav"),
            ("Result/accompaniment", "https://example.com/b.wav"),
        ]
    )
    assert vocal == "https://example.com/a.wav"
    assert background == "https://example.com/b.wav"


async def test_prepare_uses_upstream_clean_video_and_cloud_music_demix():
    ice = FakeICE()
    store = FakeStore()
    service = AliyunMediaService(ice, store)

    result = await service.prepare(request_for())

    assert result.video_url.endswith("/clean.mp4")
    assert result.source_audio_url.endswith("/source.wav")
    assert result.vocal_url.endswith("demix-vocal.wav")
    assert result.background_url.endswith("demix-accompaniment.wav")
    assert result.task_ids == {"music_demix": "demix-job"}
    submission = ice.demix_submissions[0]
    assert submission["function_name"] == "MusicDemix"
    assert "{resultType}" in submission["output_media"]


async def test_assemble_uploads_srt_and_returns_signed_video():
    ice = FakeICE()
    store = FakeStore()
    service = AliyunMediaService(ice, store)
    media = MediaArtifacts(
        video_url="https://bucket.oss-cn-beijing.aliyuncs.com/clean.mp4",
        background_url="https://bucket.oss-cn-beijing.aliyuncs.com/background.wav",
    )

    result = await service.assemble(
        request_for(subtitle_mode="soft"),
        media,
        transcript(),
        dubbing(),
    )

    assert result.provider == "aliyun_ice"
    assert result.task_id == "timeline-job-1"
    assert result.video_url.startswith("https://signed.example/")
    assert result.subtitle_url.startswith("https://signed.example/")
    uploaded_srt = next(
        content for key, (content, _content_type) in store.uploads.items() if key.endswith(".srt")
    )
    assert b"Who are you?" in uploaded_srt
    submission = ice.timeline_submissions[0]
    assert submission["output_config"]["Bitrate"] == 3000
    assert "SubtitleTracks" not in submission["timeline"]


async def test_generated_azure_clip_is_stored_as_oss_artifact():
    service = AliyunMediaService(FakeICE(), FakeStore())
    result = await service.save_artifact("line-1.mp3", b"mp3-bytes")
    assert result["url"].endswith("line-1.mp3")
    assert result["download_url"].startswith("https://signed.example/")


def test_transcript_to_srt_preserves_millisecond_timing():
    text = transcript_to_srt(transcript())
    assert "00:00:00,700 --> 00:00:02,200" in text
