"""Admin operations on the registry. Uses the OWNER database role; never imported by the gateway."""

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import Tool
from psycopg import AsyncConnection

from shared.registry.manifest import BackendManifest
from shared.registry.pinning import PinnedTool, pin_tool


@dataclass
class SyncReport:
    backend: str
    new: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)  # live definition differs from the pin
    unchanged: list[str] = field(default_factory=list)
    unregistered: list[str] = field(
        default_factory=list
    )  # live but not in the manifest: not exposed
    missing: list[str] = field(default_factory=list)  # in the manifest but not live
    applied_changes: bool = False


async def fetch_live_tools(
    url: str, timeout_s: float = 10.0, headers: Mapping[str, str] | None = None
) -> list[Tool]:
    """List a backend's tools. Backends demand a gateway-signed token, so callers pass headers."""
    async with asyncio.timeout(timeout_s), httpx2.AsyncClient(headers=dict(headers or {})) as http:
        async with Client(streamable_http_client(url, http_client=http)) as client:
            tools: list[Tool] = []
            cursor = None
            while True:
                page = await client.list_tools(cursor=cursor)
                tools.extend(page.tools)
                cursor = page.next_cursor
                if not cursor:
                    return tools


async def sync_backend(
    conn: AsyncConnection,
    manifest: BackendManifest,
    live: list[Tool],
    *,
    approve_changes: bool,
) -> SyncReport:
    report = SyncReport(manifest.name)
    live_by_name = {t.name: t for t in live}
    report.unregistered = sorted(set(live_by_name) - set(manifest.tools))
    report.missing = sorted(set(manifest.tools) - set(live_by_name))

    row = await (
        await conn.execute(
            """
            INSERT INTO mcp_servers (name, url, audience, timeout_s)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (name) DO UPDATE
              SET url = EXCLUDED.url, audience = EXCLUDED.audience, timeout_s = EXCLUDED.timeout_s
            RETURNING id
            """,
            (manifest.name, manifest.url, manifest.audience, manifest.timeout_s),
        )
    ).fetchone()
    server_id = row["id"]
    existing = {
        r["name"]: r["schema_hash"]
        for r in await (
            await conn.execute(
                "SELECT name, schema_hash FROM mcp_tools WHERE server_id = %s", (server_id,)
            )
        ).fetchall()
    }

    for name, tool_manifest in manifest.tools.items():
        if name not in live_by_name:
            continue
        pin: PinnedTool = pin_tool(
            live_by_name[name], tool_manifest.permission, tool_manifest.timeout_s
        )
        if name not in existing:
            report.new.append(name)
        elif existing[name] != pin.schema_hash:
            report.changed.append(name)
            if not approve_changes:
                continue  # leave the old pin in place; an admin must review and approve
            report.applied_changes = True
        else:
            report.unchanged.append(name)
        await conn.execute(
            """
            INSERT INTO mcp_tools (server_id, name, description, input_schema, schema_hash,
                                   permission, timeout_s, annotations)
            VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb)
            ON CONFLICT (server_id, name) DO UPDATE SET
              description = EXCLUDED.description, input_schema = EXCLUDED.input_schema,
              schema_hash = EXCLUDED.schema_hash, permission = EXCLUDED.permission,
              timeout_s = EXCLUDED.timeout_s, annotations = EXCLUDED.annotations, pinned_at = now()
            """,
            (
                server_id,
                pin.name,
                pin.description,
                json.dumps(pin.input_schema),
                pin.schema_hash,
                pin.permission,
                pin.timeout_s,
                json.dumps(pin.annotations) if pin.annotations is not None else None,
            ),
        )
    return report
