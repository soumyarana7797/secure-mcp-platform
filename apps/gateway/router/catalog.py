"""Tool catalog: what tools exist, which backend owns each, and under what public name."""

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from mcp_types import Tool

from apps.gateway.router.connector import BackendConnector

logger = logging.getLogger(__name__)

MAX_PAGES = 20  # defensive bound on tools/list pagination


@dataclass(frozen=True)
class CatalogTool:
    public_name: str  # e.g. "football.get_fixtures"
    backend: str
    original_name: str  # e.g. "get_fixtures"
    tool: Tool  # as exposed to clients (name already namespaced)
    timeout_s: float


@dataclass
class BackendStatus:
    healthy: bool = False
    tool_count: int = 0
    error: str | None = None


@dataclass
class ToolCatalog:
    connectors: Mapping[str, BackendConnector]
    discovery_timeout_s: float = 5.0
    _tools: dict[str, CatalogTool] = field(default_factory=dict)
    _by_backend: dict[str, list[CatalogTool]] = field(default_factory=dict)
    status: dict[str, BackendStatus] = field(default_factory=dict)

    def tools(self) -> list[Tool]:
        return [entry.tool for entry in self._tools.values()]

    def get(self, public_name: str) -> CatalogTool | None:
        return self._tools.get(public_name)

    @property
    def ready(self) -> bool:
        """Ready once at least one backend has been discovered successfully."""
        return any(s.healthy for s in self.status.values())

    async def refresh(self) -> None:
        names = list(self.connectors)
        results = await asyncio.gather(*(self._discover(n) for n in names))
        for name, outcome in zip(names, results, strict=True):
            if isinstance(outcome, list):
                self._by_backend[name] = outcome
                self.status[name] = BackendStatus(True, len(outcome))
            else:
                # Keep the last known tools so a flapping backend does not vanish from listings.
                kept = len(self._by_backend.get(name, []))
                self.status[name] = BackendStatus(False, kept, outcome)
                logger.warning("catalog refresh failed backend=%s error=%s", name, outcome)
        self._tools = {t.public_name: t for tools in self._by_backend.values() for t in tools}

    async def _discover(self, backend: str) -> list[CatalogTool] | str:
        connector = self.connectors[backend]
        try:
            async with asyncio.timeout(self.discovery_timeout_s):
                async with connector.connect() as client:
                    raw: list[Tool] = []
                    cursor: str | None = None
                    for _ in range(MAX_PAGES):
                        page = await client.list_tools(cursor=cursor)
                        raw.extend(page.tools)
                        cursor = page.next_cursor
                        if not cursor:
                            break
        except TimeoutError:
            return "discovery timed out"
        except Exception as exc:  # noqa: BLE001 - any backend failure must not break the gateway
            return f"{type(exc).__name__}"
        return [self._wrap(connector, t) for t in raw]

    @staticmethod
    def _wrap(connector: BackendConnector, tool: Tool) -> CatalogTool:
        public = f"{connector.spec.name}.{tool.name}"
        return CatalogTool(
            public_name=public,
            backend=connector.spec.name,
            original_name=tool.name,
            tool=tool.model_copy(update={"name": public}),
            timeout_s=connector.spec.timeout_for(tool.name),
        )
