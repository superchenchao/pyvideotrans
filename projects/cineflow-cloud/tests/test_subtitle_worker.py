from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from cineflow.media_probe import MediaProbeResult, validate_probe
from cineflow.providers.subtitle_aliyun import AliyunVideoDetextProvider
from cineflow.providers.subtitle_caca import CacaSubtitleConfig, CacaSubtitleProvider
from cineflow.state import FileStateStore
from cineflow.subtitle_models import (
    NormalizedRegion,
    ProviderSubmission,
    SubtitleJobState,
    SubtitleProviderName,
    SubtitleRemovalJob,
    SubtitleRemovalRequest,
    TimeRange,
)
from cineflow.subtitle_worker import SubtitleRuntime, SubtitleWorkerSettings


class FakeStore:
    configured = True

    def __init__(self):
        self.objects = {}
        self.deleted = []

    def key(self, *parts):
        return "detext/" + "/".join(str(part).strip("/") for part in parts)

    @staticmethod
    def canonical_url(object_key):
        return f"https://bucket.oss-cn-beijing.aliyuncs.com/{object_key}"

    @staticmethod
    def oss_uri(object_key):
        return f"oss://bucket/{object_key}"

    @staticmethod
    def signed_url(object_key, *, method="GET"):
        return f"https://signed.example/{object_key}?method={method}"

    @staticmethod
    def object_key_from_url(value):
        marker = "bucket.oss-cn-beijing.aliyuncs.com/"
        if marker in value:
            return value.split(marker, 1)[1].split("?", 1)[0]
        if value.startswith("oss://bucket/"):
            return value.removeprefix("oss://bucket/")
        return None

    def object_exists(self, object_key):
        return object_key in self.objects

    def head_object(self, object_key):
        return self.objects[object_key]

    def put_file(self, object_key, filename, *, content_type="", headers=None):
        del headers
        self.objects[object_key] = {
            "content_length": filename.stat().st_size,
            "content_type": content_type,
            "metadata": {},
            "etag": "etag",
        }

    def delete_object(self, object_key):
        self.deleted.append(object_key)
        self.objects.pop(object_key, None)


class FakeProvider:
    name = "aliyun"
    configured = True
    detail = "ready"

    def __init__(self, store):
        self.store = store
        self.submits = 0
        self.cancels = 0

    async def submit(self, request, *, job_id, output_object_key):
        del request, job_id
        self.submits += 1
        self.store.objects[output_object_key] = {
            "content_length": 1024,
            "content_type": "video/mp4",
            "metadata": {},
            "etag": "etag",
        }
        return ProviderSubmission(
            provider=self.name,
            external_job_id="provider-job",
            output_object_key=output_object_key,
            output_url=self.store.canonical_url(output_object_key),
            completed=False,
        )

    async def wait(self, submission):
        return submission.model_copy(update={"completed": True})

    async def cancel(self, submission):
        del submission
        self.cancels += 1


async def wait_for_job(runtime, job_id):
    for _ in range(200):
        job = await runtime._load(job_id)
        if job.state in {
            SubtitleJobState.SUCCEEDED,
            SubtitleJobState.DEGRADED,
            SubtitleJobState.FAILED,
            SubtitleJobState.CANCELED,
        }:
            return job
        await asyncio.sleep(0.01)
    raise AssertionError("subtitle-removal job did not finish")


async def test_subtitle_job_is_durable_and_normalizes_output_to_oss(tmp_path):
    store = FakeStore()
    provider = FakeProvider(store)
    runtime = SubtitleRuntime(
        SubtitleWorkerSettings(
            state_dir=str(tmp_path / "state"),
            oss_bucket="bucket",
        ),
        state_store=FileStateStore(tmp_path / "state"),
        oss_store=store,
        providers={"aliyun": provider},
    )
    accepted = await runtime.submit(
        SubtitleRemovalRequest(
            input_url="https://source.example/input.mp4",
            provider=SubtitleProviderName.ALIYUN,
            validate_output=False,
        )
    )
    completed = await wait_for_job(runtime, accepted.job_id)
    assert completed.state == SubtitleJobState.SUCCEEDED
    assert completed.selected_provider == "aliyun"
    assert completed.external_job_id == "provider-job"
    assert completed.output_url.startswith("https://signed.example/")
    assert completed.validation is not None and completed.validation.valid is True
    assert provider.submits == 1

    persisted = SubtitleRemovalJob.model_validate(
        await runtime.state.get("subtitle-removal-jobs", accepted.job_id)
    )
    assert persisted.state == SubtitleJobState.SUCCEEDED


