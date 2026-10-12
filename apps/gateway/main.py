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
from apps.gateway.router.connector import (
    BackendAuth,
    HttpBackendConnector,
    InternalBackendAuth,
    NoBackendAuth,
)
from apps.gateway.router.proxy import ToolProxy
from apps.gateway.routes import health, oauth
from shared.audit.logger import AuditEvent, AuditLogger, AuditSink, NullAudit
from shared.auth.internal import InternalTokenSigner, load_private_key_b64
from shared.auth.jwt_validator import HttpJwksSource, JwksCache, TokenValidator
from shared.auth.resolver import (
    BearerTokenResolver,
    DbPrincipalLookup,
    DenyAllResolver,
    DevHeaderResolver,
    PrincipalResolver,
)
from shared.db.database import Database
from shared.observability.context import request_id_var
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
    backend_auth: BackendAuth | None = None,
    audit: AuditSink | None = None,
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
        lookup = DbPrincipalLookup(db)  # type: ignore[arg-type]
        if cfg.auth_mode == "dev_header":
            logger.warning("AUTH MODE dev_header: any caller can claim any identity (local only)")
            resolver = DevHeaderResolver(lookup)
        elif cfg.auth_mode == "cognito":
            jwks = JwksCache(HttpJwksSource(f"{cfg.cognito_issuer}/.well-known/jwks.json"))
            validator = TokenValidator(
                cfg.cognito_issuer, frozenset(cfg.cognito_client_ids), cfg.required_scope, jwks
            )
            resolver = BearerTokenResolver(validator, lookup)
        else:
            resolver = DenyAllResolver()
    registry = registry or DbRegistryProvider(db)  # type: ignore[arg-type]
    policy = policy or DbPolicyEngine(db)  # type: ignore[arg-type]
    if backend_auth is None:
        if connector_factory is None:  # real HTTP backends: they will demand our token
            if not cfg.internal_private_key_b64:
                raise RuntimeError("GATEWAY_INTERNAL_PRIVATE_KEY_B64 is required")
            signer = InternalTokenSigner(
                load_private_key_b64(cfg.internal_private_key_b64), ttl_s=cfg.internal_token_ttl_s
            )
            backend_auth = InternalBackendAuth(signer)
        else:
            backend_auth = NoBackendAuth()
    connector_factory = connector_factory or HttpBackendConnector

    catalog = ToolCatalog(
        registry,
        connector_factory,
        discovery_timeout_s=cfg.discovery_timeout_s,
        backend_auth=backend_auth,
    )
    if audit is None:
        audit = AuditLogger(db) if db is not None else NullAudit()
    proxy = ToolProxy(catalog, backend_auth, audit)
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
        if isinstance(audit, AuditLogger):
            await audit.start()
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
            if isinstance(audit, AuditLogger):
                await audit.stop()  # drain queued events before the pool closes
            if owns_db and db is not None:
                await db.close()

    app = FastAPI(title="Secure MCP Gateway", lifespan=lifespan)
    app.state.settings = cfg
    app.state.catalog = catalog
    app.state.db = db

    # Last added = outermost: request ID first, then authentication, then routes.
    async def record_auth_failure(scope: dict, status: int, reason: str) -> None:
        audit.emit(  # type: ignore[union-attr]
            AuditEvent(
                event_type="auth_failed",
                decision="deny",
                outcome=reason,
                request_id=request_id_var.get(),
                tool=None,
                args_hash=None,
            )
        )

    metadata_url = f"{cfg.public_base_url.rstrip('/')}/.well-known/oauth-protected-resource"
    app.add_middleware(
        AuthMiddleware,
        resolver=resolver,
        resource_metadata_url=metadata_url,
        on_reject=record_auth_failure,
    )
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health.router)
    app.include_router(oauth.router)
    app.mount("/", mcp_http_app)  # last: catch-all that serves /mcp
    return app
