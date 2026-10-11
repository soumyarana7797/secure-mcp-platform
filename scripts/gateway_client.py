"""Talks to the gateway (port 9000) as a seeded dev user.

    uv run python scripts/gateway_client.py [sub-alice|sub-bob|sub-carol|none]
Requires the gateway to run with GATEWAY_AUTH_MODE=dev_header (local development only).
"""

import asyncio
import os
import sys

import httpx2
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client


async def main(sub: str) -> None:
    headers = {} if sub == "none" else {"X-Dev-Sub": sub}
    http = httpx2.AsyncClient(headers=headers)
    async with Client(
        streamable_http_client(
            os.environ.get("GATEWAY_URL", "http://127.0.0.1:9000/mcp"), http_client=http
        )
    ) as client:
        print(f"[{sub}] tools:", sorted(t.name for t in (await client.list_tools()).tools))
        try:
            live = await client.call_tool("football.get_live_scores", {})
            print(f"[{sub}] live matches:", live.structured_content["count"])
        except MCPError as exc:
            print(f"[{sub}] live scores -> protocol error: {exc}")
        bad = await client.call_tool("football.get_fixtures", {"competition": "XX"})
        print(f"[{sub}] bad arg  -> is_error={bad.is_error} | {bad.content[0].text}")
        try:
            await client.call_tool("football.drop_tables", {})
        except MCPError as exc:
            print(f"[{sub}] unknown  -> protocol error: {exc}")


if __name__ == "__main__":
    try:
        asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "sub-alice"))
    except* Exception as group:
        first = group.exceptions[0]
        print(f"request failed: {type(first).__name__}: {first}")
        sys.exit(1)
