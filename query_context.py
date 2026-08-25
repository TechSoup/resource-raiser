"""Explicit ownership for one asynchronous query.

The synchronous production path still uses thread-local compatibility state. New async code takes
one of these objects explicitly so simultaneous tasks cannot inherit or overwrite one another's
deadline, progress stream, accounting, or clients.
"""
from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TypeVar

import runtime


T = TypeVar("T")


@dataclass(slots=True)
class QueryBudget:
    """One event-loop-owned budget shared by every branch of a query."""
    max_attempts: int = 160
    max_fanout: int = 64
    attempts: int = 0
    fanout: int = 0

    def consume_attempt(self):
        if self.attempts >= self.max_attempts:
            raise runtime.QueryBudgetExceeded(
                f"shared query attempt budget exhausted after {self.attempts} attempts "
                f"(limit {self.max_attempts})")
        self.attempts += 1

    def consume_fanout(self, count=1):
        if self.fanout + count > self.max_fanout:
            raise runtime.QueryBudgetExceeded(
                f"query fan-out budget exhausted: planned {self.fanout + count} branches "
                f"but the limit is {self.max_fanout}")
        self.fanout += count


class ProviderPermits:
    """Application-owned semaphores acquired around one outbound call, never a whole query."""
    def __init__(self, limits=None):
        limits = limits or {}
        self._limits = {name: max(1, int(limit)) for name, limit in limits.items()}
        self._semaphores = {name: asyncio.Semaphore(limit)
                            for name, limit in self._limits.items()}
        self._active = {name: 0 for name in self._limits}
        self._waiting = {name: 0 for name in self._limits}

    async def call(self, name: str, factory: Callable[[], Awaitable[T]], context: "QueryContext") -> T:
        semaphore = self._semaphores.get(name)
        if semaphore is None:
            return await context.wait(factory())
        self._waiting[name] += 1
        try:
            await context.wait(semaphore.acquire())
        finally:
            self._waiting[name] -= 1
        self._active[name] += 1
        try:
            return await context.wait(factory())
        finally:
            self._active[name] -= 1
            semaphore.release()

    def snapshot(self):
        return {name: {"limit": self._limits[name], "active": self._active[name],
                       "waiting": self._waiting[name]}
                for name in self._limits}


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
    permits: ProviderPermits | None = None
    budget: QueryBudget = field(default_factory=QueryBudget)
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

    def fork(self):
        """Give a concurrent branch private scratch state and shared ownership state."""
        return QueryContext(deadline=self.deadline, trace_id=self.trace_id,
                            cancelled=self.cancelled, progress=self.progress,
                            usage_ledger=self.usage_ledger, discovery_ledger=self.discovery_ledger,
                            llm_client=self.llm_client, http_client=self.http_client,
                            sec_client=self.sec_client, bigquery_client=self.bigquery_client,
                            grant_pool=self.grant_pool, permits=self.permits, budget=self.budget)

    async def provider_call(self, name: str, factory: Callable[[], Awaitable[T]]) -> T:
        if self.permits is None:
            return await self.wait(factory())
        return await self.permits.call(name, factory, self)

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
        try:
            self.check()
        except BaseException:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            else:
                pending = asyncio.ensure_future(awaitable)
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            raise

        async def normalize_refusal():
            # SystemExit escaping a Task is re-raised into run_forever and terminates Uvicorn.
            # Convert at the task boundary while the sync refusal idiom is still being migrated.
            try:
                return await awaitable
            except SystemExit as exc:
                raise runtime.Refused(str(exc)) from exc

        work = asyncio.ensure_future(normalize_refusal())
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
