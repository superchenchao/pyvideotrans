from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..subtitle_models import ProviderSubmission, SubtitleRemovalRequest
from .aliyun_oss import AliyunOSSStore


class LocalSubtitleError(RuntimeError):
    """Normalized failure from a configured local subtitle-removal engine."""


@dataclass(frozen=True)
class LocalSubtitleConfig:
    command_template: str
    timeout_seconds: float = 1800.0
    download_timeout_seconds: float = 300.0
    max_download_bytes: int = 20 * 1024 * 1024 * 1024


class LocalSubtitleProvider:
    """Run a trusted server-side VSR command and upload the result to OSS.

    The command is configured by the deployer, never supplied by an API caller.
    Supported placeholders: {input}, {output}, {regions}, {time_ranges}, {workdir}.
    """

    name = "local"

    def __init__(
        self,
        config: LocalSubtitleConfig,
        store: AliyunOSSStore,
    ) -> None:
        self.config = config
        self.store = store

    @property
    def configured(self) -> bool:
        return bool(self.config.command_template.strip() and self.store.configured)

    @property
    def detail(self) -> str:
        return (
            "local subtitle command and OSS configured"
            if self.configured
            else "local command or OSS configuration missing"
        )

    async def _materialize(self, url: str, destination: Path) -> None:
        parsed = urlparse(url)
        if parsed.scheme == "file":
            await asyncio.to_thread(shutil.copy2, Path(parsed.path), destination)
            return
        if parsed.scheme not in {"http", "https"}:
            source = Path(url)
            if source.is_file():
                await asyncio.to_thread(shutil.copy2, source, destination)
                return
            raise LocalSubtitleError(f"unsupported local-removal input URL: {url}")

        total = 0
        try:
            async with httpx.AsyncClient(
                timeout=self.config.download_timeout_seconds,
                follow_redirects=True,
                trust_env=False,
            ) as client, client.stream("GET", url) as response:
                response.raise_for_status()
                with destination.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self.config.max_download_bytes:
                            raise LocalSubtitleError(
                                "local subtitle-removal input exceeds the configured limit"
                            )
                        output.write(chunk)
        except LocalSubtitleError:
            raise
        except Exception as exc:
            raise LocalSubtitleError(f"failed to download local-removal input: {exc}") from exc
        if total <= 0:
            raise LocalSubtitleError("downloaded local-removal input is empty")

    def _run_command(
        self,
        *,
        input_path: Path,
        output_path: Path,
        regions_path: Path,
        time_ranges_path: Path,
        workdir: Path,
    ) -> None:
        command = shlex.split(
            self.config.command_template.format(
                input=str(input_path),
                output=str(output_path),
                regions=str(regions_path),
                time_ranges=str(time_ranges_path),
                workdir=str(workdir),
            )
        )
        if not command:
            raise LocalSubtitleError("local subtitle command is empty")
        try:
            process = subprocess.run(
                command,
                cwd=workdir,
                env=os.environ.copy(),
                capture_output=True,
                text=True,
                timeout=self.config.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LocalSubtitleError(
                f"local subtitle command exceeded {self.config.timeout_seconds:.0f}s"
            ) from exc
        if process.returncode != 0:
            detail = (process.stderr or process.stdout or "").strip()[-4000:]
            raise LocalSubtitleError(
                f"local subtitle command failed with code {process.returncode}: {detail}"
            )
        if not output_path.is_file() or output_path.stat().st_size <= 0:
            raise LocalSubtitleError("local subtitle command did not create a usable output")

    async def submit(
        self,
        request: SubtitleRemovalRequest,
        *,
        job_id: str,
        output_object_key: str,
    ) -> ProviderSubmission:
        if not self.configured:
            raise LocalSubtitleError(self.detail)
        with tempfile.TemporaryDirectory(prefix=f"cineflow-detext-{job_id[:8]}-") as directory:
            workdir = Path(directory)
            input_path = workdir / "input.mp4"
            output_path = workdir / "output.mp4"
            regions_path = workdir / "regions.json"
            time_ranges_path = workdir / "time-ranges.json"
            regions_path.write_text(
                json.dumps(
                    [region.model_dump() for region in request.regions],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            time_ranges_path.write_text(
                json.dumps(
                    [item.model_dump() for item in request.time_ranges],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            await self._materialize(str(request.input_url), input_path)
            await asyncio.to_thread(
                self._run_command,
                input_path=input_path,
                output_path=output_path,
                regions_path=regions_path,
                time_ranges_path=time_ranges_path,
                workdir=workdir,
            )
            await asyncio.to_thread(
                self.store.put_file,
                output_object_key,
                output_path,
                content_type="video/mp4",
            )
        return ProviderSubmission(
            provider=self.name,
            external_job_id=f"local-{job_id}",
            output_object_key=output_object_key,
            output_url=self.store.canonical_url(output_object_key),
            completed=True,
            metadata={"command_backend": self.config.command_template.split()[0]},
        )

    async def wait(self, submission: ProviderSubmission) -> ProviderSubmission:
        if not submission.completed:
            raise LocalSubtitleError(
                "a local job interrupted before completion must be submitted again"
            )
        return submission

    async def cancel(self, submission: ProviderSubmission) -> None:
        del submission
