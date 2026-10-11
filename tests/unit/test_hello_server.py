import pytest
from mcp import Client

from apps.hello_server.server import mcp


@pytest.mark.anyio
async def test_greet_over_in_process_client() -> None:
    async with Client(mcp) as client:
        tools = await client.list_tools()
        assert [t.name for t in tools.tools] == ["greet"]
        result = await client.call_tool("greet", {"name": "Ada"})
        assert result.content[0].text == "Hello, Ada!"
