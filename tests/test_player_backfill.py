from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from sgr.connectors.espn import EspnConnector
from sgr.models import NFLAthlete, NFLPlayerStatline, NFLTeam
from sgr.research.player_backfill import backfill_boxscores
from sgr.research.storage import ResearchStore

from _game_factory import make_game

SEASON_START = datetime(2023, 9, 8, tzinfo=timezone.utc)
TEAM = NFLTeam(espn_team_id="2", abbreviation="BUF", name="Buffalo Bills")
ATHLETE = NFLAthlete(espn_athlete_id="1", display_name="Test Player", position="RB")


def _statline(event_id: str) -> NFLPlayerStatline:
    return NFLPlayerStatline(
        event_id=event_id,
        team=TEAM,
        athlete=ATHLETE,
        stat_category="rushing",
        stat_labels=("CAR", "YDS"),
        stat_values=("10", "40"),
        retrieved_at=SEASON_START,
        source_url=f"https://example.test/summary?event={event_id}",
        raw_snapshot_path=f"raw/espn/event-{event_id}/x.json",
        raw_snapshot_sha256="0" * 64,
        normalization_version="espn-summary-v1",
    )


def _seed_games(store: ResearchStore) -> None:
    games = [
        make_game(
            event_id="g1", season_year=2023, week=1, home_abbr="BUF", away_abbr="MIA",
            kickoff_at=SEASON_START, home_score=30, away_score=10, completed=True,
        ),
        make_game(
            event_id="g2", season_year=2023, week=2, home_abbr="BUF", away_abbr="NYJ",
            kickoff_at=SEASON_START, home_score=20, away_score=17, completed=True,
        ),
        make_game(
            event_id="g3-preseason", season_year=2023, week=1, home_abbr="BUF", away_abbr="CAR",
            kickoff_at=SEASON_START, home_score=10, away_score=7, completed=True,
        ),
        make_game(
            event_id="g4-incomplete", season_year=2023, week=3, home_abbr="BUF", away_abbr="NE",
            kickoff_at=SEASON_START, home_score=None, away_score=None, completed=False,
        ),
    ]
    from sgr.models import NFLSeasonType

    games[2] = make_game(
        event_id="g3-preseason", season_year=2023, week=1, home_abbr="BUF", away_abbr="CAR",
        kickoff_at=SEASON_START, home_score=10, away_score=7, completed=True,
        season_type=NFLSeasonType.PRESEASON,
    )
    store.write(games)


def test_backfill_only_considers_completed_regular_season_games(tmp_path, monkeypatch):
    store = ResearchStore(root=tmp_path / "store")
    _seed_games(store)
    connector = EspnConnector(cache_dir=tmp_path / "espn")

    calls = []

    async def fake_game_summary(event_id, *, refresh=False):
        calls.append(event_id)
        return [_statline(event_id)], []

    monkeypatch.setattr(connector, "game_summary", fake_game_summary)

    report = asyncio.run(backfill_boxscores(connector, store, [2023]))

    # Only g1 and g2 (completed regular season) are considered; the
    # preseason game and the incomplete game are excluded.
    assert set(calls) == {"g1", "g2"}
    assert report.games_considered == 2
    assert report.games_with_statlines == 2
    assert report.statlines_written == 2


def test_zero_statline_games_are_listed_explicitly(tmp_path, monkeypatch):
    store = ResearchStore(root=tmp_path / "store")
    _seed_games(store)
    connector = EspnConnector(cache_dir=tmp_path / "espn")

    async def fake_game_summary(event_id, *, refresh=False):
        if event_id == "g2":
            return [], []  # schema drift or provider gap
        return [_statline(event_id)], []

    monkeypatch.setattr(connector, "game_summary", fake_game_summary)

    report = asyncio.run(backfill_boxscores(connector, store, [2023]))

    assert report.games_with_zero_statlines == ("g2",)
    assert report.games_with_statlines == 1


def test_rerun_produces_no_duplicate_statlines(tmp_path, monkeypatch):
    store = ResearchStore(root=tmp_path / "store")
    _seed_games(store)
    connector = EspnConnector(cache_dir=tmp_path / "espn")

    async def fake_game_summary(event_id, *, refresh=False):
        return [_statline(event_id)], []

    monkeypatch.setattr(connector, "game_summary", fake_game_summary)

    asyncio.run(backfill_boxscores(connector, store, [2023]))
    asyncio.run(backfill_boxscores(connector, store, [2023]))

    loaded = store.load_all("player_game_statline")
    assert len(loaded) == 2  # one per game, not duplicated by the second run


def test_backfill_never_writes_availability_reports(tmp_path, monkeypatch):
    store = ResearchStore(root=tmp_path / "store")
    _seed_games(store)
    connector = EspnConnector(cache_dir=tmp_path / "espn")

    async def fake_game_summary(event_id, *, refresh=False):
        return [_statline(event_id)], []

    monkeypatch.setattr(connector, "game_summary", fake_game_summary)
    asyncio.run(backfill_boxscores(connector, store, [2023]))

    assert store.load_all("availability_report") == []
