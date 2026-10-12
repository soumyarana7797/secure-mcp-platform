"""How the gateway opens a connection to a backend MCP server, and how it authenticates to it.

Every call opens a short-lived `Client`. That matches stateless backends and keeps the gateway
free of per-session state. Tests substitute connectors that point at in-process servers.
"""

from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from shared.auth.internal import InternalTokenSigner
from shared.auth.principal import Principal
from shared.observability.context import request_id_var
from shared.registry.models import BackendSpec


class BackendConnector(Protocol):
    spec: BackendSpec

    def connect(
        self, headers: Mapping[str, str] | None = None
    ) -> AbstractAsyncContextManager[Client]:
        """Open a client to the backend; use with `async with`."""
        ...


class HttpBackendConnector:
    def __init__(self, spec: BackendSpec) -> None:
        self.spec = spec

    @asynccontextmanager
    async def connect(self, headers: Mapping[str, str] | None = None) -> AsyncIterator[Client]:
        async with httpx2.AsyncClient(headers=dict(headers or {})) as http:
            transport = streamable_http_client(self.spec.url, http_client=http)
            async with Client(transport) as client:
                yield client


class BackendAuth(Protocol):
    def headers(
        self, spec: BackendSpec, principal: Principal | None, tool: str | None = None
    ) -> dict[str, str]: ...


class NoBackendAuth:
    """For tests and in-process backends."""

    def headers(
        self, spec: BackendSpec, principal: Principal | None, tool: str | None = None
    ) -> dict[str, str]:
        return {}


class InternalBackendAuth:
    """Mints a short-lived, audience-bound token per backend call."""

    def __init__(self, signer: InternalTokenSigner) -> None:
        self._signer = signer

    def headers(
        self, spec: BackendSpec, principal: Principal | None, tool: str | None = None
    ) -> dict[str, str]:
        token = self._signer.mint(
            audience=spec.audience, principal=principal, tool=tool, request_id=request_id_var.get()
        )
        return {"Authorization": f"Bearer {token}"}
