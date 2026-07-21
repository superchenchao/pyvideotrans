import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import videotrans.subtitle_removal as subtitle_removal
from videotrans.subtitle_removal import cloud
from videotrans.subtitle_removal.aliyun_ims import ImsClient, ImsConfig, classify_status as classify_ims
from videotrans.subtitle_removal.caca_api import CacaClient, CacaConfig, classify_status as classify_caca
from videotrans.subtitle_removal.cloud_storage import OssConfig
from videotrans.subtitle_removal import cloud_storage
from videotrans.subtitle_removal.cloud_state import write_state
from videotrans.task.trans_create import validate_cloud_clean_video
from videotrans.task.trans_create import TransCreate


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class RecordingSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.responses.pop(0))


def test_oss_config_derives_public_endpoint_and_clamps_signed_url():
    config = OssConfig.from_mapping({
        "subtitle_oss_region": "cn-beijing",
        "subtitle_oss_bucket": "private-bucket",
        "subtitle_oss_endpoint": "",
        "subtitle_oss_signed_url_hours": 999,
        "subtitle_oss_transfer_threads": 999,
    })

    assert config.endpoint == "https://oss-cn-beijing.aliyuncs.com"
    assert config.signed_url_seconds == 7 * 86400
    assert config.transfer_threads == 16
    assert config.transfer_pool_size == 18


def test_oss_config_defaults_to_eight_transfer_threads():
    config = OssConfig.from_mapping({})

    assert config.transfer_threads == 8
    assert config.transfer_pool_size == 10
    assert OssConfig("bucket", "region", "https://endpoint", transfer_threads=0).transfer_threads == 1
    assert OssConfig("bucket", "region", "https://endpoint", transfer_threads=99).transfer_threads == 16


