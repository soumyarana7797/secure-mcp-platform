"""Tool catalog: which tools exist, who owns them, and what clients are allowed to see.

Clients are shown the admin-PINNED definition, never whatever the backend currently reports.
A live tool is exposed only if it is pinned AND its live definition still matches the pin.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from mcp_types import Tool, ToolAnnotations

from apps.gateway.router.connector import BackendAuth, BackendConnector, NoBackendAuth
from shared.registry.models import BackendSpec
from shared.registry.pinning import PinnedTool, definition_hash
from shared.registry.repository import LoadedRegistry, RegistryProvider

logger = logging.getLogger(__name__)

MAX_PAGES = 20  # defensive bound on tools/list pagination
ConnectorFactory = Callable[[BackendSpec], BackendConnector]


@dataclass(frozen=True)
class CatalogTool:
    public_name: str  # e.g. "football.get_fixtures"
    backend: str
    original_name: str  # e.g. "get_fixtures"
    tool: Tool  # exactly what clients see, rebuilt from the pin
    permission: str
    timeout_s: float
    connector: BackendConnector
    spec: BackendSpec


@dataclass
class BackendStatus:
    healthy: bool = False
    exposed: int = 0
    error: str | None = None
    drifted: list[str] = field(default_factory=list)  # live definition != pin: hidden
    unregistered: list[str] = field(default_factory=list)  # live but never pinned: hidden
    missing: list[str] = field(default_factory=list)  # pinned but not served: hidden


def _public_tool(public_name: str, pin: PinnedTool) -> Tool:
    """Rebuild a Tool from the pin only. Title, icons, meta and output schema are NOT forwarded:
    they are free text a hostile backend could use to smuggle instructions to the model."""
    annotations = ToolAnnotations(**pin.annotations) if pin.annotations else None
    return Tool(
        name=public_name,
        description=pin.description,
        input_schema=pin.input_schema,
        annotations=annotations,
    )


@dataclass
class ToolCatalog:
    registry: RegistryProvider
    connector_factory: ConnectorFactory
    discovery_timeout_s: float = 5.0
    backend_auth: BackendAuth = field(default_factory=NoBackendAuth)
    _by_backend: dict[str, list[CatalogTool]] = field(default_factory=dict)
    _tools: dict[str, CatalogTool] = field(default_factory=dict)
    status: dict[str, BackendStatus] = field(default_factory=dict)
    registry_loaded: bool = False

    def get(self, public_name: str) -> CatalogTool | None:
        return self._tools.get(public_name)

    def tools_for(self, permissions: frozenset[str]) -> list[Tool]:
        """Only the tools whose required permission the caller holds."""
        return [e.tool for e in self._tools.values() if e.permission in permissions]

    @property
    def ready(self) -> bool:
        """Ready once the registry loaded and at least one backend serves an exposed tool."""
        return self.registry_loaded and any(s.healthy for s in self.status.values())

    async def refresh(self) -> None:
        try:
            loaded = await self.registry.load()
        except Exception:
            logger.exception("registry load failed; keeping previous catalog")
            return
        self.registry_loaded = True
        await self._refresh_backends(loaded)

    async def _refresh_backends(self, loaded: LoadedRegistry) -> None:
        specs = loaded.backends
        results = await asyncio.gather(*(self._discover(s, loaded.pinned[s.name]) for s in specs))
        current = {s.name for s in specs}
        for spec, outcome in zip(specs, results, strict=True):
            if isinstance(outcome, str):
                # Keep last-known tools so a flapping backend does not vanish from listings.
                kept = len(self._by_backend.get(spec.name, []))
                self.status[spec.name] = BackendStatus(False, kept, outcome)
                logger.warning("catalog refresh failed backend=%s error=%s", spec.name, outcome)
            else:
                entries, status = outcome
                self._by_backend[spec.name] = entries
                self.status[spec.name] = status
        for gone in set(self._by_backend) - current:  # backend removed/disabled in the registry
            del self._by_backend[gone]
            self.status.pop(gone, None)
        self._tools = {t.public_name: t for ts in self._by_backend.values() for t in ts}

    async def _discover(
        self, spec: BackendSpec, pinned: dict[str, PinnedTool]
    ) -> tuple[list[CatalogTool], BackendStatus] | str:
        connector = self.connector_factory(spec)
        try:
            async with asyncio.timeout(self.discovery_timeout_s):
                headers = self.backend_auth.headers(spec, None)  # system identity
                async with connector.connect(headers) as client:
                    live: list[Tool] = []
                    cursor: str | None = None
                    for _ in range(MAX_PAGES):
                        page = await client.list_tools(cursor=cursor)
                        live.extend(page.tools)
                        cursor = page.next_cursor
                        if not cursor:
                            break
        except TimeoutError:
            return "discovery timed out"
        except Exception as exc:
            return type(exc).__name__

        status = BackendStatus(healthy=True)
        entries: list[CatalogTool] = []
        live_names = {t.name for t in live}
        for tool in live:
            pin = pinned.get(tool.name)
            if pin is None:
                status.unregistered.append(tool.name)
            elif definition_hash(tool.description, tool.input_schema) != pin.schema_hash:
                status.drifted.append(tool.name)
                logger.warning(
                    "tool definition drifted, hiding backend=%s tool=%s", spec.name, tool.name
                )
            else:
                public = f"{spec.name}.{tool.name}"
                entries.append(
                    CatalogTool(
                        public_name=public,
                        backend=spec.name,
                        original_name=tool.name,
                        tool=_public_tool(public, pin),
                        permission=pin.permission,
                        timeout_s=pin.timeout_s if pin.timeout_s is not None else spec.timeout_s,
                        connector=connector,
                        spec=spec,
                    )
                )
        status.missing = sorted(set(pinned) - live_names)
        status.exposed = len(entries)
        return entries, status
