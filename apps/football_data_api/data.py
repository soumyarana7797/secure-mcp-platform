"""Deterministic fake football data.

Fixtures for a given date are always the same. Live state (status, minute, score) is a pure
function of the match and the current time, so "live" matches progress as the clock moves.
"""

import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

COMPETITIONS: dict[str, tuple[str, list[str]]] = {
    "PL": (
        "Premier League",
        [
            "Arsenal",
            "Chelsea",
            "Liverpool",
            "Man City",
            "Man United",
            "Spurs",
            "Newcastle",
            "Villa",
        ],
    ),
    "LL": (
        "La Liga",
        [
            "Barcelona",
            "Real Madrid",
            "Atletico",
            "Sevilla",
            "Valencia",
            "Betis",
            "Villarreal",
            "Girona",
        ],
    ),
}
KICKOFFS = [time(12, 0), time(14, 30), time(17, 0), time(19, 30)]
MATCH_MINUTES = 95  # 90 plus a little stoppage; no half-time simulation


@dataclass(frozen=True)
class Match:
    id: str
    competition: str
    home: str
    away: str
    kickoff: datetime
    status: str  # SCHEDULED | IN_PLAY | FINISHED
    minute: int | None
    home_score: int | None
    away_score: int | None


def _goal_minutes(match_id: str, side: str) -> list[int]:
    rng = random.Random(f"{match_id}-{side}")
    return sorted(rng.randint(1, 90) for _ in range(rng.randint(0, 4)))


def _build(competition: str, day: date, slot: int, home: str, away: str, now: datetime) -> Match:
    match_id = f"{competition}-{day:%Y%m%d}-{slot + 1}"
    kickoff = datetime.combine(day, KICKOFFS[slot], tzinfo=UTC)
    elapsed = (now - kickoff).total_seconds() / 60
    if elapsed < 0:
        return Match(match_id, competition, home, away, kickoff, "SCHEDULED", None, None, None)
    minute = min(int(elapsed), 90)
    status = "IN_PLAY" if elapsed <= MATCH_MINUTES else "FINISHED"
    shown = minute if status == "IN_PLAY" else 90
    home_score = sum(m <= shown for m in _goal_minutes(match_id, "home"))
    away_score = sum(m <= shown for m in _goal_minutes(match_id, "away"))
    return Match(
        match_id,
        competition,
        home,
        away,
        kickoff,
        status,
        minute if status == "IN_PLAY" else None,
        home_score,
        away_score,
    )


def matches_on(day: date, now: datetime, competition: str | None = None) -> list[Match]:
    result: list[Match] = []
    for code, (_, teams) in COMPETITIONS.items():
        if competition and code != competition:
            continue
        shuffled = teams[:]
        random.Random(f"{code}-{day.isoformat()}").shuffle(shuffled)
        for slot in range(len(KICKOFFS)):
            home, away = shuffled[2 * slot], shuffled[2 * slot + 1]
            result.append(_build(code, day, slot, home, away, now))
    return sorted(result, key=lambda m: (m.kickoff, m.competition))


def matches_between(
    date_from: date, date_to: date, now: datetime, competition: str | None = None
) -> list[Match]:
    out: list[Match] = []
    day = date_from
    while day <= date_to:
        out.extend(matches_on(day, now, competition))
        day += timedelta(days=1)
    return out


def live_matches(now: datetime, competition: str | None = None) -> list[Match]:
    today = now.date()
    return [m for m in matches_on(today, now, competition) if m.status == "IN_PLAY"]
