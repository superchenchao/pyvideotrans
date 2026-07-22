from __future__ import annotations

from typing import Protocol

from ..subtitle_models import ProviderSubmission, SubtitleRemovalRequest


class SubtitleRemovalProvider(Protocol):
    name: str

    @property
    def configured(self) -> bool: ...

    @property
    def detail(self) -> str: ...

    async def submit(
        self,
        request: SubtitleRemovalRequest,
        *,
        job_id: str,
        output_object_key: str,
    ) -> ProviderSubmission: ...

    async def wait(self, submission: ProviderSubmission) -> ProviderSubmission: ...

    async def cancel(self, submission: ProviderSubmission) -> None: ...
