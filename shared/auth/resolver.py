"""Turns an incoming request into a Principal. Phase 4 adds a Cognito JWT resolver."""

import logging
from collections.abc import Mapping
from typing import Protocol

from shared.auth.principal import Principal
from shared.db.database import Database

logger = logging.getLogger(__name__)


class PrincipalResolver(Protocol):
    async def resolve(self, headers: Mapping[str, str]) -> Principal | None:
        """Return the caller, or None if unauthenticated. Raise only for infrastructure failure."""
        ...


class DenyAllResolver:
    """Fail-closed default: nobody is authenticated."""

    async def resolve(self, headers: Mapping[str, str]) -> Principal | None:
        return None


class DevHeaderResolver:
    """DEV ONLY. Trusts an `X-Dev-Sub` header naming a seeded user. Anyone can claim any
    identity, so this must never run outside a local environment (enforced in Settings)."""

    HEADER = "x-dev-sub"

    def __init__(self, db: Database) -> None:
        self._db = db

    async def resolve(self, headers: Mapping[str, str]) -> Principal | None:
        sub = headers.get(self.HEADER)
        if not sub:
            return None
        async with self._db.tx() as conn:
            row = await (
                await conn.execute(
                    "SELECT user_id, tenant_id, tenant_slug, roles FROM resolve_principal(%s)",
                    (sub,),
                )
            ).fetchone()
        if row is None:
            return None
        return Principal(
            subject=sub,
            user_id=row["user_id"],
            tenant_id=row["tenant_id"],
            tenant_slug=row["tenant_slug"],
            roles=frozenset(row["roles"]),
        )
