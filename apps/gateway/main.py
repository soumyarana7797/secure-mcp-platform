"""Secure MCP Gateway.

Run: `uv run uvicorn apps.gateway.main:create_app --factory --port 9000`
MCP endpoint: http://127.0.0.1:9000/mcp
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from mcp.server.transport_security import TransportSecuritySettings

from apps.gateway.config import Settings
from apps.gateway.mcp_app import build_gateway_server
from apps.gateway.middleware.auth import AuthMiddleware
from apps.gateway.middleware.request_id import RequestIdMiddleware
from apps.gateway.router.catalog import ConnectorFactory, ToolCatalog
from apps.gateway.router.connector import HttpBackendConnector
from apps.gateway.router.proxy import ToolProxy
from apps.gateway.routes import health
from shared.auth.resolver import DenyAllResolver, DevHeaderResolver, PrincipalResolver
from shared.db.database import Database
from shared.observability.logging import configure_logging
from shared.policies.engine import DbPolicyEngine, PolicyEngine
from shared.registry.repository import DbRegistryProvider, RegistryProvider

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    db: Database | None = None,
    registry: RegistryProvider | None = None,
    connector_factory: ConnectorFactory | None = None,
    resolver: PrincipalResolver | None = None,
    policy: PolicyEngine | None = None,
) -> FastAPI:
    """Builds the app. Every collaborator can be injected, which is how tests run without ports."""
    cfg = settings or Settings()
    configure_logging()

    owns_db = db is None and not (registry and policy and resolver)
    if owns_db:
        if not cfg.database_url:
            raise RuntimeError("GATEWAY_DATABASE_URL is required")
        db = Database(cfg.database_url)

    if resolver is None:
        if cfg.auth_mode == "dev_header":
            logger.warning("AUTH MODE dev_header: any caller can claim any identity (local only)")
            resolver = DevHeaderResolver(db)  # type: ignore[arg-type]
        else:
            resolver = DenyAllResolver()
    registry = registry or DbRegistryProvider(db)  # type: ignore[arg-type]
    policy = policy or DbPolicyEngine(db)  # type: ignore[arg-type]
    connector_factory = connector_factory or HttpBackendConnector

    catalog = ToolCatalog(registry, connector_factory, discovery_timeout_s=cfg.discovery_timeout_s)
    proxy = ToolProxy(catalog)
    mcp_server = build_gateway_server(catalog, proxy, policy)
    mcp_http_app = mcp_server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        host=cfg.host,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=cfg.allowed_hosts
        ),
    )

    async def refresh_forever() -> None:
        while True:
            await asyncio.sleep(cfg.catalog_refresh_s)
            try:
                await catalog.refresh()
            except Exception:
                logger.exception("catalog refresh crashed; will retry")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if owns_db and db is not None:
            await db.open()
        try:
            # The mounted MCP app has its own lifespan (its session manager); run it explicitly.
            async with mcp_http_app.router.lifespan_context(mcp_http_app):
                await catalog.refresh()
                task = asyncio.create_task(refresh_forever())
                try:
                    yield
                finally:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
        finally:
            if owns_db and db is not None:
                await db.close()

    app = FastAPI(title="Secure MCP Gateway", lifespan=lifespan)
    app.state.catalog = catalog
    app.state.db = db
    # Last added = outermost: request ID first, then authentication, then routes.
    app.add_middleware(AuthMiddleware, resolver=resolver)
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health.router)
    app.mount("/", mcp_http_app)  # last: catch-all that serves /mcp
    return app
