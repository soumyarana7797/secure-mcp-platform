"""PostgreSQL access for the runtime role (`gateway_app`).

Every tenant-scoped query runs inside `tenant_tx`, which sets `app.tenant_id` for the
transaction so Row-Level Security applies. Queries are always parameterized.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

STATEMENT_TIMEOUT_MS = 5000


class Database:
    def __init__(self, dsn: str, max_size: int = 10) -> None:
        self._pool = AsyncConnectionPool(
            dsn,
            min_size=1,
            max_size=max_size,
            open=False,
            kwargs={
                "row_factory": dict_row,
                "options": f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
            },
        )

    async def open(self) -> None:
        await self._pool.open(wait=True, timeout=10)

    async def close(self) -> None:
        await self._pool.close()

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[AsyncConnection]:
        """A transaction with no tenant set: only global tables and SECURITY DEFINER functions."""
        async with self._pool.connection() as conn, conn.transaction():
            yield conn

    @asynccontextmanager
    async def tenant_tx(self, tenant_id: UUID) -> AsyncIterator[AsyncConnection]:
        """A transaction scoped to one tenant. `set_config(..., true)` is transaction-local."""
        async with self.tx() as conn:
            await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
            yield conn

    async def ping(self) -> bool:
        try:
            async with self.tx() as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception:
            return False
