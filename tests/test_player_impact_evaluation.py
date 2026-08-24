from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sgr.models import NFLSeasonType
from sgr.research.player_impact_evaluation import evaluate_player_impact_on_missing_starters
from sgr.research.schemas import Game, PlayerGameStatline, RawSnapshotRef, stable_record_id
from sgr.research.storage import ResearchStore

SEASON_START = datetime(2023, 9, 8, tzinfo=timezone.utc)
SOURCE = RawSnapshotRef(
    provider="espn", path="raw/x.json", source_url="https://example.test/x",
    retrieved_at=SEASON_START, sha256="0" * 64,
)


def _week(n: int) -> datetime:
    return SEASON_START + timedelta(days=7 * (n - 1))


def _team_id(abbr: str) -> str:
    return stable_record_id("team", "espn", abbr)


def _player_id(pid: str) -> str:
    return stable_record_id("player", "espn", pid)


def _game(event_id: str, week_number: int, *, home="BUF", away="MIA", home_score=27, away_score=10) -> Game:
    return Game(
        id=stable_record_id("game", "espn", event_id),
        provider_ids={"espn": event_id},
        event_time=_week(week_number),
        retrieved_at=_week(week_number),
        source_snapshots=(SOURCE,),
        season_year=2023,
        season_type=NFLSeasonType.REGULAR,
        week=week_number,
        home_team_id=_team_id(home),
        away_team_id=_team_id(away),
        kickoff_at=_week(week_number),
        status="STATUS_FINAL",
        completed=True,
        neutral_site=False,
        home_score=home_score,
        away_score=away_score,
    )


def _statline(event_id: str, provider_id: str, team_abbr: str, category: str, labels, values, week_number: int) -> PlayerGameStatline:
    return PlayerGameStatline(
        id=stable_record_id("player_game_statline", "espn", event_id, provider_id, category),
        provider_ids={"espn": provider_id},
        event_time=_week(week_number),
        retrieved_at=_week(week_number),
        source_snapshots=(SOURCE,),
        player_id=_player_id(provider_id),
        team_id=_team_id(team_abbr),
        game_id=stable_record_id("game", "espn", event_id),
        stat_category=category,
        stat_labels=tuple(labels),
        stat_values=tuple(values),
    )


def _seed_season(store: ResearchStore, *, missing_week: int | None) -> None:
    games, statlines = [], []
    for i in range(1, 10):
        eid = f"g{i}"
        games.append(_game(eid, i, home_score=27, away_score=10))
        if i != missing_week:
            statlines.append(_statline(eid, "starterQB", "BUF", "passing", ["YDS", "TD", "INT"], ["280", "3", "0"], i))
        else:
            # Backup fills in when the starter is missing this week.
            statlines.append(_statline(eid, "backupQB", "BUF", "passing", ["YDS", "TD", "INT"], ["150", "1", "1"], i))
        if i == 1:
            statlines.append(_statline(eid, "backupQB", "BUF", "passing", ["YDS", "TD", "INT"], ["50", "0", "1"], i))
        statlines.append(_statline(eid, "miaqb", "MIA", "passing", ["YDS", "TD", "INT"], ["200", "1", "1"], i))
    store.write(games)
    store.write(statlines)


def test_evaluation_detects_a_missing_starter_and_scores_the_adjustment(tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    _seed_season(store, missing_week=8)

    import asyncio

    report = asyncio.run(evaluate_player_impact_on_missing_starters(store, [2023]))

    assert report.games_with_missing_starters >= 1
    assert any(s.player_id == _player_id("starterQB") for s in report.samples)
    assert report.baseline_brier is not None
    assert report.adjusted_brier is not None
    assert "passing" in report.position_sample_counts


def test_evaluation_finds_nothing_when_no_starter_is_ever_missing(tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    _seed_season(store, missing_week=None)

    import asyncio

    report = asyncio.run(evaluate_player_impact_on_missing_starters(store, [2023]))

    assert report.games_with_missing_starters == 0
    assert report.samples == ()
    assert report.baseline_brier is None  # no samples to score


def test_evaluation_scoped_to_requested_season_years(tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    _seed_season(store, missing_week=8)

    import asyncio

    report = asyncio.run(evaluate_player_impact_on_missing_starters(store, [2024]))
    assert report.games_considered == 0
    assert report.games_with_missing_starters == 0
