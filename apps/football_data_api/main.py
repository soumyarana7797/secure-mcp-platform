"""Sample football data provider (stands in for a third-party API such as football-data.org).

Run: `uv run uvicorn apps.football_data_api.main:create_app --factory --port 9001`
"""

import asyncio
import os
import random
from collections.abc import Callable
from datetime import UTC, date, datetime

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from apps.football_data_api import data

Clock = Callable[[], datetime]


class Faults(BaseModel):
    latency_ms: int = Field(0, ge=0, le=30_000)
    error_rate: float = Field(0.0, ge=0.0, le=1.0)


def _match_json(m: data.Match) -> dict[str, object]:
    return {
        "id": m.id,
        "competition": {"code": m.competition, "name": data.COMPETITIONS[m.competition][0]},
        "utcDate": m.kickoff.isoformat(),
        "status": m.status,
        "minute": m.minute,
        "homeTeam": {"name": m.home},
        "awayTeam": {"name": m.away},
        "score": {"home": m.home_score, "away": m.away_score},
    }


def create_app(clock: Clock | None = None, token: str | None = None) -> FastAPI:
    now: Clock = clock or (lambda: datetime.now(UTC))
    expected_token = token or os.environ.get("FOOTBALL_DATA_API_TOKEN", "dev-token")
    app = FastAPI(title="Sample Football Data API")
    faults = Faults()

    @app.middleware("http")
    async def inject_faults(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path.startswith("/v1/"):
            if faults.latency_ms:
                await asyncio.sleep(faults.latency_ms / 1000)
            if faults.error_rate and random.random() < faults.error_rate:
                return JSONResponse({"detail": "injected failure"}, status_code=503)
        return await call_next(request)

    def check_token(x_auth_token: str | None) -> None:
        if x_auth_token != expected_token:
            raise HTTPException(status_code=403, detail="invalid or missing X-Auth-Token")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/fixtures")
    async def fixtures(
        date_from: date,
        date_to: date,
        competition: str | None = Query(None, pattern="^(PL|LL)$"),
        x_auth_token: str | None = Header(None),
    ) -> dict[str, object]:
        check_token(x_auth_token)
        if date_to < date_from:
            raise HTTPException(status_code=422, detail="date_to before date_from")
        found = data.matches_between(date_from, date_to, now(), competition)
        return {"count": len(found), "matches": [_match_json(m) for m in found]}

    @app.get("/v1/matches/live")
    async def live(
        competition: str | None = Query(None, pattern="^(PL|LL)$"),
        x_auth_token: str | None = Header(None),
    ) -> dict[str, object]:
        check_token(x_auth_token)
        found = data.live_matches(now(), competition)
        return {"count": len(found), "matches": [_match_json(m) for m in found]}

    @app.put("/admin/faults")
    async def set_faults(new: Faults) -> Faults:
        nonlocal faults
        faults = new
        return faults

    @app.get("/admin/faults")
    async def get_faults() -> Faults:
        return faults

    return app
