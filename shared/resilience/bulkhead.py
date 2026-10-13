"""Bulkhead: bound concurrent calls to one dependency so a slow one cannot exhaust the gateway."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class BulkheadFull(Exception):
    """No slot became free within the queue timeout."""


class Bulkhead:
    def __init__(self, max_concurrent: int, queue_timeout_s: float = 0.25) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self.max_concurrent = max_concurrent
        self._queue_timeout = queue_timeout_s
        self._sem = asyncio.Semaphore(max_concurrent)
        self.rejected = 0

    @property
    def in_flight(self) -> int:
        return self.max_concurrent - self._sem._value  # noqa: SLF001

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        if self._sem.locked():  # full: wait briefly for a slot, then shed load
            try:
                await asyncio.wait_for(self._sem.acquire(), self._queue_timeout)
            except TimeoutError:
                self.rejected += 1
                raise BulkheadFull from None
        else:
            await self._sem.acquire()  # free slot: returns without suspending
        try:
            yield
        finally:
            self._sem.release()
