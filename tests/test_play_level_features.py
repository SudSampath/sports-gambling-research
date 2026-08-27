from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sgr.connectors.nflverse import NflverseCsvSnapshot
from sgr.research.play_level_features import ingest_play_level_features
from sgr.research.schemas import RawSnapshotRef, Team, TeamGameEfficiency, stable_record_id
from sgr.research.storage import ResearchStore

RETRIEVED_AT = datetime(2026, 8, 27, tzinfo=timezone.utc)
SOURCE = RawSnapshotRef(
    provider="nflverse",
    path=".cache/nflverse/pbp/2024/pbp.csv",
    source_url="https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_2024.csv",
    retrieved_at=RETRIEVED_AT,
    sha256="e" * 64,
)
GAMES_SOURCE = RawSnapshotRef(
    provider="nflverse",
    path=".cache/nflverse/games/games.csv",
    source_url="https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv",
    retrieved_at=RETRIEVED_AT,
    sha256="f" * 64,
)

PBP_ROW = {
    "game_id": "2024_01_ARI_BUF",
    "season": "2024",
    "season_type": "REG",
    "week": "1",
    "posteam": "BUF",
    "defteam": "ARI",
    "qtr": "1",
    "down": "1",
    "ydstogo": "10",
    "yardline_100": "75",
    "yards_gained": "5",
    "score_differential": "0",
    "epa": "0.5",
    "success": "1",
    "pass": "1",
    "rush": "0",
    "sack": "0",
    "complete_pass": "1",
    "touchdown": "0",
    "cpoe": "3.2",
    "special_teams_play": "0",
}
GAMES_ROW = {"game_id": "2024_01_ARI_BUF", "espn": "401671789"}


class _FakeConnector:
    async def games(self, *, refresh: bool = False) -> NflverseCsvSnapshot:
        return NflverseCsvSnapshot((GAMES_ROW,), GAMES_SOURCE)

    async def play_by_play(self, season_year: int, *, refresh: bool = False) -> NflverseCsvSnapshot:
        return NflverseCsvSnapshot((PBP_ROW,), SOURCE)


def _team(abbreviation: str) -> Team:
    return Team(
        id=stable_record_id("team", "espn", abbreviation),
        provider_ids={"espn": abbreviation},
        event_time=RETRIEVED_AT,
        retrieved_at=RETRIEVED_AT,
        source_snapshots=(GAMES_SOURCE,),
        abbreviation=abbreviation,
        display_name=f"Team {abbreviation}",
    )


def test_ingest_play_level_features_writes_both_variants(tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    store.write([_team("BUF"), _team("ARI")])
    connector = _FakeConnector()

    report = asyncio.run(ingest_play_level_features(connector, store, [2024]))

    assert report.by_season[2024].plays_used == 1
    stored = [r for r in store.load_all("team_game_efficiency") if isinstance(r, TeamGameEfficiency)]
    # One posteam (BUF) x two garbage-time variants = 2 records.
    assert len(stored) == 2
    assert {r.garbage_time_excluded for r in stored} == {True, False}


def test_ingest_play_level_features_is_idempotent(tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    store.write([_team("BUF"), _team("ARI")])
    connector = _FakeConnector()

    asyncio.run(ingest_play_level_features(connector, store, [2024]))
    asyncio.run(ingest_play_level_features(connector, store, [2024]))

    stored = [r for r in store.load_all("team_game_efficiency") if isinstance(r, TeamGameEfficiency)]
    assert len(stored) == 2
