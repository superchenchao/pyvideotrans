from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

_SAFE_KEY = re.compile(r"^[A-Za-z0-9._-]{1,160}$")


class StateStoreError(RuntimeError):
    """Raised when durable state cannot be read or written safely."""


class FileStateStore:
    """Small durable JSON store for single-node workers.

    Records are written atomically and survive process restarts. Production
    multi-replica deployments can replace this class with Redis/PostgreSQL while
    preserving the same async interface.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    @staticmethod
    def _validate_component(value: str, label: str) -> str:
        text = str(value or "").strip()
        if not _SAFE_KEY.fullmatch(text):
            raise StateStoreError(f"invalid {label}: {value!r}")
        return text

    def _path(self, namespace: str, key: str) -> Path:
        safe_namespace = self._validate_component(namespace, "namespace")
        safe_key = self._validate_component(key, "record key")
        directory = self.root / safe_namespace
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{safe_key}.json"

    @staticmethod
    def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(payload, output, ensure_ascii=False, sort_keys=True, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateStoreError(f"failed to read state file {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise StateStoreError(f"state file {path} does not contain an object")
        return value

    async def put(self, namespace: str, key: str, payload: dict[str, Any]) -> None:
        path = self._path(namespace, key)
        async with self._lock:
            try:
                await asyncio.to_thread(self._write_atomic, path, payload)
            except OSError as exc:
                raise StateStoreError(f"failed to write state file {path}: {exc}") from exc

    async def get(self, namespace: str, key: str) -> dict[str, Any] | None:
        path = self._path(namespace, key)
        async with self._lock:
            return await asyncio.to_thread(self._read, path)

    async def delete(self, namespace: str, key: str) -> bool:
        path = self._path(namespace, key)
        async with self._lock:
            existed = path.exists()
            try:
                await asyncio.to_thread(path.unlink, missing_ok=True)
            except OSError as exc:
                raise StateStoreError(f"failed to delete state file {path}: {exc}") from exc
            return existed

    async def list(self, namespace: str) -> list[dict[str, Any]]:
        safe_namespace = self._validate_component(namespace, "namespace")
        directory = self.root / safe_namespace
        if not directory.is_dir():
            return []

        def load_all() -> list[dict[str, Any]]:
            records: list[dict[str, Any]] = []
            for path in sorted(directory.glob("*.json")):
                value = self._read(path)
                if value is not None:
                    records.append(value)
            return records

        async with self._lock:
            return await asyncio.to_thread(load_all)
