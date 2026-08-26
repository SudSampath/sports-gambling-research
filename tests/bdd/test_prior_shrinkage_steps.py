from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pytest_bdd import given, scenarios, then, when

from _game_factory import make_game, team_id
from sgr.research.pythagorean import (
    DEFAULT_EXPONENT,
    apply_home_field,
    combine_win_probabilities_log5,
    compute_team_strength,
)
from sgr.research.prior_shrinkage import (
    calibrate_prior_season_shrinkage,
    compute_team_strength_with_prior_shrinkage,
    league_average_ppg,
    prior_shrunk_probability,
    run_prior_shrinkage_comparison,
)
from sgr.research.schemas import Game
from sgr.research.storage import ResearchStore

scenarios("../features/prior_shrinkage.feature")

SEASON_2023_START = datetime(2023, 9, 8, tzinfo=timezone.utc)
SEASON_2024_START = datetime(2024, 9, 8, tzinfo=timezone.utc)
SEASON_2025_START = datetime(2025, 9, 8, tzinfo=timezone.utc)


@pytest.fixture
def shrink_context(tmp_path):
    return {"store": ResearchStore(root=tmp_path / "store")}


@given("team-season pairs with a known 0.4 year-over-year scoring carryover")
def known_carryover_pairs(shrink_context):
    store = shrink_context["store"]
    # For-PPG: season-2024 = 0.4 * season-2023 + 12 (a known, recoverable slope).
    # Against-PPG just needs its own real variance across teams -- not
    # asserted on in this scenario, but a constant would make the against
    # regression's variance zero and raise, so it varies too.
    teams = [
        ("T1", 10, 12, 16, 20),
        ("T2", 20, 14, 20, 22),
        ("T3", 30, 16, 24, 24),
        ("T4", 40, 18, 28, 26),
    ]
    games = []
    for abbr, pf_2023, pa_2023, pf_2024, pa_2024 in teams:
        games.append(
            make_game(
                event_id=f"{abbr}-2023", season_year=2023, week=1, home_abbr=abbr, away_abbr=f"OPP-{abbr}-23",
                kickoff_at=SEASON_2023_START, home_score=pf_2023, away_score=pa_2023, completed=True,
            )
        )
        games.append(
            make_game(
                event_id=f"{abbr}-2024", season_year=2024, week=1, home_abbr=abbr, away_abbr=f"OPP-{abbr}-24",
                kickoff_at=SEASON_2024_START, home_score=pf_2024, away_score=pa_2024, completed=True,
            )
        )
    store.write(games)


@when("the prior-season shrinkage rate is calibrated from that data")
def calibrate(shrink_context):
    shrink_context["calibrated"] = calibrate_prior_season_shrinkage(shrink_context["store"], [(2023, 2024)])


@then("the calibrated rate recovers approximately 0.4")
def calibrated_recovers_known_value(shrink_context):
    shrinkage_for, _shrinkage_against = shrink_context["calibrated"]
    assert shrinkage_for == pytest.approx(0.4, abs=1e-6)


def _seed_extreme_prior_team(store: ResearchStore) -> None:
    games = []
    for i in range(1, 5):
        games.append(
            make_game(
                event_id=f"extreme-{i}", season_year=2024, week=i, home_abbr="BUF", away_abbr="MIA",
                kickoff_at=SEASON_2024_START + timedelta(days=7 * (i - 1)),
                home_score=45, away_score=7, completed=True,
            )
        )
    store.write(games)


@given("a team with an extreme prior-season scoring record and no current-season games yet")
def extreme_prior_team(shrink_context):
    _seed_extreme_prior_team(shrink_context["store"])
    shrink_context["cutoff"] = SEASON_2025_START - timedelta(days=1)


@when("team strength is computed with a prior-season shrinkage rate of 1.0")
def compute_with_shrinkage_one(shrink_context):
    store = shrink_context["store"]
    all_games = [g for g in store.load_all("game") if isinstance(g, Game)]
    shrink_context["shrunk"] = compute_team_strength_with_prior_shrinkage(
        all_games, team_id("BUF"), 2025, shrink_context["cutoff"],
        prior_shrinkage_for=1.0, prior_shrinkage_against=1.0,
    )
    shrink_context["unshrunk"] = compute_team_strength(all_games, team_id("BUF"), 2025, shrink_context["cutoff"])


@then("it matches the unshrunk strength exactly")
def matches_unshrunk_exactly(shrink_context):
    assert shrink_context["shrunk"].strength == pytest.approx(shrink_context["unshrunk"].strength)


@when("team strength is computed with a prior-season shrinkage rate of 0.0")
def compute_with_shrinkage_zero(shrink_context):
    store = shrink_context["store"]
    all_games = [g for g in store.load_all("game") if isinstance(g, Game)]
    shrink_context["shrunk"] = compute_team_strength_with_prior_shrinkage(
        all_games, team_id("BUF"), 2025, shrink_context["cutoff"],
        prior_shrinkage_for=0.0, prior_shrinkage_against=0.0,
    )
    shrink_context["league_avg_for"], _ = league_average_ppg(all_games, 2024)


