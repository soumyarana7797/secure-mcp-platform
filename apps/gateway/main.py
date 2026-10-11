"""Secure MCP Gateway.

Run: `uv run uvicorn apps.gateway.main:create_app --factory --port 9000`
MCP endpoint: http://127.0.0.1:9000/mcp
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from fastapi import FastAPI
from mcp.server.transport_security import TransportSecuritySettings

from apps.gateway.config import Settings
from apps.gateway.mcp_app import build_gateway_server
from apps.gateway.middleware.request_id import RequestIdMiddleware
from apps.gateway.router.catalog import ToolCatalog
from apps.gateway.router.connector import BackendConnector, HttpBackendConnector
from apps.gateway.router.proxy import ToolProxy
from apps.gateway.routes import health
from shared.observability.logging import configure_logging
from shared.registry.models import Registry

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    connectors: Mapping[str, BackendConnector] | None = None,
) -> FastAPI:
    cfg = settings or Settings()
    configure_logging()
    if connectors is None:
        registry = Registry.from_toml(cfg.registry_path)
        connectors = {b.name: HttpBackendConnector(b) for b in registry.backends}
    connectors = dict(connectors)

    catalog = ToolCatalog(connectors, discovery_timeout_s=cfg.discovery_timeout_s)
    proxy = ToolProxy(catalog, connectors)
    mcp_server = build_gateway_server(catalog, proxy)
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

    app = FastAPI(title="Secure MCP Gateway", lifespan=lifespan)
    app.state.catalog = catalog
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health.router)
    app.mount("/", mcp_http_app)  # last: catch-all that serves /mcp
    return app
