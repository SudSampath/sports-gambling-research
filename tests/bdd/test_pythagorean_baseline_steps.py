from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from _game_factory import make_game, team_id
from sgr.models import NFLSeasonType
from sgr.research.pythagorean import (
    InsufficientHistoryError,
    InvalidScoringInputError,
    compute_team_strength,
    generate_forecast,
    pythagorean_win_pct,
)
from sgr.research.storage import ResearchStore

scenarios("../features/pythagorean_baseline.feature")

SEASON_START = datetime(2025, 9, 8, tzinfo=timezone.utc)


def _week(n: int) -> datetime:
    return SEASON_START + timedelta(days=7 * (n - 1))


@pytest.fixture
def model_context(tmp_path):
    return {
        "store": ResearchStore(root=tmp_path / "store"),
        "result": None,
        "error": None,
        "forecasts": {},
    }


# --- formula scenarios -------------------------------------------------------


@given("positive, finite points-for and points-against totals and an exponent")
def valid_inputs(model_context):
    model_context["inputs"] = (400.0, 300.0, 2.37)


@when("team strength is calculated")
def calculate_strength_from_inputs_or_context(model_context):
    if "inputs" in model_context:
        pf, pa, x = model_context["inputs"]
        try:
            model_context["result"] = pythagorean_win_pct(pf, pa, x)
        except (InvalidScoringInputError, InsufficientHistoryError) as error:
            model_context["error"] = error
    else:
        try:
            model_context["result"] = compute_team_strength(
                model_context["games"], model_context["team_id"], 2025, _week(1)
            )
        except (InvalidScoringInputError, InsufficientHistoryError) as error:
            model_context["error"] = error


@then("it applies PF^x / (PF^x + PA^x) and returns a finite value between 0 and 1")
def result_matches_formula(model_context):
    pf, pa, x = model_context["inputs"]
    expected = (pf**x) / (pf**x + pa**x)
    assert model_context["result"] == pytest.approx(expected)
    assert 0.0 <= model_context["result"] <= 1.0


@given(parsers.parse("a {description} scoring input"))
def invalid_scoring_input(model_context, description):
    presets = {
        "zero points-for": (0.0, 300.0, 2.37),
        "negative points-for": (-50.0, 300.0, 2.37),
        "non-finite exponent": (400.0, 300.0, float("nan")),
    }
    model_context["inputs"] = presets[description]


@then("a typed invalid scoring input error is returned")
def typed_invalid_input_error(model_context):
    assert isinstance(model_context["error"], InvalidScoringInputError)


# --- point-in-time forecast scenarios ----------------------------------------


def _seed_history(store: ResearchStore, *, home: str, away: str, season_year: int, weeks: range) -> None:
    games = []
    for i in weeks:
        games.append(
            make_game(
                event_id=f"{home}-h{i}",
                season_year=season_year,
                week=i,
                home_abbr=home,
                away_abbr="LEAGUE",
                kickoff_at=_week(i),
                home_score=27,
                away_score=17,
                completed=True,
            )
        )
        games.append(
            make_game(
                event_id=f"{away}-h{i}",
                season_year=season_year,
                week=i,
                home_abbr="LEAGUE",
                away_abbr=away,
                kickoff_at=_week(i) + timedelta(hours=1),
                home_score=24,
                away_score=20,
                completed=True,
            )
        )
    store.write(games)


@given("two teams with games both before and after a prediction timestamp")
def two_teams_with_future_and_past_games(model_context):
    store = model_context["store"]
    _seed_history(store, home="BUF", away="MIA", season_year=2025, weeks=range(1, 5))
    matchup = make_game(
        event_id="matchup",
        season_year=2025,
        week=6,
        home_abbr="BUF",
        away_abbr="MIA",
        kickoff_at=_week(6),
        home_score=None,
        away_score=None,
        completed=False,
    )
    # A game after the matchup, whose data must not leak into the forecast.
    future = make_game(
        event_id="future",
        season_year=2025,
        week=7,
        home_abbr="BUF",
        away_abbr="LEAGUE",
        kickoff_at=_week(7),
        home_score=100,
        away_score=0,
        completed=True,
    )
    store.write([matchup, future])
    model_context["matchup_id"] = matchup.id
    model_context["cutoff"] = _week(6) - timedelta(hours=24)


@when("the game forecast is generated at that timestamp")
def generate_forecast_at_timestamp(model_context):
    model_context["forecast"] = generate_forecast(
        model_context["store"], model_context["matchup_id"], feature_cutoff_at=model_context["cutoff"]
    )


@then("only completed regular-season games before the timestamp are used")
def forecast_excludes_future_game(model_context):
    forecast = model_context["forecast"]
    # The blowout "future" game (100-0) is not reflected: home games played
    # stays at the 4 pre-cutoff games, not 5.
    assert forecast.home_games_played == 4


@then(
    "the home orientation, shrinkage weights, current-season sample, exponent, "
    "model version, feature cutoff, and training window are recorded on the forecast"
)
def forecast_records_provenance(model_context):
    forecast = model_context["forecast"]
    assert forecast.home_field_applied is True
    assert forecast.home_shrinkage_weight is not None
    assert forecast.away_shrinkage_weight is not None
    assert forecast.home_games_played == 4
    assert forecast.away_games_played == 4
    assert forecast.exponent is not None
    assert forecast.model_version == "pythagorean-v1"
    assert forecast.feature_cutoff_at == model_context["cutoff"]
    assert forecast.training_window_start <= forecast.feature_cutoff_at


# --- shrinkage / leakage scenarios -------------------------------------------


