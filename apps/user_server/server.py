"""User MCP server (Streamable HTTP).

Run: `uv run uvicorn apps.user_server.server:create_app --factory --port 9003`

`get_user_profile` takes NO user or tenant argument. The caller is whoever the gateway's signed
internal token says it is, so there is no parameter through which to ask for someone else.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import BaseModel
from starlette.applications import Starlette

from apps.user_server.config import Settings
from shared.auth.internal import (
    InternalTokenVerifier,
    internal_identity_var,
    load_public_key_b64,
)
from shared.auth.internal_middleware import InternalAuthMiddleware
from shared.db.database import Database

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)

_PROFILE_SQL = """
SELECT u.id, u.display_name, u.email, u.roles, t.slug AS tenant_slug, t.name AS tenant_name
FROM users u JOIN tenants t ON t.id = u.tenant_id
WHERE u.id = %s AND u.tenant_id = %s AND u.status = 'active'
"""


class TenantInfo(BaseModel):
    slug: str
    name: str


class UserProfile(BaseModel):
    user_id: UUID
    display_name: str | None
    email: str | None
    roles: list[str]
    tenant: TenantInfo


def build_mcp(db: Database) -> MCPServer:
    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        yield  # the pool is owned by create_app so tests can inject their own

    mcp = MCPServer(
        name="user",
        instructions="Returns the profile of the authenticated caller. Takes no arguments.",
        lifespan=lifespan,
    )

    @mcp.tool(annotations=READ_ONLY)
    async def get_user_profile() -> UserProfile:
        """Return the profile of the user making this request (name, email, roles, tenant)."""
        identity = internal_identity_var.get()
        if (
            identity is None
            or identity.purpose != "call"
            or not (identity.user_id and identity.tenant_id)
        ):
            raise ToolError("no authenticated user for this request")
        # Both ids come from the signed token, never from the caller. The tenant is also set for
        # row-level security, so even a wrong WHERE clause could not reach another tenant's rows.
        async with db.tenant_tx(identity.tenant_id) as conn:
            row = await (
                await conn.execute(_PROFILE_SQL, (identity.user_id, identity.tenant_id))
            ).fetchone()
        if row is None:
            raise ToolError("profile not found")
        return UserProfile(
            user_id=row["id"],
            display_name=row["display_name"],
            email=row["email"],
            roles=list(row["roles"]),
            tenant=TenantInfo(slug=row["tenant_slug"], name=row["tenant_name"]),
        )

    return mcp


def create_app(settings: Settings | None = None, db: Database | None = None) -> Starlette:
    cfg = settings or Settings()
    if not cfg.internal_public_key_b64:
        # Fail closed: a backend that accepts anonymous calls would bypass every gateway check.
        raise RuntimeError("USER_INTERNAL_PUBLIC_KEY_B64 is required")
    owns_db = db is None
    if db is None:
        if not cfg.server_database_url:
            raise RuntimeError("USER_SERVER_DATABASE_URL is required")
        db = Database(cfg.server_database_url)
    mcp = build_mcp(db)
    app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        host=cfg.host,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=cfg.allowed_hosts
        ),
    )
    inner_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a: Starlette) -> AsyncIterator[None]:
        if owns_db:
            await db.open()
        try:
            async with inner_lifespan(a):
                yield
        finally:
            if owns_db:
                await db.close()

    app.router.lifespan_context = lifespan
    verifier = InternalTokenVerifier(
        load_public_key_b64(cfg.internal_public_key_b64), cfg.internal_audience
    )
    app.add_middleware(InternalAuthMiddleware, verifier=verifier)
    return app
