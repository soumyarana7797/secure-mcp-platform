"""Authorization policy: which permissions does a principal hold?

Permissions come from the database (`role_permissions`) for the principal's tenant and roles.
The tenant filter is applied twice on purpose: explicitly in SQL and by row-level security.
"""

from typing import Protocol

from shared.auth.principal import Principal
from shared.db.database import Database


class PolicyEngine(Protocol):
    async def permissions_for(self, principal: Principal) -> frozenset[str]: ...


class DbPolicyEngine:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def permissions_for(self, principal: Principal) -> frozenset[str]:
        if not principal.roles:
            return frozenset()
        async with self._db.tenant_tx(principal.tenant_id) as conn:
            rows = await (
                await conn.execute(
                    "SELECT DISTINCT permission FROM role_permissions "
                    "WHERE tenant_id = %s AND role = ANY(%s)",
                    (principal.tenant_id, sorted(principal.roles)),
                )
            ).fetchall()
        return frozenset(r["permission"] for r in rows)
