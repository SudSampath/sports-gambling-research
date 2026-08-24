from __future__ import annotations

from datetime import timedelta

import pytest

from sgr.research.holdout_backtest import run_holdout_backtest, select_holdout_game_ids
from sgr.research.schemas import Team, stable_record_id
from sgr.research.storage import ResearchStore

from _game_factory import make_game


# --- select_holdout_game_ids ---------------------------------------------------


def test_selection_is_deterministic_for_the_same_seed():
    ids = [f"g{i}" for i in range(20)]
    a = select_holdout_game_ids(ids, holdout_fraction=0.6, seed=42)
    b = select_holdout_game_ids(ids, holdout_fraction=0.6, seed=42)
    assert a == b


def test_selection_differs_for_a_different_seed():
    ids = [f"g{i}" for i in range(20)]
    a = select_holdout_game_ids(ids, holdout_fraction=0.6, seed=1)
    b = select_holdout_game_ids(ids, holdout_fraction=0.6, seed=2)
    assert a != b


def test_selection_respects_the_holdout_fraction():
    ids = [f"g{i}" for i in range(100)]
    selected = select_holdout_game_ids(ids, holdout_fraction=0.6, seed=42)
    assert len(selected) == 60


def test_selection_result_independent_of_input_order():
    ids = [f"g{i}" for i in range(20)]
    a = select_holdout_game_ids(ids, holdout_fraction=0.6, seed=42)
    b = select_holdout_game_ids(list(reversed(ids)), holdout_fraction=0.6, seed=42)
    assert a == b


def test_invalid_fraction_is_rejected():
    with pytest.raises(ValueError):
        select_holdout_game_ids(["g1"], holdout_fraction=0.0, seed=1)
    with pytest.raises(ValueError):
        select_holdout_game_ids(["g1"], holdout_fraction=1.5, seed=1)


# --- run_holdout_backtest -------------------------------------------------------


def _seed_season(store: ResearchStore, season_start) -> None:
    games = []
    teams = []
    for i in range(1, 18):
        games.append(
            make_game(
                event_id=f"g{i}", season_year=2025, week=i, home_abbr="BUF", away_abbr="MIA",
                kickoff_at=season_start + timedelta(days=7 * (i - 1)),
                home_score=27 if i % 3 else 10, away_score=10 if i % 3 else 27, completed=True,
            )
        )
    store.write(games)
    store.write(
        [
            Team(
                id=stable_record_id("team", "espn", "BUF"), provider_ids={"espn": "BUF"},
                event_time=season_start, retrieved_at=season_start,
                source_snapshots=games[0].source_snapshots, abbreviation="BUF", display_name="Buffalo Bills",
            ),
            Team(
                id=stable_record_id("team", "espn", "MIA"), provider_ids={"espn": "MIA"},
                event_time=season_start, retrieved_at=season_start,
                source_snapshots=games[0].source_snapshots, abbreviation="MIA", display_name="Miami Dolphins",
            ),
        ]
    )


def test_holdout_backtest_produces_readable_rows_with_real_team_abbreviations(tmp_path):
    from datetime import datetime, timezone

    store = ResearchStore(root=tmp_path / "store")
    _seed_season(store, datetime(2025, 9, 8, tzinfo=timezone.utc))

    report = run_holdout_backtest(store, [2025], holdout_fraction=0.6, seed=42)

    assert report.holdout_game_count > 0
    # 17 games seeded, but week 1 has no prior-season data in this
    # synthetic two-team-only league, so it correctly abstains (same
    # InsufficientHistoryError pattern SUD-38 already exercises).
    assert report.full_game_count == 16
    for row in report.rows:
        assert row.home_team in {"BUF", "MIA"}
        assert row.away_team in {"BUF", "MIA"}
        assert row.correct == ((row.predicted_home_win_probability >= 0.5) == row.actual_home_win)


def test_holdout_metrics_and_full_metrics_are_both_reported(tmp_path):
    from datetime import datetime, timezone

    store = ResearchStore(root=tmp_path / "store")
    _seed_season(store, datetime(2025, 9, 8, tzinfo=timezone.utc))

    report = run_holdout_backtest(store, [2025], holdout_fraction=0.6, seed=42)

    assert report.holdout_brier is not None
    assert report.full_brier is not None
    assert report.holdout_accuracy is not None
    assert report.full_accuracy is not None


def test_rerunning_with_the_same_seed_selects_the_same_holdout_games(tmp_path):
    from datetime import datetime, timezone

    store = ResearchStore(root=tmp_path / "store")
    _seed_season(store, datetime(2025, 9, 8, tzinfo=timezone.utc))

    first = run_holdout_backtest(store, [2025], seed=42)
    second = run_holdout_backtest(store, [2025], seed=42)

    assert {r.game_id for r in first.rows} == {r.game_id for r in second.rows}
