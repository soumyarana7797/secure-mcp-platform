"""Domain models returned by the Football MCP tools. Independent of any upstream API shape."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

Competition = Literal["PL", "LL"]
MatchStatus = Literal["SCHEDULED", "IN_PLAY", "FINISHED"]


class Match(BaseModel):
    id: str
    competition: Competition
    home_team: str
    away_team: str
    kickoff_utc: datetime
    status: MatchStatus
    minute: int | None = None
    home_score: int | None = None
    away_score: int | None = None


class FixturesResult(BaseModel):
    date_from: date
    date_to: date
    competition: Competition | None
    count: int
    matches: list[Match]


class LiveScoresResult(BaseModel):
    competition: Competition | None
    count: int
    matches: list[Match]