@then("its blended points-for equals that season's league-average points-for exactly")
def blended_equals_league_average(shrink_context):
    # At current_games_played=0, shrink_toward_prior returns the prior
    # value unchanged (weight=0 on current), so the blended strength is
    # driven entirely by the (now fully-regressed) prior PPG -- reconstruct
    # it independently via pythagorean_win_pct to confirm.
    from sgr.research.pythagorean import pythagorean_win_pct

    league_avg_for = shrink_context["league_avg_for"]
    _, league_avg_against = league_average_ppg(
        [g for g in shrink_context["store"].load_all("game") if isinstance(g, Game)], 2024
    )
    expected_strength = pythagorean_win_pct(league_avg_for, league_avg_against, DEFAULT_EXPONENT)
    assert shrink_context["shrunk"].strength == pytest.approx(expected_strength)


@given("two teams with very different prior-season scoring records and no current-season games yet")
def two_teams_different_records(shrink_context):
    store = shrink_context["store"]
    games = []
    for i in range(1, 5):
        games.append(
            make_game(
                event_id=f"buf-{i}", season_year=2024, week=i, home_abbr="BUF", away_abbr="OPP1",
                kickoff_at=SEASON_2024_START + timedelta(days=7 * (i - 1)),
                home_score=40, away_score=10, completed=True,
            )
        )
        games.append(
            make_game(
                event_id=f"mia-{i}", season_year=2024, week=i, home_abbr="OPP2", away_abbr="MIA",
                kickoff_at=SEASON_2024_START + timedelta(days=7 * (i - 1)) + timedelta(hours=1),
                home_score=35, away_score=13, completed=True,
            )
        )
    store.write(games)
    shrink_context["cutoff"] = SEASON_2025_START - timedelta(days=1)


@when("the shrunk-prior win probability is computed for their matchup")
def compute_shrunk_matchup(shrink_context):
    store = shrink_context["store"]
    all_games = [g for g in store.load_all("game") if isinstance(g, Game)]
    cutoff = shrink_context["cutoff"]
    shrink_context["shrunk"] = prior_shrunk_probability(
        all_games, team_id("BUF"), team_id("MIA"), 2025, cutoff, neutral_site=False, exponent=DEFAULT_EXPONENT,
    )
    home_strength = compute_team_strength(all_games, team_id("BUF"), 2025, cutoff)
    away_strength = compute_team_strength(all_games, team_id("MIA"), 2025, cutoff)
    raw = combine_win_probabilities_log5(home_strength.strength, away_strength.strength)
    shrink_context["plain"] = apply_home_field(raw, neutral_site=False)


@then("it differs from the plain unshrunk win probability for the same matchup")
def shrunk_differs_from_plain(shrink_context):
    assert shrink_context["shrunk"] != pytest.approx(shrink_context["plain"])


@given("a season with real completed games across multiple weeks and teams")
def multi_week_season(shrink_context):
    store = shrink_context["store"]
    games = []
    matchups = [("BUF", "MIA"), ("KC", "DEN"), ("SF", "SEA")]
    for idx, (home, away) in enumerate(matchups):
        # Prior-season (2024) history so Week 1 of 2025 has something to
        # forecast from -- otherwise every Week 1 game abstains for lack
        # of any history at all, current or prior.
        games.append(
            make_game(
                event_id=f"prior{idx}", season_year=2024, week=1, home_abbr=home, away_abbr=away,
                kickoff_at=SEASON_2024_START, home_score=24, away_score=17, completed=True,
            )
        )
        for week in range(1, 4):
            games.append(
                make_game(
                    event_id=f"m{idx}w{week}", season_year=2025, week=week, home_abbr=home, away_abbr=away,
                    kickoff_at=SEASON_2025_START + timedelta(days=7 * (week - 1)),
                    home_score=27, away_score=13, completed=True,
                )
            )
    store.write(games)
    shrink_context["season_years"] = [2025]


@when("the prior-shrinkage comparison runs")
def run_comparison(shrink_context):
    shrink_context["report"] = run_prior_shrinkage_comparison(shrink_context["store"], shrink_context["season_years"])


@then("Week 1 metrics and full-season metrics are both reported, for the baseline and the shrunk-prior candidate")
def both_breakdowns_reported(shrink_context):
    report = shrink_context["report"]
    assert report.week1_baseline.sample_count > 0
    assert report.week1_prior_shrunk.sample_count > 0
    assert report.full_season_baseline.sample_count > report.week1_baseline.sample_count
    assert report.full_season_prior_shrunk.sample_count > report.week1_prior_shrunk.sample_count
    assert report.week1_significance.fisher_exact_p_value is not None
    assert report.week1_significance.mcnemar_p_value is not None
    assert report.full_season_significance.fisher_exact_p_value is not None
    assert report.full_season_significance.mcnemar_p_value is not None
