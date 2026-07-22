from cineflow.fusion import fuse_speakers
from cineflow.models import CandidateScore, LineEvidence, SubtitleLine


def line(line_id):
    return SubtitleLine(
        line_id=line_id,
        start_ms=(line_id - 1) * 1000,
        end_ms=line_id * 1000,
        text="x",
    )


def test_visual_active_speaker_can_correct_ambiguous_audio():
    decisions = fuse_speakers(
        [line(1)],
        [
            LineEvidence(
                line_id=1,
                audio=[
                    CandidateScore(character_id="a", score=0.55),
                    CandidateScore(character_id="b", score=0.60),
                ],
                visual=[
                    CandidateScore(character_id="a", score=0.96),
                    CandidateScore(character_id="b", score=0.05),
                ],
            )
        ],
    )
    assert decisions[0].character_id == "a"


def test_offscreen_line_ignores_misleading_visible_face():
    decisions = fuse_speakers(
        [line(1)],
        [
            LineEvidence(
                line_id=1,
                offscreen=True,
                audio=[CandidateScore(character_id="a", score=0.91)],
                visual=[CandidateScore(character_id="b", score=0.99)],
                text=[CandidateScore(character_id="a", score=0.80)],
            )
        ],
    )
    assert decisions[0].character_id == "a"


def test_sequence_decoder_suppresses_one_short_spurious_switch():
    rows = [
        LineEvidence(line_id=1, audio=[CandidateScore(character_id="a", score=0.95)]),
        LineEvidence(
            line_id=2,
            audio=[
                CandidateScore(character_id="a", score=0.56),
                CandidateScore(character_id="b", score=0.58),
            ],
        ),
        LineEvidence(line_id=3, audio=[CandidateScore(character_id="a", score=0.95)]),
    ]
    decisions = fuse_speakers([line(1), line(2), line(3)], rows)
    assert [item.character_id for item in decisions] == ["a", "a", "a"]


def test_azure_ssml_is_single_prosody_and_escaped():
    from cineflow.providers.azure_tts import AzureTTSClient

    ssml = AzureTTSClient.build_ssml(
        "en-US", "en-US-AvaMultilingualNeural", "A & B < C"
    )
    assert ssml.count("<prosody") == 1
    assert "A &amp; B &lt; C" in ssml
    assert ssml.endswith("</speak>")
