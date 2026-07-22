from __future__ import annotations

from cineflow.providers.aliyun_media import collect_i_production_outputs


class FakeStore:
    @staticmethod
    def canonical_url(object_key: str) -> str:
        return f"https://bucket.oss-cn-beijing.aliyuncs.com/{object_key}"


def test_i_production_output_files_accept_json_string_and_remove_duplicates():
    result = {
        "OutputUrls": [
            "https://bucket.oss-cn-beijing.aliyuncs.com/out/vocal.wav"
        ],
        "OutputFiles": '["out/vocal.wav","out/background.wav"]',
        "Result": {
            "vocal": "https://bucket.oss-cn-beijing.aliyuncs.com/out/vocal.wav"
        },
    }

    outputs = collect_i_production_outputs(result, FakeStore())

    assert outputs == [
        (
            "OutputUrls/0",
            "https://bucket.oss-cn-beijing.aliyuncs.com/out/vocal.wav",
        ),
        (
            "OutputFiles/1",
            "https://bucket.oss-cn-beijing.aliyuncs.com/out/background.wav",
        ),
    ]


def test_i_production_output_files_accept_mapping_values():
    result = {
        "OutputFiles": {
            "vocal": "out/vocal.wav",
            "accompaniment": "out/accompaniment.wav",
        }
    }

    outputs = collect_i_production_outputs(result, FakeStore())

    assert [url for _label, url in outputs] == [
        "https://bucket.oss-cn-beijing.aliyuncs.com/out/vocal.wav",
        "https://bucket.oss-cn-beijing.aliyuncs.com/out/accompaniment.wav",
    ]
