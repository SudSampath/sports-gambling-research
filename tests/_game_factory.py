"""Synthetic canonical Game/RawSnapshotRef builders for model-layer unit tests.

These tests exercise sgr.research.pythagorean against already-canonical Game
records directly, rather than round-tripping through EspnConnector/JSON --
that boundary is covered by SUD-23/SUD-35's own tests.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sgr.models import NFLSeasonType
from sgr.research.schemas import Game, RawSnapshotRef, stable_record_id

_SOURCE = RawSnapshotRef(
    provider="espn",
    path="synthetic.json",
    source_url="https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates=synthetic",
    retrieved_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    sha256="0" * 64,
)


def team_id(abbr: str) -> str:
    return stable_record_id("team", "espn", abbr)


def make_game(
    *,
    event_id: str,
    season_year: int,
    week: int,
    home_abbr: str,
    away_abbr: str,
    kickoff_at: datetime,
    home_score: int | None,
    away_score: int | None,
    completed: bool,
    season_type: NFLSeasonType = NFLSeasonType.REGULAR,
    neutral_site: bool | None = False,
) -> Game:
    status = "STATUS_FINAL" if completed else "STATUS_SCHEDULED"
    return Game(
        id=stable_record_id("game", "espn", event_id),
        provider_ids={"espn": event_id},
        event_time=kickoff_at,
        retrieved_at=kickoff_at,
        source_snapshots=(_SOURCE,),
        season_year=season_year,
        season_type=season_type,
        week=week,
        home_team_id=team_id(home_abbr),
        away_team_id=team_id(away_abbr),
        kickoff_at=kickoff_at,
        status=status,
        completed=completed,
        neutral_site=neutral_site,
        home_score=home_score,
        away_score=away_score,
    )
