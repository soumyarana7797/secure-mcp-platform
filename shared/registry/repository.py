"""Reads the backend registry and pinned tools from PostgreSQL (runtime, read-only role)."""

from dataclasses import dataclass, field

from shared.db.database import Database
from shared.registry.models import BackendSpec
from shared.registry.pinning import PinnedTool


@dataclass(frozen=True)
class LoadedRegistry:
    backends: list[BackendSpec] = field(default_factory=list)
    pinned: dict[str, dict[str, PinnedTool]] = field(default_factory=dict)  # backend -> tool -> pin


class RegistryProvider:
    """Interface: anything with `async load() -> LoadedRegistry`."""

    async def load(self) -> LoadedRegistry:
        raise NotImplementedError


class StaticRegistryProvider(RegistryProvider):
    """In-memory registry for tests and tooling."""

    def __init__(self, registry: LoadedRegistry) -> None:
        self.registry = registry

    async def load(self) -> LoadedRegistry:
        return self.registry


_SERVERS = "SELECT id, name, url, timeout_s FROM mcp_servers WHERE status = 'active' ORDER BY name"
_TOOLS = """
SELECT s.name AS server, t.name, t.description, t.input_schema, t.schema_hash,
       t.permission, t.timeout_s, t.annotations
FROM mcp_tools t JOIN mcp_servers s ON s.id = t.server_id
WHERE s.status = 'active' AND t.enabled
"""


class DbRegistryProvider(RegistryProvider):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def load(self) -> LoadedRegistry:
        async with self._db.tx() as conn:
            servers = await (await conn.execute(_SERVERS)).fetchall()
            tools = await (await conn.execute(_TOOLS)).fetchall()
        pinned: dict[str, dict[str, PinnedTool]] = {s["name"]: {} for s in servers}
        for row in tools:
            pinned[row["server"]][row["name"]] = PinnedTool(
                name=row["name"],
                description=row["description"],
                input_schema=row["input_schema"],
                schema_hash=row["schema_hash"],
                permission=row["permission"],
                timeout_s=float(row["timeout_s"]) if row["timeout_s"] is not None else None,
                annotations=row["annotations"],
            )
        backends = [
            BackendSpec(
                name=s["name"],
                url=s["url"],
                timeout_s=float(s["timeout_s"]),
                tool_timeouts={
                    n: p.timeout_s for n, p in pinned[s["name"]].items() if p.timeout_s is not None
                },
            )
            for s in servers
        ]
        return LoadedRegistry(backends=backends, pinned=pinned)
