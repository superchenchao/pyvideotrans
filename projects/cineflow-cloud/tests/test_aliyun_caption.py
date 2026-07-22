import httpx

from cineflow.models import JobRequest, OCRRegion, VideoProbe
from cineflow.providers.aliyun_caption import AliyunCaptionExtractor, caption_output_urls


class FakeICE:
    configured = True

    def __init__(self):
        self.submission = None

    async def submit_i_production(self, **kwargs):
        self.submission = kwargs
        return "caption-job"

    async def wait_i_production(self, job_id):
        assert job_id == "caption-job"
        return {
            "Status": "Success",
            "RequestId": "request-id",
            "OutputUrls": [
                "https://bucket.oss-cn-beijing.aliyuncs.com/captions/job/source.srt"
            ],
        }


class FakeStore:
    configured = True

    @staticmethod
    def key(*parts):
        return "/".join(parts)

    @staticmethod
    def canonical_url(object_key):
        return f"https://bucket.oss-cn-beijing.aliyuncs.com/{object_key}"

    @staticmethod
    def oss_uri(object_key):
        return f"oss://bucket/{object_key}"

    @staticmethod
    def canonicalize_oss_input(value):
        return str(value).split("?", 1)[0]

    @staticmethod
    def object_key_from_url(value):
        marker = "bucket.oss-cn-beijing.aliyuncs.com/"
        return value.split(marker, 1)[1] if marker in value else None

    @staticmethod
    def signed_url(object_key):
        return f"https://download.example/{object_key}?signed=1"


def request_for():
    return JobRequest(
        input_url="https://bucket.oss-cn-beijing.aliyuncs.com/original.mp4?signature=old",
        clean_video_url="https://bucket.oss-cn-beijing.aliyuncs.com/clean.mp4",
        probe=VideoProbe(duration_seconds=60, input_bytes=10_000_000),
        target_language="en-US",
        ocr_region=OCRRegion(x=0.1, y=0.7, width=0.8, height=0.2),
    )


async def test_caption_extractor_calls_cloud_api_and_parses_srt():
    async def handler(request):
        assert request.url.host == "download.example"
        return httpx.Response(
            200,
            text="1\n00:00:00,000 --> 00:00:01,200\n云端字幕\n",
        )

    ice = FakeICE()
    extractor = AliyunCaptionExtractor(
        ice,
        FakeStore(),
        transport=httpx.MockTransport(handler),
    )
    transcript = await extractor.extract(request_for())

    assert transcript.provider == "aliyun_caption_extraction"
    assert transcript.lines[0].text == "云端字幕"
    assert transcript.metadata["ocr_local_inference"] is False
    assert ice.submission["function_name"] == "CaptionExtraction"
    assert ice.submission["input_media"].endswith("/original.mp4")
    assert not ice.submission["input_media"].endswith("/clean.mp4")
    assert ice.submission["job_params"]["fps"] == 5
    assert ice.submission["job_params"]["roi"] == [[0.7, 0.9], [0.1, 0.9]]


def test_caption_output_url_parser_accepts_output_files_and_deduplicates():
    result = {
        "OutputUrls": ["https://example.com/source.srt"],
        "OutputFiles": '["captions/source.srt"]',
        "Result": '{"url":"https://example.com/source.srt"}',
    }
    urls = caption_output_urls(result, FakeStore())
    assert urls == [
        "https://example.com/source.srt",
        "https://bucket.oss-cn-beijing.aliyuncs.com/captions/source.srt",
    ]
