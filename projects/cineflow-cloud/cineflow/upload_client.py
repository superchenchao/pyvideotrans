from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import mimetypes
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


class UploadClientError(RuntimeError):
    """Raised when the desktop-side upload cannot be completed safely."""


@dataclass(frozen=True)
class LocalFingerprint:
    path: str
    size_bytes: int
    mtime_ns: int

    @classmethod
    def from_path(cls, path: Path) -> LocalFingerprint:
        stat = path.stat()
        return cls(
            path=str(path.resolve()),
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )


class UploadManifest:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {}
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            self.data = {}
            return self.data
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UploadClientError(f"failed to read upload checkpoint: {exc}") from exc
        if not isinstance(value, dict):
            raise UploadClientError("upload checkpoint is not a JSON object")
        self.data = value
        return self.data

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(self.data, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)

    def update_part(self, part_number: int, etag: str) -> None:
        with self._lock:
            parts = self.data.setdefault("parts", {})
            parts[str(part_number)] = etag
        self.save()


def sha256_file(path: Path, *, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def checkpoint_path(root: Path, fingerprint: LocalFingerprint) -> Path:
    key = hashlib.sha256(
        f"{fingerprint.path}\0{fingerprint.size_bytes}\0{fingerprint.mtime_ns}".encode()
    ).hexdigest()
    return root / f"{key}.json"


class UploadWorkerClient:
    def __init__(self, base_url: str, bearer_token: str = "", timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}
        self.timeout = timeout

    def _request(self, method: str, path: str, *, payload: dict | None = None) -> dict:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.request(
                method,
                f"{self.base_url}{path}",
                headers=self.headers,
                json=payload,
            )
        if response.is_error:
            raise UploadClientError(
                f"upload worker {method} {path} failed: HTTP {response.status_code} "
                f"{response.text[:1000]}"
            )
        value = response.json()
        if not isinstance(value, dict):
            raise UploadClientError("upload worker returned a non-object response")
        return value

    def create_session(
        self,
        *,
        filename: str,
        size_bytes: int,
        content_type: str,
        sha256: str,
        project_id: str,
    ) -> dict:
        return self._request(
            "POST",
            "/v1/uploads/sessions",
            payload={
                "filename": filename,
                "size_bytes": size_bytes,
                "content_type": content_type,
                "sha256": sha256,
                "project_id": project_id,
            },
        )

    def refresh(self, session_id: str) -> dict:
        return self._request("POST", f"/v1/uploads/{session_id}/credentials")

    def register_multipart(self, session_id: str, upload_id: str) -> dict:
        return self._request(
            "POST",
            f"/v1/uploads/{session_id}/multipart",
            payload={"upload_id": upload_id},
        )

    def complete(self, session_id: str, size_bytes: int, sha256: str) -> dict:
        return self._request(
            "POST",
            f"/v1/uploads/{session_id}/complete",
            payload={"size_bytes": size_bytes, "sha256": sha256},
        )

    def abort(self, session_id: str, *, delete_object: bool = True) -> dict:
        suffix = "true" if delete_object else "false"
        return self._request(
            "DELETE",
            f"/v1/uploads/{session_id}?delete_object={suffix}",
        )


def build_bucket(session: dict):
    try:
        import oss2
    except ImportError as exc:  # pragma: no cover - production dependency
        raise UploadClientError(
            "install the 'aliyun' optional dependencies before uploading"
        ) from exc
    credentials = session.get("credentials") or {}
    auth = oss2.StsAuth(
        credentials["access_key_id"],
        credentials["access_key_secret"],
        credentials["security_token"],
    )
    return oss2.Bucket(auth, session["endpoint"], session["bucket"])


def part_plan(size_bytes: int, part_size: int) -> list[tuple[int, int, int]]:
    parts = []
    offset = 0
    number = 1
    while offset < size_bytes:
        length = min(part_size, size_bytes - offset)
        parts.append((number, offset, length))
        number += 1
        offset += length
    return parts


def upload_one_part(
    bucket,
    file_path: Path,
    object_key: str,
    upload_id: str,
    part_number: int,
    offset: int,
    length: int,
) -> tuple[int, str, int]:
    with file_path.open("rb") as source:
        source.seek(offset)
        content = source.read(length)
    if len(content) != length:
        raise UploadClientError(
            f"local file changed while reading part {part_number}: expected {length}, got {len(content)}"
        )
    result = bucket.upload_part(object_key, upload_id, part_number, content)
    etag = str(getattr(result, "etag", "") or "")
    if not etag:
        raise UploadClientError(f"OSS did not return an ETag for part {part_number}")
    return part_number, etag, length


def resumable_upload(
    file_path: Path,
    *,
    worker: UploadWorkerClient,
    checkpoint_dir: Path,
    project_id: str,
    part_size: int,
    threads: int,
    progress: bool,
) -> dict:
    if not file_path.is_file() or file_path.stat().st_size <= 0:
        raise UploadClientError(f"input file does not exist or is empty: {file_path}")
    fingerprint = LocalFingerprint.from_path(file_path)
    manifest = UploadManifest(checkpoint_path(checkpoint_dir, fingerprint))
    state = manifest.load()
    expected_fingerprint = {
        "path": fingerprint.path,
        "size_bytes": fingerprint.size_bytes,
        "mtime_ns": fingerprint.mtime_ns,
    }
    if state.get("fingerprint") != expected_fingerprint:
        state.clear()

    digest = str(state.get("sha256", "") or "")
    if not digest:
        digest = sha256_file(file_path)
    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"

    session_id = str(state.get("session_id", "") or "")
    if session_id:
        session = worker.refresh(session_id)
    else:
        session = worker.create_session(
            filename=file_path.name,
            size_bytes=fingerprint.size_bytes,
            content_type=content_type,
            sha256=digest,
            project_id=project_id,
        )
        session_id = session["session_id"]

    manifest.data = {
        **state,
        "fingerprint": expected_fingerprint,
        "sha256": digest,
        "session_id": session_id,
        "endpoint": session["endpoint"],
        "bucket": session["bucket"],
        "object_key": session["object_key"],
        "content_type": content_type,
        "parts": dict(state.get("parts", {})),
    }
    manifest.save()

    bucket = build_bucket(session)
    object_key = session["object_key"]
    upload_id = str(manifest.data.get("upload_id", "") or "")
    headers = {
        "Content-Type": content_type,
        "x-oss-meta-sha256": digest,
        "x-oss-meta-original-name": file_path.name,
        "x-oss-meta-cineflow-session": session_id,
    }
    if not upload_id:
        result = bucket.init_multipart_upload(object_key, headers=headers)
        upload_id = str(getattr(result, "upload_id", "") or "")
        if not upload_id:
            raise UploadClientError("OSS did not return a multipart upload ID")
        manifest.data["upload_id"] = upload_id
        manifest.data["parts"] = {}
        manifest.save()
        worker.register_multipart(session_id, upload_id)

    completed = {
        int(number): str(etag)
        for number, etag in dict(manifest.data.get("parts", {})).items()
    }
    plan = part_plan(fingerprint.size_bytes, part_size)
    pending = [item for item in plan if item[0] not in completed]
    uploaded_bytes = sum(length for number, _offset, length in plan if number in completed)
    progress_lock = threading.Lock()
    started = time.monotonic()

    def show_progress(increment: int) -> None:
        nonlocal uploaded_bytes
        if not progress:
            return
        with progress_lock:
            uploaded_bytes += increment
            elapsed = max(0.001, time.monotonic() - started)
            percent = uploaded_bytes * 100 / fingerprint.size_bytes
            speed_mib = uploaded_bytes / elapsed / 1024 / 1024
            print(
                f"\rupload {percent:6.2f}%  {speed_mib:7.2f} MiB/s  "
                f"{uploaded_bytes}/{fingerprint.size_bytes}",
                end="",
                flush=True,
            )

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
            futures = {
                pool.submit(
                    upload_one_part,
                    bucket,
                    file_path,
                    object_key,
                    upload_id,
                    number,
                    offset,
                    length,
                ): number
                for number, offset, length in pending
            }
            for future in concurrent.futures.as_completed(futures):
                number, etag, length = future.result()
                completed[number] = etag
                manifest.update_part(number, etag)
                show_progress(length)
    except Exception as exc:
        if progress:
            print(file=sys.stderr)
        raise UploadClientError(
            f"multipart upload stopped; checkpoint retained for resume: {exc}"
        ) from exc

    if progress:
        print()
    try:
        from oss2.models import PartInfo
    except ImportError as exc:  # pragma: no cover - production dependency
        raise UploadClientError("oss2 is missing PartInfo") from exc
    part_infos = [PartInfo(number, completed[number]) for number, _offset, _length in plan]
    result = bucket.complete_multipart_upload(
        object_key,
        upload_id,
        part_infos,
        headers=headers,
    )
    status_code = int(getattr(result, "status", 200) or 200)
    if status_code < 200 or status_code >= 300:
        raise UploadClientError(f"OSS multipart completion failed: HTTP {status_code}")

    completed_response = worker.complete(session_id, fingerprint.size_bytes, digest)
    manifest.data["completed"] = True
    manifest.data["result"] = completed_response
    manifest.save()
    return completed_response


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Upload a source video to OSS with STS, multipart concurrency, and resume"
    )
    parser.add_argument("file", type=Path)
    parser.add_argument("--worker", default="http://127.0.0.1:8094")
    parser.add_argument("--token", default=os.getenv("CINEFLOW_UPLOAD_CLIENT_TOKEN", ""))
    parser.add_argument("--project-id", default="default")
    parser.add_argument("--checkpoint-dir", type=Path, default=Path(".cineflow-upload"))
    parser.add_argument("--part-size-mib", type=int, default=8)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--abort", action="store_true")
    args = parser.parse_args()

    try:
        file_path = args.file.expanduser().resolve()
        fingerprint = LocalFingerprint.from_path(file_path)
        manifest = UploadManifest(checkpoint_path(args.checkpoint_dir, fingerprint))
        state = manifest.load()
        worker = UploadWorkerClient(args.worker, args.token)
        if args.abort:
            session_id = str(state.get("session_id", "") or "")
            if not session_id:
                raise UploadClientError("no resumable session exists for this file")
            result = worker.abort(session_id, delete_object=True)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        result = resumable_upload(
            file_path,
            worker=worker,
            checkpoint_dir=args.checkpoint_dir,
            project_id=args.project_id,
            part_size=max(1, args.part_size_mib) * 1024 * 1024,
            threads=max(1, args.threads),
            progress=not args.quiet,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"upload failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
