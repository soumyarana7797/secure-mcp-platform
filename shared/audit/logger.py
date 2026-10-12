"""Asynchronous, bounded audit writer.

`emit()` never blocks or raises: events go to a bounded queue drained by a background task that
inserts them in batches. If the database is unavailable or the queue is full, the event is still
written to the application log (`audit_fallback`) so it is not silently lost.
"""

import asyncio
import contextlib
import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol
from uuid import UUID

from shared.db.database import Database

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuditEvent:
    event_type: str  # tool_call | auth_failed
    decision: str  # allow | deny
    outcome: str
    request_id: str | None = None
    tenant_id: UUID | None = None
    user_id: UUID | None = None
    subject: str | None = None
    tool: str | None = None
    latency_ms: float | None = None
    args_hash: str | None = None
    redacted_args: dict[str, Any] | None = field(default=None)


class AuditSink(Protocol):
    def emit(self, event: AuditEvent) -> None: ...


class NullAudit:
    def emit(self, event: AuditEvent) -> None:
        return None


_INSERT = """
INSERT INTO audit_events (request_id, event_type, tenant_id, user_id, subject, tool, decision,
                          outcome, latency_ms, args_hash, redacted_args)
VALUES (%(request_id)s, %(event_type)s, %(tenant_id)s, %(user_id)s, %(subject)s, %(tool)s,
        %(decision)s, %(outcome)s, %(latency_ms)s, %(args_hash)s, %(redacted_args)s::jsonb)
"""


class AuditLogger:
    def __init__(
        self,
        db: Database,
        *,
        queue_size: int = 1000,
        batch_size: int = 50,
        flush_interval_s: float = 0.5,
    ) -> None:
        self._db = db
        self._queue: asyncio.Queue[AuditEvent] = asyncio.Queue(queue_size)
        self._batch_size = batch_size
        self._flush_interval = flush_interval_s
        self._task: asyncio.Task[None] | None = None
        self.dropped = 0
        self.failed = 0

    def emit(self, event: AuditEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1
            self._fallback(event, "queue_full")

    async def start(self) -> None:
        self._task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        """Drain what is queued, then stop."""
        await self.flush()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def flush(self) -> None:
        await self._queue.join()

    async def _worker(self) -> None:
        while True:
            batch = [await self._queue.get()]
            deadline = asyncio.get_running_loop().time() + self._flush_interval
            while len(batch) < self._batch_size:
                timeout = deadline - asyncio.get_running_loop().time()
                if timeout <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(self._queue.get(), timeout))
                except TimeoutError:
                    break
            try:
                await self._write(batch)
            except Exception:
                logger.exception("audit write failed; falling back to the application log")
                self.failed += len(batch)
                for event in batch:
                    self._fallback(event, "db_error")
            finally:
                for _ in batch:
                    self._queue.task_done()

    async def _write(self, batch: list[AuditEvent]) -> None:
        by_tenant: dict[UUID | None, list[AuditEvent]] = defaultdict(list)
        for event in batch:
            by_tenant[event.tenant_id].append(event)
        for tenant_id, events in by_tenant.items():
            rows = [self._row(e) for e in events]
            ctx = self._db.tenant_tx(tenant_id) if tenant_id else self._db.tx()
            async with ctx as conn:
                async with conn.cursor() as cur:
                    await cur.executemany(_INSERT, rows)

    @staticmethod
    def _row(event: AuditEvent) -> dict[str, Any]:
        row = asdict(event)
        row["redacted_args"] = (
            json.dumps(event.redacted_args) if event.redacted_args is not None else None
        )
        return row

    @staticmethod
    def _fallback(event: AuditEvent, why: str) -> None:
        payload = {k: (str(v) if isinstance(v, UUID) else v) for k, v in asdict(event).items()}
        logger.error("audit_fallback why=%s event=%s", why, json.dumps(payload, default=str))
