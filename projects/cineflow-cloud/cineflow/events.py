from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator

from .models import JobEvent


class EventBroker:
    """Best-effort in-process event stream for desktop clients.

    It never blocks the media deadline. Production multi-replica deployments can
    replace it with Redis Streams without changing the orchestrator API.
    """

    def __init__(self, history_limit: int = 200) -> None:
        self.history_limit = history_limit
        self._history: dict[str, list[JobEvent]] = defaultdict(list)
        self._subscribers: dict[str, set[asyncio.Queue[JobEvent]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def publish(self, job_id: str, event: JobEvent) -> None:
        async with self._lock:
            history = self._history[job_id]
            history.append(event)
            if len(history) > self.history_limit:
                del history[: len(history) - self.history_limit]
            queues = tuple(self._subscribers[job_id])
        for queue in queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def history(self, job_id: str) -> list[JobEvent]:
        async with self._lock:
            return list(self._history.get(job_id, ()))

    async def subscribe(self, job_id: str) -> AsyncIterator[JobEvent]:
        queue: asyncio.Queue[JobEvent] = asyncio.Queue(maxsize=100)
        async with self._lock:
            initial = list(self._history.get(job_id, ()))
            self._subscribers[job_id].add(queue)
        try:
            for event in initial:
                yield event
                if event.event_type in {"succeeded", "degraded", "failed", "timed_out"}:
                    return
            while True:
                event = await queue.get()
                yield event
                if event.event_type in {"succeeded", "degraded", "failed", "timed_out"}:
                    return
        finally:
            async with self._lock:
                self._subscribers[job_id].discard(queue)
