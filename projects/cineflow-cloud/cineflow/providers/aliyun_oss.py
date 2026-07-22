from __future__ import annotations

import mimetypes
import posixpath
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlparse, urlunparse


class AliyunOSSError(RuntimeError):
    """Normalized Alibaba OSS failure."""


@dataclass(frozen=True)
class AliyunOSSConfig:
    endpoint: str
    bucket: str
    prefix: str
    access_key_id: str
    access_key_secret: str
    security_token: str = ""
    signed_url_ttl_seconds: int = 86400


class AliyunOSSStore:
    """Private OSS storage for source uploads and CineFlow-generated artifacts.

    The same implementation supports server-side validation/cleanup for direct
    client uploads and storage of generated TTS clips, subtitles, and final media.
    Source files and generated files should use separate configured prefixes.
    """

    def __init__(self, config: AliyunOSSConfig, *, bucket_client=None) -> None:
        self.config = config
        self._bucket_client = bucket_client

    @property
    def configured(self) -> bool:
        return bool(
            self.config.endpoint.strip()
            and self.config.bucket.strip()
            and self.config.access_key_id.strip()
            and self.config.access_key_secret.strip()
        )

    @property
    def endpoint_host(self) -> str:
        parsed = urlparse(self._normalized_endpoint())
        return parsed.netloc

    def _normalized_endpoint(self) -> str:
        value = self.config.endpoint.strip().rstrip("/")
        if not value:
            return ""
        return value if "://" in value else f"https://{value}"

    def _bucket(self):
        if self._bucket_client is not None:
            return self._bucket_client
        if not self.configured:
            raise AliyunOSSError("Alibaba OSS configuration is incomplete")
        try:
            import oss2
        except ImportError as exc:  # pragma: no cover - production dependency
            raise AliyunOSSError(
                "install the 'aliyun' optional dependencies to access OSS"
            ) from exc

        if self.config.security_token.strip():
            auth = oss2.StsAuth(
                self.config.access_key_id.strip(),
                self.config.access_key_secret.strip(),
                self.config.security_token.strip(),
            )
        else:
            auth = oss2.Auth(
                self.config.access_key_id.strip(),
                self.config.access_key_secret.strip(),
            )
        self._bucket_client = oss2.Bucket(
            auth,
            self._normalized_endpoint(),
            self.config.bucket.strip(),
        )
        return self._bucket_client

    def key(self, *parts: str) -> str:
        cleaned = [
            str(PurePosixPath(part.strip().lstrip("/")))
            for part in parts
            if str(part or "").strip()
        ]
        prefix = str(PurePosixPath(self.config.prefix.strip().strip("/")))
        if prefix and prefix != ".":
            cleaned.insert(0, prefix)
        return posixpath.normpath("/".join(cleaned)).lstrip("/")

    def canonical_url(self, object_key: str) -> str:
        if not self.config.bucket.strip() or not self.endpoint_host:
            raise AliyunOSSError("Alibaba OSS endpoint or bucket is missing")
        encoded = quote(object_key.lstrip("/"), safe="/~-._")
        return f"https://{self.config.bucket.strip()}.{self.endpoint_host}/{encoded}"

    def oss_uri(self, object_key: str) -> str:
        return f"oss://{self.config.bucket.strip()}/{object_key.lstrip('/')}"

    def put_bytes(
        self,
        object_key: str,
        content: bytes,
        *,
        content_type: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        request_headers = dict(headers or {})
        guessed = content_type or mimetypes.guess_type(object_key)[0] or ""
        if guessed:
            request_headers.setdefault("Content-Type", guessed)
        try:
            result = self._bucket().put_object(
                object_key,
                content,
                headers=request_headers,
            )
        except Exception as exc:
            raise AliyunOSSError(f"failed to upload OSS object {object_key}: {exc}") from exc
        self._require_success(result, f"upload OSS object {object_key}")

    def put_file(
        self,
        object_key: str,
        filename: str | Path,
        *,
        content_type: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        request_headers = dict(headers or {})
        guessed = content_type or mimetypes.guess_type(str(filename))[0] or ""
        if guessed:
            request_headers.setdefault("Content-Type", guessed)
        try:
            result = self._bucket().put_object_from_file(
                object_key,
                str(filename),
                headers=request_headers,
            )
        except Exception as exc:
            raise AliyunOSSError(f"failed to upload OSS file {object_key}: {exc}") from exc
        self._require_success(result, f"upload OSS file {object_key}")

    @staticmethod
    def _require_success(result: object, action: str) -> None:
        status = int(getattr(result, "status", 200) or 200)
        if status < 200 or status >= 300:
            raise AliyunOSSError(f"failed to {action}: HTTP {status}")

    def head_object(self, object_key: str) -> dict[str, Any]:
        try:
            result = self._bucket().head_object(object_key)
        except Exception as exc:
            raise AliyunOSSError(f"failed to inspect OSS object {object_key}: {exc}") from exc
        headers = {
            str(key).lower(): str(value)
            for key, value in dict(getattr(result, "headers", {}) or {}).items()
        }
        content_length = getattr(result, "content_length", None)
        if content_length is None:
            content_length = headers.get("content-length", 0)
        return {
            "object_key": object_key,
            "content_length": int(content_length or 0),
            "content_type": str(
                getattr(result, "content_type", "")
                or headers.get("content-type", "")
            ),
            "etag": str(getattr(result, "etag", "") or headers.get("etag", "")),
            "last_modified": str(
                getattr(result, "last_modified", "")
                or headers.get("last-modified", "")
            ),
            "headers": headers,
            "metadata": {
                key.removeprefix("x-oss-meta-"): value
                for key, value in headers.items()
                if key.startswith("x-oss-meta-")
            },
        }

    def object_exists(self, object_key: str) -> bool:
        try:
            return bool(self._bucket().object_exists(object_key))
        except Exception as exc:
            raise AliyunOSSError(
                f"failed to check OSS object {object_key}: {exc}"
            ) from exc

    def delete_object(self, object_key: str) -> None:
        try:
            result = self._bucket().delete_object(object_key)
        except Exception as exc:
            raise AliyunOSSError(f"failed to delete OSS object {object_key}: {exc}") from exc
        self._require_success(result, f"delete OSS object {object_key}")

    def abort_multipart_upload(self, object_key: str, upload_id: str) -> None:
        try:
            result = self._bucket().abort_multipart_upload(object_key, upload_id)
        except Exception as exc:
            raise AliyunOSSError(
                f"failed to abort multipart upload {upload_id} for {object_key}: {exc}"
            ) from exc
        self._require_success(result, f"abort multipart upload {upload_id}")

    def signed_url(self, object_key: str, *, method: str = "GET") -> str:
        try:
            return str(
                self._bucket().sign_url(
                    method,
                    object_key,
                    int(self.config.signed_url_ttl_seconds),
                    slash_safe=True,
                )
            )
        except TypeError:
            try:
                return str(
                    self._bucket().sign_url(
                        method,
                        object_key,
                        int(self.config.signed_url_ttl_seconds),
                    )
                )
            except Exception as exc:
                raise AliyunOSSError(f"failed to sign OSS object {object_key}: {exc}") from exc
        except Exception as exc:
            raise AliyunOSSError(f"failed to sign OSS object {object_key}: {exc}") from exc

    def object_key_from_url(self, value: str) -> str | None:
        parsed = urlparse(str(value or ""))
        if parsed.scheme == "oss" and parsed.netloc == self.config.bucket.strip():
            return unquote(parsed.path.lstrip("/"))
        if parsed.scheme not in {"http", "https"}:
            return None
        expected_host = f"{self.config.bucket.strip()}.{self.endpoint_host}".lower()
        if parsed.netloc.split(":", 1)[0].lower() != expected_host:
            return None
        return unquote(parsed.path.lstrip("/"))

    def sign_if_owned(self, value: str) -> str:
        object_key = self.object_key_from_url(value)
        return self.signed_url(object_key) if object_key else value

    @staticmethod
    def canonicalize_oss_input(value: str) -> str:
        """Strip an expiring query string from an Alibaba OSS URL."""

        parsed = urlparse(str(value or ""))
        host = parsed.netloc.split(":", 1)[0].lower()
        is_aliyun_oss = host.endswith(".aliyuncs.com") and ".oss-" in host
        if parsed.scheme in {"http", "https"} and is_aliyun_oss:
            return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
        return str(value)
