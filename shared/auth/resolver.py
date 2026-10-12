"""Turns an incoming request into a Principal.

Identity (who you are) comes from the token; tenant and roles come from OUR database, so a token
can never grant itself more than the database allows.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

from shared.auth.jwt_validator import TokenError, TokenValidator
from shared.auth.principal import Principal
from shared.db.database import Database

logger = logging.getLogger(__name__)

PrincipalLookup = Callable[[str], Awaitable[Principal | None]]


class AuthFailure(Exception):
    """Credentials were presented but rejected. `reason` is internal; the rest is public."""

    def __init__(self, status: int, error: str, description: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.error = error
        self.description = description
        self.reason = reason


class PrincipalResolver(Protocol):
    async def resolve(self, headers: Mapping[str, str]) -> Principal | None:
        """Return the caller; None if no credentials were presented; raise AuthFailure if they
        were presented but rejected. Other exceptions mean infrastructure failure."""
        ...


class DbPrincipalLookup:
    """subject -> Principal via the SECURITY DEFINER function (works before the tenant is known)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def __call__(self, sub: str) -> Principal | None:
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


class DenyAllResolver:
    """Fail-closed default: nobody is authenticated."""

    async def resolve(self, headers: Mapping[str, str]) -> Principal | None:
        return None


class DevHeaderResolver:
    """DEV ONLY. Trusts an `X-Dev-Sub` header naming a seeded user. Anyone can claim any
    identity, so this must never run outside a local environment (enforced in Settings)."""

    HEADER = "x-dev-sub"

    def __init__(self, lookup: PrincipalLookup) -> None:
        self._lookup = lookup

    async def resolve(self, headers: Mapping[str, str]) -> Principal | None:
        sub = headers.get(self.HEADER)
        return await self._lookup(sub) if sub else None


class BearerTokenResolver:
    def __init__(self, validator: TokenValidator, lookup: PrincipalLookup) -> None:
        self._validator = validator
        self._lookup = lookup

    async def resolve(self, headers: Mapping[str, str]) -> Principal | None:
        header = headers.get("authorization")
        if not header:
            return None
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise AuthFailure(401, "invalid_token", "The access token is invalid.", "bad_scheme")
        try:
            verified = await self._validator.validate(token.strip())
        except TokenError as exc:
            description = (
                "The access token does not grant the required scope."
                if exc.insufficient_scope
                else "The access token is invalid."
            )
            raise AuthFailure(exc.status, exc.error_code, description, exc.reason) from exc
        principal = await self._lookup(verified.subject)
        if principal is None:
            # Valid token, but we do not know this person (or they are disabled).
            raise AuthFailure(
                403, "access_denied", "This account is not provisioned.", "unprovisioned"
            )
        return principal