async def test_recovery_resumes_existing_external_job_without_resubmission(tmp_path):
    store = FakeStore()
    provider = FakeProvider(store)
    state = FileStateStore(tmp_path / "state")
    runtime = SubtitleRuntime(
        SubtitleWorkerSettings(state_dir=str(tmp_path / "state"), oss_bucket="bucket"),
        state_store=state,
        oss_store=store,
        providers={"aliyun": provider},
    )
    output_key = "detext/recovered/output.mp4"
    store.objects[output_key] = {
        "content_length": 2048,
        "content_type": "video/mp4",
        "metadata": {},
        "etag": "etag",
    }
    now = datetime.now(timezone.utc).isoformat()
    submission = ProviderSubmission(
        provider="aliyun",
        external_job_id="existing-cloud-job",
        output_object_key=output_key,
        output_url=store.canonical_url(output_key),
        completed=False,
    )
    job = SubtitleRemovalJob(
        job_id="recover-job",
        state=SubtitleJobState.RUNNING,
        request=SubtitleRemovalRequest(
            input_url="https://source.example/input.mp4",
            provider=SubtitleProviderName.ALIYUN,
            validate_output=False,
        ),
        selected_provider="aliyun",
        external_job_id="existing-cloud-job",
        output_object_key=output_key,
        canonical_output_url=store.canonical_url(output_key),
        created_at=now,
        updated_at=now,
        metadata={"provider_submission": submission.model_dump(mode="json")},
    )
    await state.put("subtitle-removal-jobs", job.job_id, job.model_dump(mode="json"))

    recovered = await runtime.recover()
    assert recovered == [job.job_id]
    completed = await wait_for_job(runtime, job.job_id)
    assert completed.state == SubtitleJobState.SUCCEEDED
    assert provider.submits == 0


async def test_cleanup_keeps_old_successful_output_unless_explicitly_requested(tmp_path):
    store = FakeStore()
    provider = FakeProvider(store)
    state = FileStateStore(tmp_path / "state")
    runtime = SubtitleRuntime(
        SubtitleWorkerSettings(state_dir=str(tmp_path / "state"), oss_bucket="bucket"),
        state_store=state,
        oss_store=store,
        providers={"aliyun": provider},
    )
    output_key = "detext/old/output.mp4"
    store.objects[output_key] = {"content_length": 100, "metadata": {}}
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    job = SubtitleRemovalJob(
        job_id="old-success",
        state=SubtitleJobState.SUCCEEDED,
        request=SubtitleRemovalRequest(
            input_url="https://source.example/input.mp4",
            provider=SubtitleProviderName.ALIYUN,
            validate_output=False,
        ),
        selected_provider="aliyun",
        output_object_key=output_key,
        canonical_output_url=store.canonical_url(output_key),
        output_url=store.signed_url(output_key),
        created_at=old,
        updated_at=old,
        completed_at=old,
    )
    await state.put("subtitle-removal-jobs", job.job_id, job.model_dump(mode="json"))

    result = await runtime.cleanup(
        older_than_seconds=900,
        delete_successful_outputs=False,
    )
    assert result["cleaned"] == []
    assert output_key in store.objects
    persisted = await runtime._load(job.job_id)
    assert persisted.state == SubtitleJobState.SUCCEEDED


def test_video_detext_params_and_media_validation():
    request = SubtitleRemovalRequest(
        input_url="https://example.com/input.mp4",
        regions=[NormalizedRegion(x=0.1, y=0.8, width=0.8, height=0.15)],
        time_ranges=[TimeRange(start_seconds=2, end_seconds=10)],
        expected_duration_seconds=12,
        expected_width=1920,
        expected_height=1080,
        expected_fps=25,
    )
    params = AliyunVideoDetextProvider._job_params(request)
    assert params["LimitRegion"] == [[0.1, 0.8, 0.8, 0.15]]
    assert params["Time"] == [2.0, 10.0]

    validation = validate_probe(
        request,
        MediaProbeResult(
            duration_seconds=12.1,
            size_bytes=1_000_000,
            width=1920,
            height=1080,
            fps=25,
            codec="h264",
            has_audio=True,
        ),
    )
    assert validation.valid is True


def test_caca_flexible_nested_field_parser_requires_result_store():
    store = FakeStore()
    provider = CacaSubtitleProvider(
        CacaSubtitleConfig(base_url="https://caca.example", api_key="key"),
        store,
    )
    payload = {
        "data": {
            "job": {"taskId": "task-123", "state": "SUCCESS"},
            "result": {"downloadUrl": "https://cdn.example/clean.mp4"},
        }
    }
    assert provider.configured is True
    assert provider._task_id(payload) == "task-123"
    assert provider._status(payload) == "success"
    assert provider._output_url(payload) == "https://cdn.example/clean.mp4"
