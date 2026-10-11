"""Football MCP server (Streamable HTTP).

Run: `uv run uvicorn apps.football_server.server:create_app --factory --port 9002`
MCP endpoint: http://127.0.0.1:9002/mcp
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import Field
from starlette.applications import Starlette

from apps.football_server.config import Settings
from apps.football_server.domain import Competition, FixturesResult, LiveScoresResult
from apps.football_server.provider import FootballProvider, HttpFootballProvider, ProviderError

DateFrom = Annotated[date | None, Field(description="First day (UTC). Default: today.")]
DateTo = Annotated[date | None, Field(description="Last day (UTC). Default: same as date_from.")]
Comp = Annotated[Competition | None, Field(description="PL or LL. Default: both.")]

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)


def build_mcp(provider: FootballProvider, settings: Settings) -> MCPServer:
    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await provider.aclose()

    mcp = MCPServer(
        name="football",
        instructions="Football fixtures and live scores for PL and LL.",
        lifespan=lifespan,
    )

    @mcp.tool(annotations=READ_ONLY)
    async def get_fixtures(
        date_from: DateFrom = None,
        date_to: DateTo = None,
        competition: Comp = None,
    ) -> FixturesResult:
        """List football fixtures (with status and score if started) in a date range."""
        start = date_from or datetime.now(UTC).date()
        end = date_to or start
        if end < start:
            raise ToolError("date_to must not be before date_from")
        if (end - start) > timedelta(days=settings.max_fixture_range_days):
            raise ToolError(f"date range must be at most {settings.max_fixture_range_days} days")
        try:
            matches = await provider.fixtures(start, end, competition)
        except ProviderError as exc:
            raise ToolError(str(exc)) from exc
        return FixturesResult(
            date_from=start,
            date_to=end,
            competition=competition,
            count=len(matches),
            matches=matches,
        )

    @mcp.tool(annotations=READ_ONLY)
    async def get_live_scores(
        competition: Comp = None,
    ) -> LiveScoresResult:
        """List matches that are in play right now, with the current minute and score."""
        try:
            matches = await provider.live(competition)
        except ProviderError as exc:
            raise ToolError(str(exc)) from exc
        return LiveScoresResult(competition=competition, count=len(matches), matches=matches)

    return mcp


def create_app(
    settings: Settings | None = None, provider: FootballProvider | None = None
) -> Starlette:
    cfg = settings or Settings()
    prov = provider or HttpFootballProvider(
        cfg.data_api_url, cfg.data_api_token, cfg.provider_timeout_s
    )
    mcp = build_mcp(prov, cfg)
    return mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        host=cfg.host,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=cfg.allowed_hosts
        ),
    )
