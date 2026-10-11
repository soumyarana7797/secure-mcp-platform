"""Phase 0 learning client: launches the hello server over stdio and talks to it."""

import asyncio
import sys

from mcp import Client, StdioServerParameters


async def main() -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "apps.hello_server.server"])
    async with Client(params) as client:
        tools = await client.list_tools()
        for tool in tools.tools:
            print(f"tool: {tool.name} - {tool.description}")
            print(f"  input schema: {tool.input_schema}")
        result = await client.call_tool("greet", {"name": "Soumya", "shout": True})
        print("result:", result.content[0].text)


asyncio.run(main())
