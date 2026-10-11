"""The MCP-facing side of the gateway: a low-level MCP server whose tools come from the catalog."""

from mcp.server import Server
from mcp.server.lowlevel.server import ServerRequestContext
from mcp_types import CallToolRequestParams, CallToolResult, ListToolsResult, PaginatedRequestParams

from apps.gateway.router.catalog import ToolCatalog
from apps.gateway.router.proxy import ToolProxy


def build_gateway_server(catalog: ToolCatalog, proxy: ToolProxy) -> Server:
    async def on_list_tools(
        ctx: ServerRequestContext, params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        return ListToolsResult(tools=catalog.tools())

    async def on_call_tool(
        ctx: ServerRequestContext, params: CallToolRequestParams
    ) -> CallToolResult:
        return await proxy.call_tool(params.name, params.arguments)

    return Server(
        "secure-mcp-gateway",
        instructions="Gateway to football, user and knowledge tools. Tool names are prefixed.",
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