@given("a team with prior-season history and no current-season games yet")
def team_with_prior_season_only(model_context):
    team = "BUF"
    prior = [
        make_game(
            event_id=f"prior-{i}",
            season_year=2024,
            week=i,
            home_abbr=team,
            away_abbr="OPP",
            kickoff_at=_week(i) - timedelta(days=365),
            home_score=30,
            away_score=20,
            completed=True,
        )
        for i in range(1, 4)
    ]
    preseason = make_game(
        event_id="preseason-noise",
        season_year=2025,
        week=1,
        home_abbr=team,
        away_abbr="OPP",
        kickoff_at=_week(1) - timedelta(days=20),
        home_score=3,
        away_score=45,  # if this leaked in, it would drag strength down sharply
        completed=True,
        season_type=NFLSeasonType.PRESEASON,
    )
    model_context["games"] = prior + [preseason]
    model_context["team_id"] = team_id(team)


@when("team strength is calculated for the new season")
def calculate_strength_for_new_season(model_context):
    model_context["result"] = compute_team_strength(
        model_context["games"], model_context["team_id"], 2025, _week(1)
    )


@then("the strength is shrunk entirely toward the prior season")
def strength_is_entirely_prior(model_context):
    strength = model_context["result"]
    assert strength.current_games_played == 0
    assert strength.shrinkage_weight == 0.0
    assert strength.strength == pytest.approx(pythagorean_win_pct(30, 20, strength.exponent))


@then("preseason scores are excluded from the calculation")
def preseason_excluded(model_context):
    strength = model_context["result"]
    # 3-45 preseason blowout is not reflected in the (30, 20) prior-season totals.
    assert strength.prior_points_for == 90
    assert strength.prior_points_against == 60


@given("a team with no prior-season and no current-season completed games")
def team_with_no_history(model_context):
    model_context["games"] = []
    model_context["team_id"] = team_id("BUF")


@then("a typed insufficient history error is returned")
def typed_insufficient_history_error(model_context):
    assert isinstance(model_context["error"], InsufficientHistoryError)


# --- neutral-site symmetry scenario ------------------------------------------


@given("the same two teams' historical strength with a neutral-site matchup")
def neutral_site_matchup(model_context):
    store = model_context["store"]
    _seed_history(store, home="BUF", away="MIA", season_year=2025, weeks=range(1, 5))
    forward = make_game(
        event_id="neutral-forward",
        season_year=2025,
        week=6,
        home_abbr="BUF",
        away_abbr="MIA",
        kickoff_at=_week(6),
        home_score=None,
        away_score=None,
        completed=False,
        neutral_site=True,
    )
    reverse = make_game(
        event_id="neutral-reverse",
        season_year=2025,
        week=6,
        home_abbr="MIA",
        away_abbr="BUF",
        kickoff_at=_week(6),
        home_score=None,
        away_score=None,
        completed=False,
        neutral_site=True,
    )
    store.write([forward, reverse])
    model_context["neutral_ids"] = (forward.id, reverse.id)
    model_context["cutoff"] = _week(6) - timedelta(hours=24)


@when("the forecast is generated for each team designated as home")
def generate_both_neutral_forecasts(model_context):
    forward_id, reverse_id = model_context["neutral_ids"]
    store = model_context["store"]
    cutoff = model_context["cutoff"]
    model_context["forecasts"]["forward"] = generate_forecast(store, forward_id, feature_cutoff_at=cutoff)
    model_context["forecasts"]["reverse"] = generate_forecast(store, reverse_id, feature_cutoff_at=cutoff)


@then("the two forecasts' home-win probabilities sum to one")
def neutral_forecasts_are_complementary(model_context):
    forward = model_context["forecasts"]["forward"]
    reverse = model_context["forecasts"]["reverse"]
    total = float(forward.home_win_probability) + float(reverse.home_win_probability)
    assert total == pytest.approx(1.0, abs=1e-4)


@then("neither forecast records a home-field adjustment")
def neither_forecast_has_home_field(model_context):
    assert model_context["forecasts"]["forward"].home_field_applied is False
    assert model_context["forecasts"]["reverse"].home_field_applied is False


# --- reproducibility scenario -------------------------------------------------


@given("a generated forecast for a matchup")
def generated_forecast_for_matchup(model_context):
    store = model_context["store"]
    _seed_history(store, home="BUF", away="MIA", season_year=2025, weeks=range(1, 5))
    matchup = make_game(
        event_id="repro-matchup",
        season_year=2025,
        week=6,
        home_abbr="BUF",
        away_abbr="MIA",
        kickoff_at=_week(6),
        home_score=None,
        away_score=None,
        completed=False,
    )
    store.write([matchup])
    model_context["matchup_id"] = matchup.id
    model_context["cutoff"] = _week(6) - timedelta(hours=24)
    model_context["created_at"] = datetime(2025, 10, 10, tzinfo=timezone.utc)
    model_context["forecasts"]["first"] = generate_forecast(
        store, matchup.id, feature_cutoff_at=model_context["cutoff"], forecast_created_at=model_context["created_at"]
    )


@when("the same forecast is regenerated with identical inputs")
def regenerate_forecast(model_context):
    model_context["forecasts"]["second"] = generate_forecast(
        model_context["store"],
        model_context["matchup_id"],
        feature_cutoff_at=model_context["cutoff"],
        forecast_created_at=model_context["created_at"],
    )


@then("the two forecasts are identical")
def forecasts_are_identical(model_context):
    assert model_context["forecasts"]["first"] == model_context["forecasts"]["second"]
