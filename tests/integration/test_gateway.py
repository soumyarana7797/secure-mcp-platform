"""Gateway behaviour with in-process backends (no ports, no network)."""

import asyncio
import logging

import httpx
import pytest
from mcp import Client, MCPError
from mcp.server.mcpserver import MCPServer

from apps.football_data_api.main import create_app as create_data_api
from apps.football_server.config import Settings as FootballSettings
from apps.football_server.provider import HttpFootballProvider
from apps.football_server.server import build_mcp
from apps.gateway.config import Settings
from apps.gateway.main import create_app
from apps.gateway.mcp_app import build_gateway_server
from apps.gateway.router.catalog import ToolCatalog
from apps.gateway.router.proxy import ToolProxy
from shared.registry.models import BackendSpec, Registry
from tests.integration.test_football_server import NOW


class _KeepOpenProvider(HttpFootballProvider):
    """In-process clients re-run the server lifespan per connection; keep the HTTP client open."""

    async def aclose(self) -> None:
        return None


def make_football() -> tuple[MCPServer, None]:
    api = create_data_api(clock=lambda: NOW, token="t")
    provider = _KeepOpenProvider("http://data", "t", 3.0, transport=httpx.ASGITransport(app=api))
    return build_mcp(provider, FootballSettings(data_api_token="t")), None


class InProcessConnector:
    def __init__(self, spec: BackendSpec, server: MCPServer) -> None:
        self.spec = spec
        self._server = server

    def connect(self) -> Client:
        return Client(self._server)


class BrokenConnector:
    def __init__(self, spec: BackendSpec) -> None:
        self.spec = spec

    def connect(self) -> Client:
        raise ConnectionError("backend down")


def make_lab() -> tuple[MCPServer, list[str]]:
    """A tiny backend with a call counter, a slow tool and a tool that raises."""
    calls: list[str] = []
    lab = MCPServer(name="lab")

    @lab.tool()
    def echo(text: str) -> str:
        calls.append("echo")
        return text

    @lab.tool()
    async def slow() -> str:
        calls.append("slow")
        await asyncio.sleep(2)
        return "done"

    return lab, calls


async def build_gateway(*, lab_timeout: float = 5.0, broken: bool = False):
    football_mcp, _ = make_football()
    lab, calls = make_lab()
    connectors: dict = {
        "football": InProcessConnector(BackendSpec(name="football", url="x"), football_mcp),
        "lab": InProcessConnector(
            BackendSpec(name="lab", url="x", timeout_s=lab_timeout, tool_timeouts={"slow": 0.3}),
            lab,
        ),
    }
    if broken:
        connectors["ghost"] = BrokenConnector(BackendSpec(name="ghost", url="x"))
    catalog = ToolCatalog(connectors, discovery_timeout_s=2)
    await catalog.refresh()
    return build_gateway_server(catalog, ToolProxy(catalog, connectors)), catalog, calls, connectors


@pytest.mark.anyio
async def test_tools_are_namespaced_and_keep_their_schema() -> None:
    server, *_ = await build_gateway()
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert {"football.get_fixtures", "football.get_live_scores", "lab.echo", "lab.slow"} == set(
        tools
    )
    assert "competition" in tools["football.get_fixtures"].input_schema["properties"]
    assert tools["football.get_fixtures"].annotations.read_only_hint is True


@pytest.mark.anyio
async def test_call_is_forwarded_and_result_passes_through() -> None:
    server, *_ = await build_gateway()
    async with Client(server) as client:
        result = await client.call_tool("football.get_fixtures", {"competition": "PL"})
    assert not result.is_error
    assert result.structured_content["count"] == 4


@pytest.mark.anyio
async def test_unknown_tool_is_a_protocol_error() -> None:
    server, *_ = await build_gateway()
    async with Client(server) as client:
        with pytest.raises(MCPError) as exc:
            await client.call_tool("football.delete_everything", {})
    assert "Unknown tool" in str(exc.value)


@pytest.mark.anyio
async def test_unprefixed_name_is_unknown() -> None:
    server, *_ = await build_gateway()
    async with Client(server) as client:
        with pytest.raises(MCPError):
            await client.call_tool("get_fixtures", {})


@pytest.mark.anyio
async def test_invalid_arguments_are_rejected_before_reaching_the_backend() -> None:
    server, _, calls, _ = await build_gateway()
    async with Client(server) as client:
        missing = await client.call_tool("lab.echo", {})
        wrong_type = await client.call_tool("lab.echo", {"text": 123})
    assert missing.is_error and "text" in missing.content[0].text
    assert wrong_type.is_error
    assert calls == []  # backend never invoked


