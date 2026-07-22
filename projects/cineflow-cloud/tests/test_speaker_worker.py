from cineflow.models import JobRequest, SubtitleLine, Transcript, VideoProbe
from cineflow.providers.http_bundle import resolve_character_voice
from cineflow.speaker_worker import (
    AudioTurn,
    VisualTrack,
    associate_audio_speakers_with_faces,
    build_line_evidence,
)


def test_audio_speaker_is_associated_with_active_face():
    mapping = associate_audio_speakers_with_faces(
        [AudioTurn(start_ms=0, end_ms=1000, speaker_id="spk0")],
        [
            VisualTrack(
                start_ms=0,
                end_ms=1000,
                face_id="character_001",
                score=0.98,
                av_sync_confidence=0.95,
            )
        ],
    )
    assert mapping == {"spk0": "character_001"}


def test_line_evidence_uses_same_identity_for_audio_and_visual():
    transcript = Transcript(
        language="zh-CN",
        lines=[SubtitleLine(line_id=1, start_ms=0, end_ms=1000, text="你好")],
    )
    rows = build_line_evidence(
        transcript,
        [AudioTurn(start_ms=0, end_ms=1000, speaker_id="spk0")],
        [
            VisualTrack(
                start_ms=0,
                end_ms=1000,
                face_id="character_001",
                score=0.95,
            )
        ],
        {"spk0": "character_001"},
    )
    assert rows[0].audio[0].character_id == "character_001"
    assert rows[0].visual[0].character_id == "character_001"
    assert rows[0].offscreen is False


def test_character_specific_azure_voice_overrides_default():
    request = JobRequest(
        input_url="https://example.com/input.mp4",
        probe=VideoProbe(duration_seconds=60, input_bytes=1_000_000),
        target_language="en-US",
        target_voice="en-US-AvaMultilingualNeural",
        character_voices={"character_002": "en-US-AndrewMultilingualNeural"},
    )
    assert (
        resolve_character_voice(request, "character_002")
        == "en-US-AndrewMultilingualNeural"
    )
    assert (
        resolve_character_voice(request, "character_001")
        == "en-US-AvaMultilingualNeural"
    )
