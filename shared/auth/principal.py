"""The authenticated caller, as the rest of the system sees it."""

from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class Principal:
    subject: str  # identity-provider subject (Cognito `sub`)
    user_id: UUID
    tenant_id: UUID
    tenant_slug: str
    roles: frozenset[str]


# Set by the auth middleware for the duration of one HTTP request.
principal_var: ContextVar[Principal | None] = ContextVar("principal", default=None)
