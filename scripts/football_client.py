"""Calls the Football MCP server over Streamable HTTP using the SDK client."""

import asyncio
import json

from mcp import Client


async def main() -> None:
    async with Client("http://127.0.0.1:9002/mcp") as client:
        tools = await client.list_tools()
        print("tools:", [t.name for t in tools.tools])
        live = await client.call_tool("get_live_scores", {})
        print("live:", json.dumps(live.structured_content["matches"][:1], indent=1))
        fx = await client.call_tool("get_fixtures", {"competition": "PL"})
        print("today's PL fixtures:", fx.structured_content["count"])
        reversed_range = {"date_from": "2026-10-12", "date_to": "2026-10-01"}
        bad = await client.call_tool("get_fixtures", reversed_range)
        print("bad call -> is_error:", bad.is_error, "|", bad.content[0].text)


asyncio.run(main())