@pytest.mark.anyio
async def test_slow_backend_times_out_with_a_safe_error() -> None:
    server, *_ = await build_gateway()
    async with Client(server) as client:
        result = await client.call_tool("lab.slow", {})
    assert result.is_error
    assert result.content[0].text == "lab.slow timed out after 0.3s"


@pytest.mark.anyio
async def test_dead_backend_does_not_break_the_others() -> None:
    server, catalog, *_ = await build_gateway(broken=True)
    assert catalog.status["ghost"].healthy is False
    assert catalog.status["football"].healthy is True
    assert catalog.ready
    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert "football.get_live_scores" in names
        result = await client.call_tool("football.get_live_scores", {})
    assert not result.is_error


@pytest.mark.anyio
async def test_backend_failure_after_discovery_is_reported_without_leaking_details() -> None:
    server, catalog, _, connectors = await build_gateway()
    connectors["lab"] = BrokenConnector(BackendSpec(name="lab", url="x"))  # dies after discovery
    async with Client(server) as client:
        result = await client.call_tool("lab.echo", {"text": "hi"})
    assert result.is_error
    assert result.content[0].text == "Backend 'lab' is unavailable"
    assert "ConnectionError" not in result.content[0].text


@pytest.mark.anyio
async def test_last_known_tools_survive_a_failed_refresh() -> None:
    _, catalog, _, connectors = await build_gateway()
    connectors["lab"] = BrokenConnector(BackendSpec(name="lab", url="x"))
    await catalog.refresh()
    assert catalog.status["lab"].healthy is False
    assert catalog.get("lab.echo") is not None  # stale-on-error


def test_registry_rejects_bad_names_and_duplicates() -> None:
    with pytest.raises(ValueError):
        BackendSpec(name="foot.ball", url="x")
    with pytest.raises(ValueError):
        Registry(backends=[BackendSpec(name="a", url="x"), BackendSpec(name="a", url="y")])


@pytest.mark.anyio
async def test_http_app_health_ready_request_id_and_mcp_endpoint() -> None:
    football_mcp, _ = make_football()
    connectors = {
        "football": InProcessConnector(BackendSpec(name="football", url="x"), football_mcp)
    }
    app = create_app(Settings(), connectors)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9000") as c:
            assert (await c.get("/healthz")).json() == {"status": "ok"}
            ready = await c.get("/readyz")
            assert ready.status_code == 200
            assert ready.json()["backends"]["football"]["tools"] == 2

            echoed = await c.get("/healthz", headers={"X-Request-ID": "abc-123"})
            assert echoed.headers["x-request-id"] == "abc-123"
            hostile = await c.get("/healthz", headers={"X-Request-ID": "bad id\twith junk"})
            assert hostile.headers["x-request-id"] != "bad id\twith junk"

            rpc = await c.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Accept": "application/json, text/event-stream"},
            )
            names = {t["name"] for t in rpc.json()["result"]["tools"]}
            assert names == {"football.get_fixtures", "football.get_live_scores"}

            wrong_host = await c.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={
                    "Host": "evil.example.com",
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert wrong_host.status_code == 421


@pytest.mark.anyio
async def test_not_ready_when_no_backend_is_healthy() -> None:
    app = create_app(Settings(), {"ghost": BrokenConnector(BackendSpec(name="ghost", url="x"))})
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9000") as c:
            assert (await c.get("/healthz")).status_code == 200  # liveness unaffected
            ready = await c.get("/readyz")
    assert ready.status_code == 503
    assert ready.json()["status"] == "not_ready"


@pytest.mark.anyio
async def test_enum_violation_message_lists_the_allowed_values() -> None:
    server, *_ = await build_gateway()
    async with Client(server) as client:
        result = await client.call_tool("football.get_fixtures", {"competition": "XX"})
    text = result.content[0].text
    assert "'PL'" in text and "'LL'" in text
    assert "type 'null'" not in text


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.mark.anyio
async def test_every_outcome_is_logged() -> None:
    server, *_ = await build_gateway()
    capture = _Capture()
    logger = logging.getLogger("apps.gateway.router.proxy")
    old_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(capture)
    try:
        async with Client(server) as client:
            await client.call_tool("football.get_live_scores", {})
            await client.call_tool("lab.echo", {})
            await client.call_tool("lab.slow", {})
            with pytest.raises(MCPError):
                await client.call_tool("nope.nope", {})
    finally:
        logger.removeHandler(capture)
        logger.setLevel(old_level)
    outcomes = [m.split("outcome=")[1].split()[0] for m in capture.messages if "tool_call" in m]
    assert outcomes == ["ok", "invalid_args", "timeout", "unknown_tool"]
