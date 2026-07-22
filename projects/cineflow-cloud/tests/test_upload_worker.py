from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cineflow.ingest_models import (
    MultipartRegistration,
    UploadCompleteRequest,
    UploadSessionRequest,
    UploadState,
)
from cineflow.providers.aliyun_sts import AliyunSTSClient, TemporaryCredentials
from cineflow.state import FileStateStore
from cineflow.upload_client import LocalFingerprint, part_plan
from cineflow.upload_worker import UploadRuntime, UploadWorkerSettings


class FakeSTS:
    configured = True

    @staticmethod
    def object_policy(bucket, object_key, *, allow_read=True):
        return AliyunSTSClient.object_policy(
            bucket,
            object_key,
            allow_read=allow_read,
        )

    async def aassume_role(self, **_kwargs):
        return TemporaryCredentials(
            access_key_id="tmp-id",
            access_key_secret="tmp-secret",
            security_token="tmp-token",
            expiration="2030-01-01T00:00:00Z",
        )


class FakeOSS:
    configured = True

    def __init__(self):
        self.objects = {}
        self.aborted = []
        self.deleted = []

    def key(self, *parts):
        return "sources/" + "/".join(str(part).strip("/") for part in parts)

    @staticmethod
    def canonical_url(object_key):
        return f"https://bucket.oss-cn-beijing.aliyuncs.com/{object_key}"

    @staticmethod
    def signed_url(object_key, *, method="GET"):
        return f"https://signed.example/{object_key}?method={method}"

    def head_object(self, object_key):
        return self.objects[object_key]

    def object_exists(self, object_key):
        return object_key in self.objects

    def delete_object(self, object_key):
        self.deleted.append(object_key)
        self.objects.pop(object_key, None)

    def abort_multipart_upload(self, object_key, upload_id):
        self.aborted.append((object_key, upload_id))


async def test_upload_session_persists_without_temporary_secret_and_completes(tmp_path):
    store = FileStateStore(tmp_path / "state")
    oss = FakeOSS()
    runtime = UploadRuntime(
        UploadWorkerSettings(
            state_dir=str(tmp_path / "state"),
            oss_bucket="bucket",
            aliyun_role_arn="acs:ram::123:role/uploader",
            aliyun_access_key_id="server-id",
            aliyun_access_key_secret="server-secret",
        ),
        state_store=store,
        oss_store=oss,
        sts_client=FakeSTS(),
    )

    session = await runtime.create_session(
        UploadSessionRequest(
            filename="episode 1.mp4",
            size_bytes=1234,
            content_type="video/mp4",
            sha256="a" * 64,
            project_id="series-a",
        )
    )
    assert session.credentials is not None
    assert session.credentials.access_key_id == "tmp-id"
    assert session.object_key.endswith("episode-1.mp4")

    persisted = await runtime._load(session.session_id)
    assert persisted.credentials is None
    uploading = await runtime.register_multipart(
        session.session_id,
        MultipartRegistration(upload_id="multipart-1"),
    )
    assert uploading.state == UploadState.UPLOADING

    oss.objects[session.object_key] = {
        "content_length": 1234,
        "content_type": "video/mp4",
        "etag": "etag",
        "metadata": {"sha256": "a" * 64},
    }
    completed, validation = await runtime.complete(
        session.session_id,
        UploadCompleteRequest(size_bytes=1234, sha256="a" * 64),
    )
    assert validation.valid is True
    assert completed.state == UploadState.COMPLETED
    assert completed.download_url.startswith("https://signed.example/")


async def test_abort_records_multipart_and_deletes_partial_object(tmp_path):
    oss = FakeOSS()
    runtime = UploadRuntime(
        UploadWorkerSettings(
            state_dir=str(tmp_path / "state"),
            oss_bucket="bucket",
            aliyun_role_arn="acs:ram::123:role/uploader",
            aliyun_access_key_id="server-id",
            aliyun_access_key_secret="server-secret",
        ),
        state_store=FileStateStore(tmp_path / "state"),
        oss_store=oss,
        sts_client=FakeSTS(),
    )
    session = await runtime.create_session(
        UploadSessionRequest(filename="input.mp4", size_bytes=100)
    )
    await runtime.register_multipart(
        session.session_id,
        MultipartRegistration(upload_id="upload-id"),
    )
    oss.objects[session.object_key] = {
        "content_length": 20,
        "metadata": {},
    }

    aborted = await runtime.abort(session.session_id, delete_object=True)
    assert aborted.state == UploadState.ABORTED
    assert oss.aborted == [(session.object_key, "upload-id")]
    assert oss.deleted == [session.object_key]


async def test_stale_cleanup_does_not_delete_completed_session_by_default(tmp_path):
    oss = FakeOSS()
    state = FileStateStore(tmp_path / "state")
    runtime = UploadRuntime(
        UploadWorkerSettings(
            state_dir=str(tmp_path / "state"),
            oss_bucket="bucket",
            aliyun_role_arn="acs:ram::123:role/uploader",
            aliyun_access_key_id="server-id",
            aliyun_access_key_secret="server-secret",
        ),
        state_store=state,
        oss_store=oss,
        sts_client=FakeSTS(),
    )
    session = await runtime.create_session(
        UploadSessionRequest(filename="done.mp4", size_bytes=100)
    )
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    completed = session.model_copy(
        update={
            "credentials": None,
            "state": UploadState.COMPLETED,
            "updated_at": old,
            "completed_at": old,
        }
    )
    await runtime._save(completed)
    result = await runtime.cleanup(
        older_than_seconds=900,
        delete_completed=False,
    )
    assert result["cleaned"] == []


def test_scoped_sts_policy_and_part_plan(tmp_path):
    policy = AliyunSTSClient.object_policy(
        "bucket",
        "sources/job/input.mp4",
    )
    statement = policy["Statement"][0]
    assert statement["Resource"] == ["acs:oss:*:*:bucket/sources/job/input.mp4"]
    assert "oss:AbortMultipartUpload" in statement["Action"]

    assert part_plan(10, 4) == [(1, 0, 4), (2, 4, 4), (3, 8, 2)]
    file_path = tmp_path / "input.mp4"
    file_path.write_bytes(b"data")
    fingerprint = LocalFingerprint.from_path(file_path)
    assert fingerprint.size_bytes == 4
