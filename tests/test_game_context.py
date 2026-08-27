from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sgr.connectors.nflverse import NflverseCsvSnapshot
from sgr.research.game_context import build_game_contexts, ingest_game_contexts
from sgr.research.schemas import GameContext, RawSnapshotRef
from sgr.research.storage import ResearchStore

RETRIEVED_AT = datetime(2026, 8, 27, tzinfo=timezone.utc)
SOURCE = RawSnapshotRef(
    provider="nflverse",
    path=".cache/nflverse/games/games.csv",
    source_url="https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv",
    retrieved_at=RETRIEVED_AT,
    sha256="c" * 64,
)

ROW = {
    "game_id": "2024_01_BAL_KC",
    "season": "2024",
    "game_type": "REG",
    "home_rest": "7",
    "away_rest": "7",
    "div_game": "0",
    "roof": "outdoors",
    "surface": "grass",
    "temp": "72",
    "wind": "8",
    "espn": "401671789",
}
PRESEASON_ROW = {**ROW, "game_id": "2024_00_BAL_KC", "game_type": "PRE", "espn": "401671700"}
NO_ESPN_ROW = {**ROW, "game_id": "2024_02_BAL_KC", "espn": ""}
MISSING_REST_ROW = {**ROW, "game_id": "2024_03_BAL_KC", "espn": "401671701", "home_rest": "NA"}


class _FakeConnector:
    def __init__(self, snapshot: NflverseCsvSnapshot) -> None:
        self._snapshot = snapshot

    async def games(self, *, refresh: bool = False) -> NflverseCsvSnapshot:
        return self._snapshot


def test_only_regular_season_rows_are_ingested():
    snapshot = NflverseCsvSnapshot((ROW, PRESEASON_ROW), SOURCE)
    records, report = build_game_contexts(snapshot, [2024])
    assert len(records) == 1
    assert report.by_season[2024].games_in_source == 1


def test_row_with_no_espn_id_is_excluded_and_reported():
    snapshot = NflverseCsvSnapshot((NO_ESPN_ROW,), SOURCE)
    records, report = build_game_contexts(snapshot, [2024])
    assert records == ()
    assert "2024_02_BAL_KC" in report.unmatched_espn_ids


def test_row_missing_rest_is_excluded_without_error():
    snapshot = NflverseCsvSnapshot((MISSING_REST_ROW,), SOURCE)
    records, report = build_game_contexts(snapshot, [2024])
    assert records == ()


def test_observed_weather_is_retained_but_not_a_feature_field():
    snapshot = NflverseCsvSnapshot((ROW,), SOURCE)
    records, _ = build_game_contexts(snapshot, [2024])
    assert records[0].observed_temp_fahrenheit == 72
    assert records[0].observed_wind_mph == 8


def test_ingest_game_contexts_persists_records_and_is_idempotent(tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    connector = _FakeConnector(NflverseCsvSnapshot((ROW,), SOURCE))

    asyncio.run(ingest_game_contexts(connector, store, [2024]))
    asyncio.run(ingest_game_contexts(connector, store, [2024]))

    stored = [r for r in store.load_all("game_context") if isinstance(r, GameContext)]
    assert len(stored) == 1
