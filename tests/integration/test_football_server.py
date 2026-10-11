"""MCP client -> Football MCP server -> (in-process) sample football API."""

from datetime import UTC, datetime

import httpx
import pytest
from mcp import Client

from apps.football_data_api.main import create_app as create_data_api
from apps.football_server.config import Settings
from apps.football_server.provider import HttpFootballProvider
from apps.football_server.server import build_mcp

NOW = datetime(2026, 10, 11, 12, 30, tzinfo=UTC)


def make_server(error_rate: float = 0.0, timeout_s: float = 3.0, latency_ms: int = 0):
    api = create_data_api(clock=lambda: NOW, token="t")
    provider = HttpFootballProvider(
        "http://data", "t", timeout_s, transport=httpx.ASGITransport(app=api)
    )
    settings = Settings(data_api_token="t")
    return build_mcp(provider, settings), api


@pytest.mark.anyio
async def test_lists_both_tools_with_read_only_hint() -> None:
    mcp, _ = make_server()
    async with Client(mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"get_fixtures", "get_live_scores"}
    assert all(t.annotations and t.annotations.read_only_hint for t in tools.values())


@pytest.mark.anyio
async def test_get_fixtures_returns_structured_matches() -> None:
    mcp, _ = make_server()
    async with Client(mcp) as client:
        result = await client.call_tool(
            "get_fixtures", {"date_from": "2026-10-11", "competition": "PL"}
        )
    assert not result.is_error
    data = result.structured_content
    assert data["count"] == 4
    assert {m["competition"] for m in data["matches"]} == {"PL"}


@pytest.mark.anyio
async def test_get_live_scores() -> None:
    mcp, _ = make_server()
    async with Client(mcp) as client:
        result = await client.call_tool("get_live_scores", {})
    assert result.structured_content["count"] == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    "args",
    [
        {"date_from": "2026-10-12", "date_to": "2026-10-11"},  # reversed
        {"date_from": "2026-10-01", "date_to": "2026-12-01"},  # too wide
        {"competition": "XX"},  # not in schema enum
        {"date_from": "not-a-date"},
    ],
)
async def test_invalid_arguments_are_rejected(args: dict) -> None:
    mcp, _ = make_server()
    async with Client(mcp) as client:
        result = await client.call_tool("get_fixtures", args)
    assert result.is_error


@pytest.mark.anyio
async def test_upstream_failure_becomes_safe_tool_error() -> None:
    mcp, api = make_server()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://d") as c:
        await c.put("/admin/faults", json={"latency_ms": 0, "error_rate": 1.0})
    async with Client(mcp) as client:
        result = await client.call_tool("get_live_scores", {})
    assert result.is_error
    text = result.content[0].text
    assert "HTTP 503" in text
    assert "http://data" not in text and "X-Auth-Token" not in text
