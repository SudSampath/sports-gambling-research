from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sgr.models import NFLSeasonType
from sgr.research.player_impact import (
    CATEGORY_PRODUCTION_WEIGHTS,
    MissingReplacementError,
    compute_player_usages,
    estimate_player_impact,
    find_replacement,
    points_per_production_unit,
    production_score,
)
from sgr.research.pythagorean import InsufficientHistoryError
from sgr.research.schemas import Game, PlayerGameStatline, RawSnapshotRef, stable_record_id

SEASON_START = datetime(2025, 9, 8, tzinfo=timezone.utc)
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


def _game(event_id: str, week_number: int, *, home="BUF", away="MIA", home_score=27, away_score=17) -> Game:
    return Game(
        id=stable_record_id("game", "espn", event_id),
        provider_ids={"espn": event_id},
        event_time=_week(week_number),
        retrieved_at=_week(week_number),
        source_snapshots=(SOURCE,),
        season_year=2025,
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


# --- production_score ---------------------------------------------------------


def test_production_score_applies_known_weights_and_ignores_unknown_labels():
    statline = _statline("g1", "qb1", "BUF", "passing", ["C/ATT", "YDS", "TD", "INT", "QBR"], ["20/30", "300", "3", "1", "95.0"], 1)
    weights = CATEGORY_PRODUCTION_WEIGHTS["passing"]
    expected = 300 * weights["YDS"] + 3 * weights["TD"] + 1 * weights["INT"]
    assert production_score(statline) == pytest.approx(expected)


def test_production_score_is_zero_for_unrecognized_category():
    statline = _statline("g1", "k1", "BUF", "fumbles", ["FUM", "LOST"], ["1", "1"], 1)
    assert production_score(statline) == 0.0


# --- compute_player_usages / find_replacement ---------------------------------


def _build_season(games_count=5, backup_week=1):
    games = []
    statlines = []
    for i in range(1, games_count + 1):
        eid = f"g{i}"
        games.append(_game(eid, i))
        statlines.append(_statline(eid, "starterQB", "BUF", "passing", ["YDS", "TD", "INT"], ["280", "3", "0"], i))
        if i == backup_week:
            statlines.append(_statline(eid, "backupQB", "BUF", "passing", ["YDS", "TD", "INT"], ["50", "0", "1"], i))
        statlines.append(_statline(eid, "miaqb", "MIA", "passing", ["YDS", "TD", "INT"], ["220", "1", "1"], i))
    return games, statlines


def test_usages_are_point_in_time_and_team_scoped():
    games, statlines = _build_season()
    games_by_id = {g.id: g for g in games}
    cutoff = _week(6)
    usages = compute_player_usages(statlines, games_by_id, _team_id("BUF"), 2025, cutoff)
    player_ids = {u.player_id for u in usages}
    assert _player_id("starterQB") in player_ids
    assert _player_id("miaqb") not in player_ids  # different team, excluded


def test_find_replacement_picks_the_next_most_used_player_in_category():
    games, statlines = _build_season()
    games_by_id = {g.id: g for g in games}
    usages = compute_player_usages(statlines, games_by_id, _team_id("BUF"), 2025, _week(6))
    replacement = find_replacement(usages, _player_id("starterQB"))
    assert replacement.player_id == _player_id("backupQB")


def test_find_replacement_returns_none_with_no_second_player():
    games, statlines = _build_season()
    games_by_id = {g.id: g for g in games}
    usages = compute_player_usages(statlines, games_by_id, _team_id("MIA"), 2025, _week(6))
    assert find_replacement(usages, _player_id("miaqb")) is None


# --- points_per_production_unit -----------------------------------------------


def test_points_per_production_unit_is_positive_and_finite():
    games, statlines = _build_season()
    conversion = points_per_production_unit(statlines, games)
    assert conversion > 0
    import math
    assert math.isfinite(conversion)


# --- estimate_player_impact -----------------------------------------------------


def test_estimate_player_impact_is_positive_for_a_clear_starter():
    games, statlines = _build_season()
    result = estimate_player_impact(
        statlines, games, _player_id("starterQB"), _team_id("BUF"), _team_id("MIA"), 2025, _week(6)
    )
    assert result.mean_impact > 0
    assert result.replacement_player_id == _player_id("backupQB")
    assert result.games_played == 5


def test_estimate_player_impact_abstains_with_no_replacement():
    games, statlines = _build_season()
    with pytest.raises(MissingReplacementError):
        estimate_player_impact(
            statlines, games, _player_id("miaqb"), _team_id("MIA"), _team_id("BUF"), 2025, _week(6)
        )


def test_estimate_player_impact_abstains_for_unknown_player():
    games, statlines = _build_season()
    with pytest.raises(InsufficientHistoryError):
        estimate_player_impact(
            statlines, games, _player_id("nobody"), _team_id("BUF"), _team_id("MIA"), 2025, _week(6)
        )


def test_estimate_player_impact_reflects_real_game_to_game_variance():
    games, statlines = [], []
    varying_yards = ["280", "150", "320", "90", "250"]
    for i in range(1, 6):
        eid = f"g{i}"
        games.append(_game(eid, i))
        statlines.append(_statline(eid, "starterQB", "BUF", "passing", ["YDS", "TD", "INT"], [varying_yards[i - 1], "3", "0"], i))
        if i == 1:
            statlines.append(_statline(eid, "backupQB", "BUF", "passing", ["YDS", "TD", "INT"], ["50", "0", "1"], i))
        statlines.append(_statline(eid, "miaqb", "MIA", "passing", ["YDS", "TD", "INT"], ["220", "1", "1"], i))

    result = estimate_player_impact(
        statlines, games, _player_id("starterQB"), _team_id("BUF"), _team_id("MIA"), 2025, _week(6)
    )
    assert result.impact_stdev > 0


def test_sparse_player_shrinks_toward_league_average():
    # A one-game-sample player with an extreme outlier stat line should be
    # pulled toward the league-average production for their category, not
    # taken at face value.
    games = [_game("g1", 1)]
    statlines = [
        _statline("g1", "oneGameWonder", "BUF", "passing", ["YDS", "TD", "INT"], ["500", "6", "0"], 1),
        _statline("g1", "backupQB", "BUF", "passing", ["YDS", "TD", "INT"], ["50", "0", "1"], 1),
        _statline("g1", "miaqb", "MIA", "passing", ["YDS", "TD", "INT"], ["220", "1", "1"], 1),
    ]
    result = estimate_player_impact(
        statlines, games, _player_id("oneGameWonder"), _team_id("BUF"), _team_id("MIA"), 2025, _week(2)
    )
    assert result.shrinkage_weight < 1.0  # not fully trusted on one game alone


def test_defensive_player_impact_uses_points_against():
    games, statlines = [], []
    for i in range(1, 4):
        eid = f"g{i}"
        games.append(_game(eid, i))
        statlines.append(_statline(eid, "defender1", "BUF", "defensive", ["SACKS", "TOT"], ["2", "6"], i))
        statlines.append(_statline(eid, "defender2", "BUF", "defensive", ["SACKS", "TOT"], ["0", "3"], i))
        statlines.append(_statline(eid, "miaqb", "MIA", "passing", ["YDS", "TD", "INT"], ["220", "1", "1"], i))
    result = estimate_player_impact(
        statlines, games, _player_id("defender1"), _team_id("BUF"), _team_id("MIA"), 2025, _week(4)
    )
    # Losing a productive defender should still register a positive
    # (defender helps the team) mean impact, just realized through
    # points-against rather than points-for.
    assert result.mean_impact > 0
