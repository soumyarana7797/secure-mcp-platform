"""Talks to the football tools *through the gateway* (port 9000), never directly."""

import asyncio

from mcp import Client, MCPError


async def main() -> None:
    async with Client("http://127.0.0.1:9000/mcp") as client:
        print("tools:", [t.name for t in (await client.list_tools()).tools])
        live = await client.call_tool("football.get_live_scores", {})
        print("live matches:", live.structured_content["count"])
        bad = await client.call_tool("football.get_fixtures", {"competition": "XX"})
        print("bad arg  -> is_error:", bad.is_error, "|", bad.content[0].text)
        try:
            await client.call_tool("football.drop_tables", {})
        except MCPError as exc:
            print("unknown  -> protocol error:", exc)


asyncio.run(main())