def test_oss_upload_uses_configured_threads_and_larger_pool(tmp_path, monkeypatch):
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video-data")
    captured = {}

    class FakeStore:
        def __init__(self, *, root):
            self.root = root

    def fake_resumable_upload(bucket, object_key, filename, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(etag="etag")

    monkeypatch.setitem(
        sys.modules,
        "oss2",
        SimpleNamespace(ResumableStore=FakeStore, resumable_upload=fake_resumable_upload),
    )
    monkeypatch.setattr(
        cloud_storage,
        "create_bucket",
        lambda config, *, pool_size=4: captured.update(pool_size=pool_size) or object(),
    )
    monkeypatch.setattr(
        cloud_storage,
        "head_object",
        lambda config, object_key: {
            "content_length": source.stat().st_size,
            "etag": "etag",
            "last_modified": None,
        },
    )
    config = OssConfig(
        "bucket", "cn-shanghai", "https://oss-cn-shanghai.aliyuncs.com",
        transfer_threads=12,
    )

    cloud_storage.upload_file(
        config,
        source,
        "input.mp4",
        checkpoint_root=tmp_path / "checkpoint",
    )

    assert captured["num_threads"] == 12
    assert captured["pool_size"] == 14


def test_oss_download_uses_configured_threads_and_larger_pool(tmp_path, monkeypatch):
    destination = tmp_path / "clean.mp4"
    payload = b"clean-video"
    captured = {}

    class FakeStore:
        def __init__(self, *, root):
            self.root = root

    def fake_resumable_download(bucket, object_key, filename, **kwargs):
        captured.update(kwargs)
        Path(filename).write_bytes(payload)

    monkeypatch.setitem(
        sys.modules,
        "oss2",
        SimpleNamespace(ResumableStore=FakeStore, resumable_download=fake_resumable_download),
    )
    monkeypatch.setattr(
        cloud_storage,
        "create_bucket",
        lambda config, *, pool_size=4: captured.update(pool_size=pool_size) or object(),
    )
    monkeypatch.setattr(
        cloud_storage,
        "head_object",
        lambda config, object_key: {
            "content_length": len(payload),
            "etag": "etag",
            "last_modified": None,
        },
    )
    config = OssConfig(
        "bucket", "cn-shanghai", "https://oss-cn-shanghai.aliyuncs.com",
        transfer_threads=8,
    )

    cloud_storage.download_object(config, "output.mp4", destination)

    assert destination.read_bytes() == payload
    assert captured["num_threads"] == 8
    assert captured["pool_size"] == 10


def test_caca_submit_uses_signed_video_link_and_safe_default_region(monkeypatch):
    monkeypatch.setenv("PYVIDEOTRANS_CACA_SECRET_ID", "id-value")
    monkeypatch.setenv("PYVIDEOTRANS_CACA_SECRET_KEY", "key-value")
    session = RecordingSession([{"isOk": 1, "taskId": "task-1"}])
    client = CacaClient(CacaConfig(), session=session)

    result = client.submit("https://signed.example/video.mp4?token=hidden", [0.1, 0.7, 0.8, 0.1])

    assert result["taskId"] == "task-1"
    url, kwargs = session.calls[0]
    assert url.endswith("/user/pub/cacaLinkPullAsyc")
    assert kwargs["json"]["video_link"].startswith("https://signed.example/")
    assert [kwargs["json"][key] for key in ("x1", "y1", "x2", "y2")] == [0, 0, 0, 0]
    assert kwargs["json"]["secret_id"] == "id-value"
    assert kwargs["allow_redirects"] is False


def test_caca_status_classification():
    assert classify_caca({"status": "processing"})[0] == "processing"
    assert classify_caca({"status": "finished", "media": "https://out"}) == (
        "success", "https://out"
    )
    assert classify_caca({"status": "failed", "lastError": "bad"}) == (
        "failed", "bad"
    )


def test_ims_submit_uses_videodetext_oss_and_premium_model():
    client = ImsClient(ImsConfig(region="cn-shanghai", quality="premium"), client=object())
    captured = {}

    def fake_call(action, parameters):
        captured.update({"action": action, "parameters": parameters})
        return {"JobId": "job-1"}

    client._call = fake_call
    result = client.submit(
        input_oss_uri="oss://bucket/in.mp4",
        output_oss_uri="oss://bucket/out.mp4",
        normalized_rect=[0.1, 0.7, 0.8, 0.1],
        name="test",
    )

    assert result["JobId"] == "job-1"
    assert captured["action"] == "SubmitIProductionJob"
    params = captured["parameters"]
    assert params["FunctionName"] == "VideoDetext"
    assert json.loads(params["Input"])["Media"] == "oss://bucket/in.mp4"
    assert json.loads(params["Output"])["Media"] == "oss://bucket/out.mp4"
    assert json.loads(params["JobParams"])["LimitRegion"] == [[0.1, 0.7, 0.8, 0.1]]
    assert params["ModelId"] == "algo-video-detext-new"


def test_ims_status_classification():
    assert classify_ims({"Status": "Queuing"}) == ("processing", "queuing")
    assert classify_ims({"Status": "Analysing"}) == ("processing", "analysing")
    assert classify_ims({"Status": "Success"}) == ("success", "success")
    assert classify_ims({"Status": "Fail", "Message": "bad"}) == ("failed", "bad")


def test_cloud_endpoints_require_https(monkeypatch):
    with pytest.raises(ValueError, match="HTTPS"):
        OssConfig("bucket", "cn-shanghai", "http://oss.example").validate()
    monkeypatch.setenv("PYVIDEOTRANS_CACA_SECRET_ID", "id")
    monkeypatch.setenv("PYVIDEOTRANS_CACA_SECRET_KEY", "key")
    with pytest.raises(ValueError, match="HTTPS"):
        CacaClient(CacaConfig(base_url="http://api.example"))


def test_ims_rejects_unsupported_region():
    with pytest.raises(ValueError, match="不支持"):
        ImsClient(ImsConfig(region="cn-hangzhou"), client=object())


def test_unknown_submit_is_not_retried(tmp_path, monkeypatch):
    source = tmp_path / "api.mp4"
    source.write_bytes(b"video")
    output = tmp_path / "clean.mp4"
    settings = {
        "subtitle_oss_region": "cn-shanghai",
        "subtitle_oss_bucket": "bucket",
        "subtitle_oss_endpoint": "https://oss-cn-shanghai.aliyuncs.com",
    }
    submit_calls = []

    class FailingClient:
        def __init__(self, config):
            pass

        def submit(self, *args, **kwargs):
            submit_calls.append(1)
            raise TimeoutError("network timeout")

    monkeypatch.setattr(cloud, "validate_cloud_configuration", lambda *args: None)
    monkeypatch.setattr(cloud, "CacaClient", FailingClient)
    monkeypatch.setattr(cloud, "upload_file", lambda *args, **kwargs: {})
    monkeypatch.setattr(cloud, "signed_download_url", lambda *args: "https://signed/video")

    kwargs = dict(
        provider="caca_link",
        input_file=source.as_posix(),
        output_file=output.as_posix(),
        normalized_rect=[0.1, 0.7, 0.8, 0.1],
        duration_ms=1000,
        identity={"source": "same"},
        settings_values=settings,
    )
    with pytest.raises(RuntimeError, match="不会自动重提"):
        cloud.remove_burned_subtitles_cloud(**kwargs)
    with pytest.raises(RuntimeError, match="不会自动重提"):
        cloud.remove_burned_subtitles_cloud(**kwargs)

    assert len(submit_calls) == 1
    state = json.loads(Path(f"{output}.cloud.json").read_text(encoding="utf-8"))
    assert state["status"] == "submit_unknown"


@pytest.mark.parametrize(
    ("provider", "expected_keys"),
    [
        ("caca_link", ["prefix/input.mp4"]),
        ("aliyun_ims", ["prefix/input.mp4", "prefix/output.mp4"]),
    ],
)
def test_cleanup_deletes_only_owned_oss_objects(
        tmp_path, monkeypatch, provider, expected_keys):
    output = tmp_path / "clean.mp4"
    output.write_bytes(b"verified-local-result")
    write_state(
        f"{output}.cloud.json",
        {
            "status": "downloaded",
            "input_object_key": "prefix/input.mp4",
            "output_object_key": "prefix/output.mp4",
        },
    )
    deleted = []
    monkeypatch.setattr(cloud, "delete_object", lambda config, key: deleted.append(key))

    result = cloud.cleanup_cloud_objects(
        provider=provider,
        output_file=output.as_posix(),
        settings_values={
            "subtitle_cloud_delete_after_download": True,
            "subtitle_oss_region": "cn-shanghai",
            "subtitle_oss_bucket": "bucket",
            "subtitle_oss_endpoint": "https://oss-cn-shanghai.aliyuncs.com",
        },
    )

    assert deleted == expected_keys
    assert result["status"] == "cleanup_complete"
    state = json.loads(Path(f"{output}.cloud.json").read_text(encoding="utf-8"))
    assert state["deleted_object_keys"] == expected_keys


def test_cleanup_failure_keeps_local_result_and_records_retry(tmp_path, monkeypatch):
    output = tmp_path / "clean.mp4"
    output.write_bytes(b"verified-local-result")
    write_state(
        f"{output}.cloud.json",
        {"status": "downloaded", "input_object_key": "prefix/input.mp4"},
    )
    attempts = []

    def fail_delete(*args):
        attempts.append(args)
        raise RuntimeError("denied")

    monkeypatch.setattr(cloud, "delete_object", fail_delete)
    monkeypatch.setattr(cloud.time, "sleep", lambda _seconds: None)

    result = cloud.cleanup_cloud_objects(
        provider="caca_link",
        output_file=output.as_posix(),
        settings_values={
            "subtitle_cloud_delete_after_download": True,
            "subtitle_oss_region": "cn-shanghai",
            "subtitle_oss_bucket": "bucket",
            "subtitle_oss_endpoint": "https://oss-cn-shanghai.aliyuncs.com",
        },
    )

    assert output.read_bytes() == b"verified-local-result"
    assert len(attempts) == 3
    assert result["status"] == "cleanup_pending"
    assert "denied" in result["errors"][0]


def test_cloud_clean_result_checks_metadata_and_full_decode(tmp_path, monkeypatch):
    result = tmp_path / "clean.mp4"
    result.write_bytes(b"video")
    decode_calls = []
    monkeypatch.setattr(
        "videotrans.task.trans_create.tools.get_video_info",
        lambda path: {
            "video_streams": 1,
            "width": 1080,
            "height": 1920,
            "video_fps": 25,
            "time": 10_000,
        },
    )
    monkeypatch.setattr(
        "videotrans.task.trans_create.run_work_video_ffmpeg",
        lambda args, **kwargs: decode_calls.append((args, kwargs)) or True,
    )

    info = validate_cloud_clean_video(
        result.as_posix(), target_width=1080, target_height=1920, duration_ms=10_000
    )

    assert info["video_fps"] == 25
    assert decode_calls and decode_calls[0][0][-3:] == ["-f", "null", "-"]
    assert decode_calls[0][1]["duration_ms"] == 10_000


def test_cloud_clean_result_rejects_fps_change(tmp_path, monkeypatch):
    result = tmp_path / "clean.mp4"
    result.write_bytes(b"video")
    monkeypatch.setattr(
        "videotrans.task.trans_create.tools.get_video_info",
        lambda path: {
            "video_streams": 1,
            "width": 1080,
            "height": 1920,
            "video_fps": 30,
            "time": 10_000,
        },
    )

    with pytest.raises(Exception, match="帧率"):
        validate_cloud_clean_video(
            result.as_posix(), target_width=1080, target_height=1920, duration_ms=10_000
        )


def test_transcreate_routes_cloud_provider_without_local_engine(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    work = tmp_path / "source-working-api-25fps-6000k-noaudio.mp4"
    source.write_bytes(b"source")
    work.write_bytes(b"work")
    task = object.__new__(TransCreate)
    task.cfg = type("Cfg", (), {
        "remove_burned_subtitles": True,
        "subtitle_removal_rect": [0.1, 0.7, 0.8, 0.1],
        "subtitle_removal_aspect_ratio": 1080 / 1920,
        "subtitle_removal_provider": "caca_link",
        "cache_folder": tmp_path.as_posix(),
        "name": source.as_posix(),
    })()
    task.video_info = {"width": 1080, "height": 1920, "time": 5000}
    task.source_display_width = 1080
    task.source_display_height = 1920
    task.visual_source = work.as_posix()
    task.ocr_source = work.as_posix()
    task.visual_work_profile = {"fps": 25, "rate_control": "constrained-6000k"}
    task.signal = lambda **kwargs: None
    task._exit = lambda: False
    captured = {}
    cleanup_calls = []

    def fake_cloud(**kwargs):
        captured.update(kwargs)
        Path(kwargs["output_file"]).write_bytes(b"clean")
        return kwargs["output_file"]

    monkeypatch.setattr(subtitle_removal, "cloud_strategy_key", lambda *args: {"provider": "caca_link"})
    monkeypatch.setattr(subtitle_removal, "remove_burned_subtitles_cloud", fake_cloud)
    monkeypatch.setattr(
        subtitle_removal,
        "remove_burned_subtitles",
        lambda **kwargs: pytest.fail("cloud provider must not call local remover"),
    )
    monkeypatch.setattr(
        "videotrans.task.trans_create.validate_cloud_clean_video",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        subtitle_removal,
        "cleanup_cloud_objects",
        lambda **kwargs: cleanup_calls.append(
            Path(f"{kwargs['output_file']}.json").is_file()
        ) or {"status": "cleanup_complete"},
    )

    task._prepare_clean_visual_source()

    assert task.visual_source.endswith("source-without-burned-subtitles-caca_link.mp4")
    assert task.ocr_source == work.as_posix()
    assert captured["input_file"] == work.resolve().as_posix()
    assert captured["normalized_rect"] == pytest.approx([0.1, 0.7, 0.8, 0.1])
    metadata = json.loads(
        Path(f"{task.visual_source}.json").read_text(encoding="utf-8")
    )
    assert metadata["subtitle_removal_provider"] == "caca_link"
    assert metadata["working_video"]["fps"] == 25
    assert cleanup_calls == [True]
