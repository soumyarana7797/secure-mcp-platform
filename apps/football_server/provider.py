"""Provider adapter: the only place that knows how to talk to the upstream football API.

Tools depend on the `FootballProvider` protocol, so swapping the sample API for a real one
(e.g. football-data.org) means writing another class here, not touching the tools.
"""

from datetime import date
from typing import Any, Protocol

import httpx

from apps.football_server.domain import Match


class ProviderError(Exception):
    """Upstream failure. The message is safe to show to an MCP client (no URLs or secrets)."""


class FootballProvider(Protocol):
    async def fixtures(
        self, date_from: date, date_to: date, competition: str | None
    ) -> list[Match]: ...

    async def live(self, competition: str | None) -> list[Match]: ...

    async def aclose(self) -> None: ...


def _to_match(raw: dict[str, Any]) -> Match:
    """Translate the upstream JSON shape into our domain model."""
    return Match(
        id=raw["id"],
        competition=raw["competition"]["code"],
        home_team=raw["homeTeam"]["name"],
        away_team=raw["awayTeam"]["name"],
        kickoff_utc=raw["utcDate"],
        status=raw["status"],
        minute=raw["minute"],
        home_score=raw["score"]["home"],
        away_score=raw["score"]["away"],
    )


class HttpFootballProvider:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"X-Auth-Token": token},
            timeout=httpx.Timeout(timeout_s),
            transport=transport,
        )

    async def _get(self, path: str, params: dict[str, str]) -> list[Match]:
        try:
            response = await self._client.get(path, params=params)
            response.raise_for_status()
            payload = response.json()
            return [_to_match(m) for m in payload["matches"]]
        except httpx.TimeoutException as exc:
            raise ProviderError("football data provider timed out") from exc
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                f"football data provider returned HTTP {exc.response.status_code}"
            ) from exc
        except (httpx.RequestError, KeyError, ValueError) as exc:
            raise ProviderError("football data provider is unavailable") from exc

    async def fixtures(
        self, date_from: date, date_to: date, competition: str | None
    ) -> list[Match]:
        params = {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()}
        if competition:
            params["competition"] = competition
        return await self._get("/v1/fixtures", params)

    async def live(self, competition: str | None) -> list[Match]:
        return await self._get(
            "/v1/matches/live", {"competition": competition} if competition else {}
        )

    async def aclose(self) -> None:
        await self._client.aclose()
