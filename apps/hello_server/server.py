"""Phase 0 learning server: the smallest useful MCP server.

Run directly (stdio transport): `uv run python -m apps.hello_server.server`
"""

from mcp.server.mcpserver import MCPServer

mcp = MCPServer(name="hello", instructions="A tiny server used to learn MCP basics.")


@mcp.tool()
def greet(name: str, shout: bool = False) -> str:
    """Return a friendly greeting for `name`."""
    message = f"Hello, {name}!"
    return message.upper() if shout else message


if __name__ == "__main__":
    mcp.run("stdio")
