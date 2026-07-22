from cineflow.models import JobRequest, LineEvidence, SubtitleLine, Transcript, VideoProbe
from cineflow.providers.http_bundle import ProductionProviders


class FakeSpeaker:
    async def post(self, *_args, **_kwargs):
        return {
            "evidence": [
                {
                    "line_id": 1,
                    "audio": [
                        {"character_id": "a", "score": 0.55},
                        {"character_id": "b", "score": 0.56},
                    ],
                    "visual": [],
                    "text": [],
                    "offscreen": True,
                    "overlap_speech": False,
                    "av_sync_confidence": 0.0,
                }
            ]
        }


class FakeTranslator:
    called = False

    async def reason_speakers(self, _request, _transcript, _evidence, ambiguous):
        self.called = True
        assert ambiguous == {1}
        return [
            LineEvidence(
                line_id=1,
                text=[{"character_id": "a", "score": 0.95}],
            )
        ]


async def test_low_confidence_line_gets_batched_text_evidence():
    providers = object.__new__(ProductionProviders)
    providers.speaker = FakeSpeaker()
    providers.translator = FakeTranslator()
    request = JobRequest(
        input_url="https://example.com/input.mp4",
        probe=VideoProbe(duration_seconds=60, input_bytes=1_000_000),
        target_language="en-US",
        target_voice="en-US-AvaMultilingualNeural",
    )
    transcript = Transcript(
        language="zh-CN",
        lines=[SubtitleLine(line_id=1, start_ms=0, end_ms=1000, text="小雪，你来了")],
    )
    evidence = await providers.analyze_speakers(request, transcript)
    assert providers.translator.called is True
    assert evidence[0].text[0].character_id == "a"
