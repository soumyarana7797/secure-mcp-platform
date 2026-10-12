"""Rate-limit policies from PostgreSQL: a tenant-specific row overrides the platform default."""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from shared.db.database import Database
from shared.rate_limit.limiter import Limit

logger = logging.getLogger(__name__)

# Used only if the database has never been reachable and has no platform default.
FALLBACK = {"tenant": Limit(600, 100), "user": Limit(120, 30)}


class PolicyStore(Protocol):
    async def limit_for(self, scope: str, tenant_id: UUID) -> Limit: ...


class StaticPolicyStore:
    def __init__(self, defaults: dict[str, Limit] | None = None, overrides: dict | None = None):
        self.defaults = defaults or dict(FALLBACK)
        self.overrides = overrides or {}  # (scope, tenant_id) -> Limit

    async def limit_for(self, scope: str, tenant_id: UUID) -> Limit:
        return self.overrides.get((scope, tenant_id), self.defaults[scope])


class DbPolicyStore:
    """Loads the whole (small) table and refreshes it every `ttl_s`; keeps stale data on error."""

    def __init__(
        self, db: Database, *, ttl_s: float = 30.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._db = db
        self._ttl = ttl_s
        self._clock = clock
        self._loaded_at: float | None = None
        self._rows: dict[tuple[str, UUID | None], Limit] = {}
        self._lock = asyncio.Lock()

    async def limit_for(self, scope: str, tenant_id: UUID) -> Limit:
        await self._ensure_fresh()
        return (
            self._rows.get((scope, tenant_id)) or self._rows.get((scope, None)) or FALLBACK[scope]
        )

    async def _ensure_fresh(self) -> None:
        now = self._clock()
        if self._loaded_at is not None and now - self._loaded_at < self._ttl:
            return
        async with self._lock:
            if self._loaded_at is not None and self._clock() - self._loaded_at < self._ttl:
                return
            try:
                async with self._db.tx() as conn:
                    rows = await (
                        await conn.execute(
                            "SELECT tenant_id, scope, requests_per_minute, burst "
                            "FROM rate_limit_policies"
                        )
                    ).fetchall()
                self._rows = {
                    (r["scope"], r["tenant_id"]): Limit(r["requests_per_minute"], r["burst"])
                    for r in rows
                }
            except Exception:
                logger.exception("could not load rate-limit policies; keeping previous values")
            self._loaded_at = now  # back off on failure too
