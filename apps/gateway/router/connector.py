"""How the gateway opens a connection to a backend MCP server.

Every call opens a short-lived `Client`. That matches stateless backends and keeps the gateway
free of per-session state. Tests substitute connectors that point at in-process servers.
"""

from typing import Protocol

from mcp import Client

from shared.registry.models import BackendSpec


class BackendConnector(Protocol):
    spec: BackendSpec

    def connect(self) -> Client:
        """Return a not-yet-entered client; use it with `async with`."""
        ...


class HttpBackendConnector:
    def __init__(self, spec: BackendSpec) -> None:
        self.spec = spec

    def connect(self) -> Client:
        return Client(self.spec.url)
