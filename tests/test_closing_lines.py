from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from _game_factory import make_game
from sgr.connectors.nflverse import NflverseCsvSnapshot
from sgr.research.closing_lines import (
    ClosingLineRangeError,
    build_closing_lines,
    ingest_closing_lines,
)
from sgr.research.schemas import ClosingLine, RawSnapshotRef
from sgr.research.storage import ResearchStore

RETRIEVED_AT = datetime(2026, 8, 27, tzinfo=timezone.utc)
SOURCE = RawSnapshotRef(
    provider="nflverse",
    path=".cache/nflverse/games/games.csv",
    source_url="https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv",
    retrieved_at=RETRIEVED_AT,
    sha256="b" * 64,
)

ROW = {
    "game_id": "2024_01_BAL_KC",
    "season": "2024",
    "game_type": "REG",
    "week": "1",
    "home_team": "KC",
    "away_team": "BAL",
    "home_score": "27",
    "away_score": "20",
    "spread_line": "3",
    "total_line": "46",
    "home_moneyline": "-148",
    "away_moneyline": "124",
    "espn": "401671789",
}

PRESEASON_ROW = {**ROW, "game_id": "2024_00_BAL_KC", "game_type": "PRE", "espn": "401671700"}


class _FakeConnector:
    def __init__(self, snapshot: NflverseCsvSnapshot) -> None:
        self._snapshot = snapshot

    async def games(self, *, refresh: bool = False) -> NflverseCsvSnapshot:
        return self._snapshot


def test_only_regular_season_rows_are_ingested():
    snapshot = NflverseCsvSnapshot((ROW, PRESEASON_ROW), SOURCE)

    records, report = build_closing_lines(snapshot, [2024])

    assert len(records) == 1
    assert report.by_season[2024].games_in_source == 1


def test_season_range_below_1999_is_rejected():
    snapshot = NflverseCsvSnapshot((ROW,), SOURCE)

    with pytest.raises(ClosingLineRangeError):
        build_closing_lines(snapshot, [1998])


def test_matched_to_local_game_reflects_existing_canonical_games():
    snapshot = NflverseCsvSnapshot((ROW,), SOURCE)
    game = make_game(
        event_id="401671789",
        season_year=2024,
        week=1,
        home_abbr="KC",
        away_abbr="BAL",
        kickoff_at=RETRIEVED_AT,
        home_score=27,
        away_score=20,
        completed=True,
    )

    _, report_without_game = build_closing_lines(snapshot, [2024])
    _, report_with_game = build_closing_lines(
        snapshot, [2024], existing_game_ids=frozenset({game.id})
    )

    assert report_without_game.matched_to_local_game == 0
    assert report_with_game.matched_to_local_game == 1


def test_ingest_closing_lines_persists_records_and_reports_coverage(tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    connector = _FakeConnector(NflverseCsvSnapshot((ROW,), SOURCE))

    report = asyncio.run(ingest_closing_lines(connector, store, [2024]))

    assert report.games_written == 1
    stored = [r for r in store.load_all("closing_line") if isinstance(r, ClosingLine)]
    assert len(stored) == 1
    assert stored[0].home_spread == 3


def test_ingest_closing_lines_is_idempotent_on_rerun(tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    connector = _FakeConnector(NflverseCsvSnapshot((ROW,), SOURCE))

    asyncio.run(ingest_closing_lines(connector, store, [2024]))
    asyncio.run(ingest_closing_lines(connector, store, [2024]))

    stored = [r for r in store.load_all("closing_line") if isinstance(r, ClosingLine)]
    assert len(stored) == 1
