from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from apps.football_data_api import data
from apps.football_data_api.main import create_app

DAY = date(2026, 10, 11)
HEADERS = {"X-Auth-Token": "t"}


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 11, hour, minute, tzinfo=UTC)


def client(now: datetime) -> httpx.AsyncClient:
    app = create_app(clock=lambda: now, token="t")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_fixtures_are_deterministic_and_complete() -> None:
    first = data.matches_on(DAY, at(0))
    assert first == data.matches_on(DAY, at(0))
    assert len(first) == 8  # 2 competitions x 4 matches
    assert all(m.home != m.away for m in first)


def test_match_lifecycle_follows_the_clock() -> None:
    slot1 = "PL-20261011-1"  # kicks off 12:00 UTC
    pick = lambda now: next(m for m in data.matches_on(DAY, now) if m.id == slot1)  # noqa: E731
    assert pick(at(11, 59)).status == "SCHEDULED"
    assert pick(at(11, 59)).home_score is None
    mid = pick(at(12, 30))
    assert (mid.status, mid.minute) == ("IN_PLAY", 30)
    done = pick(at(14, 0))
    assert (done.status, done.minute) == ("FINISHED", None)
    assert done.home_score is not None


def test_score_never_decreases_during_a_match() -> None:
    kickoff = at(12)
    totals = []
    for minute in range(0, 96):
        now = kickoff + timedelta(minutes=minute)
        m = next(x for x in data.matches_on(DAY, now) if x.id == "PL-20261011-1")
        totals.append((m.home_score or 0) + (m.away_score or 0))
    assert totals == sorted(totals)


@pytest.mark.anyio
async def test_auth_required() -> None:
    async with client(at(12, 30)) as c:
        r = await c.get("/v1/matches/live")
        assert r.status_code == 403
        assert (await c.get("/healthz")).status_code == 200


@pytest.mark.anyio
async def test_live_endpoint_returns_only_in_play() -> None:
    async with client(at(12, 30)) as c:
        body = (await c.get("/v1/matches/live", headers=HEADERS)).json()
    assert body["count"] == 2  # PL-1 and LL-1 both kicked off at 12:00
    assert {m["status"] for m in body["matches"]} == {"IN_PLAY"}


@pytest.mark.anyio
async def test_fixtures_validation() -> None:
    async with client(at(12)) as c:
        bad = await c.get(
            "/v1/fixtures",
            params={"date_from": "2026-10-12", "date_to": "2026-10-11"},
            headers=HEADERS,
        )
        assert bad.status_code == 422
        bad_comp = await c.get(
            "/v1/fixtures",
            params={"date_from": "2026-10-11", "date_to": "2026-10-11", "competition": "XX"},
            headers=HEADERS,
        )
        assert bad_comp.status_code == 422


@pytest.mark.anyio
async def test_fault_injection_error_rate() -> None:
    async with client(at(12)) as c:
        await c.put("/admin/faults", json={"latency_ms": 0, "error_rate": 1.0})
        assert (await c.get("/v1/matches/live", headers=HEADERS)).status_code == 503
        assert (await c.get("/healthz")).status_code == 200  # only /v1 is affected
