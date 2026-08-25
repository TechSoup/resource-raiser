"""Explicit ownership for one asynchronous query.

The synchronous production path still uses thread-local compatibility state. New async code takes
one of these objects explicitly so simultaneous tasks cannot inherit or overwrite one another's
deadline, progress stream, accounting, or clients.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, TypeVar

import runtime


T = TypeVar("T")


@dataclass(slots=True)
class QueryContext:
    deadline: float | None = None
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    progress: asyncio.Queue = field(default_factory=asyncio.Queue)
    usage_ledger: Any = None
    discovery_ledger: Any = None
    llm_client: Any = None
    http_client: Any = None
    sec_client: Any = None
    bigquery_client: Any = None
    grant_pool: Any = None
    memo: dict = field(default_factory=dict)

    @classmethod
    def with_timeout(cls, seconds: float, **kwargs):
        return cls(deadline=time.monotonic() + seconds, **kwargs)

    def remaining(self) -> float | None:
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - time.monotonic())

    def check(self):
        if self.cancelled.is_set():
            raise runtime.QueryCancelled("query cancelled")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.cancelled.set()
            raise runtime.QueryCancelled("query deadline exceeded")

    def cancel(self):
        self.cancelled.set()

    async def emit(self, kind: str, **data):
        self.check()
        await self.progress.put({"kind": kind, **data})

    async def sleep(self, seconds: float):
        """Cancellable backoff that also observes the query deadline."""
        if seconds <= 0:
            self.check()
            await asyncio.sleep(0)
            return
        sleeper = asyncio.create_task(asyncio.sleep(seconds))
        try:
            await self.wait(sleeper)
        finally:
            if not sleeper.done():
                sleeper.cancel()

    async def wait(self, awaitable: Awaitable[T]) -> T:
        """Await provider work while racing explicit cancellation and the deadline."""
        work = asyncio.ensure_future(awaitable)
        try:
            self.check()
        except BaseException:
            # Callers construct coroutine objects eagerly. If the query was already cancelled,
            # close that coroutine through a Task before propagating or Python reports an
            # unawaited-coroutine leak precisely during disconnect cleanup.
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            raise
        cancellation = asyncio.create_task(self.cancelled.wait())
        try:
            done, _ = await asyncio.wait(
                {work, cancellation}, timeout=self.remaining(),
                return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                cancellation.cancel()
                return await work
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            if cancellation in done:
                raise runtime.QueryCancelled("query cancelled")
            self.cancelled.set()
            raise runtime.QueryCancelled("query deadline exceeded")
        except asyncio.CancelledError:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            raise
        finally:
            cancellation.cancel()
            await asyncio.gather(cancellation, return_exceptions=True)
