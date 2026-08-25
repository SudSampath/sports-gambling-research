from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from sgr.models import NFLSeasonType
from sgr.research.pythagorean import (
    DEFAULT_EXPONENT,
    InsufficientHistoryError,
    InvalidScoringInputError,
    apply_home_field,
    combine_win_probabilities_log5,
    compute_team_strength,
    fit_exponent,
    generate_forecast,
    pythagorean_win_pct,
)
from sgr.research.schemas import AvailabilityReport, AvailabilityReportClass, RawSnapshotRef, stable_record_id
from sgr.research.storage import ResearchStore

from _game_factory import make_game, team_id

SEASON_START = datetime(2025, 9, 8, tzinfo=timezone.utc)


def _week(n: int) -> datetime:
    return SEASON_START + timedelta(days=7 * (n - 1))


# --- pythagorean_win_pct ----------------------------------------------------


def test_formula_matches_expected_value():
    assert pythagorean_win_pct(400, 300, 2.37) == pytest.approx(0.6641, abs=1e-3)


@pytest.mark.parametrize(
    "points_for, points_against, exponent",
    [
        (0, 300, 2.37),
        (400, 0, 2.37),
        (-10, 300, 2.37),
        (400, -10, 2.37),
        (float("nan"), 300, 2.37),
        (400, float("inf"), 2.37),
        (400, 300, 0),
        (400, 300, -1),
    ],
)
def test_formula_rejects_invalid_inputs(points_for, points_against, exponent):
    with pytest.raises(InvalidScoringInputError):
        pythagorean_win_pct(points_for, points_against, exponent)


def test_formula_is_symmetric_under_team_swap():
    a = pythagorean_win_pct(400, 300, 2.37)
    b = pythagorean_win_pct(300, 400, 2.37)
    assert a + b == pytest.approx(1.0)


# --- fit_exponent ------------------------------------------------------------


def test_fit_exponent_recovers_known_value():
    true_exponent = 2.6
    samples = [
        (pf, pa, pythagorean_win_pct(pf, pa, true_exponent))
        for pf, pa in [(400, 300), (350, 380), (500, 250), (300, 300), (280, 420), (450, 200)]
    ]
    fitted = fit_exponent(samples)
    assert fitted == pytest.approx(true_exponent, abs=1e-3)


def test_fit_exponent_requires_at_least_two_samples():
    with pytest.raises(InsufficientHistoryError):
        fit_exponent([(400, 300, 0.6)])


# --- compute_team_strength: shrinkage and leakage ---------------------------


def test_early_season_strength_is_entirely_prior_season():
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
    strength = compute_team_strength(prior, team_id(team), 2025, _week(1))
    assert strength.current_games_played == 0
    assert strength.shrinkage_weight == 0.0
    assert strength.strength == pytest.approx(pythagorean_win_pct(30, 20, DEFAULT_EXPONENT))


def test_late_season_strength_favors_current_season_evidence():
    team = "BUF"
    prior = [
        make_game(
            event_id=f"prior-{i}",
            season_year=2024,
            week=i,
            home_abbr=team,
            away_abbr="OPP",
            kickoff_at=_week(i) - timedelta(days=365),
            home_score=10,
            away_score=40,  # prior season: a weak team
            completed=True,
        )
        for i in range(1, 4)
    ]
    current = [
        make_game(
            event_id=f"cur-{i}",
            season_year=2025,
            week=i,
            home_abbr=team,
            away_abbr="OPP",
            kickoff_at=_week(i),
            home_score=40,
            away_score=10,  # current season: a strong team
            completed=True,
        )
        for i in range(1, 13)  # 12 games played
    ]
    strength = compute_team_strength(prior + current, team_id(team), 2025, _week(13))
    assert strength.current_games_played == 12
    assert strength.shrinkage_weight > 0.7  # dominated by current season by week 13
    assert strength.strength > 0.7  # reflects the strong current-season form


def test_future_and_incomplete_games_are_excluded_from_strength():
    team = "BUF"
    games = [
        make_game(
            event_id="past-completed",
            season_year=2025,
            week=1,
            home_abbr=team,
            away_abbr="OPP",
            kickoff_at=_week(1),
            home_score=30,
            away_score=10,
            completed=True,
        ),
        make_game(
            event_id="future-completed",
            season_year=2025,
            week=2,
            home_abbr=team,
            away_abbr="OPP",
            kickoff_at=_week(2),  # after the cutoff below
            home_score=50,
            away_score=0,
            completed=True,
        ),
        make_game(
            event_id="past-incomplete",
            season_year=2025,
            week=1,
            home_abbr="OPP2",
            away_abbr=team,
            kickoff_at=_week(1),
            home_score=None,
            away_score=None,
            completed=False,
        ),
    ]
    cutoff = _week(1) + timedelta(days=1)  # after week 1, before week 2
    strength = compute_team_strength(games, team_id(team), 2025, cutoff)
    assert strength.current_games_played == 1
    assert strength.current_points_for == 30
    assert strength.current_points_against == 10


def test_preseason_and_postseason_games_never_contribute():
    team = "BUF"
    games = [
        make_game(
            event_id="preseason",
            season_year=2025,
            week=1,
            home_abbr=team,
            away_abbr="OPP",
            kickoff_at=_week(1) - timedelta(days=30),
            home_score=20,
            away_score=17,
            completed=True,
            season_type=NFLSeasonType.PRESEASON,
        ),
        make_game(
            event_id="postseason",
            season_year=2024,
            week=1,
            home_abbr=team,
            away_abbr="OPP",
            kickoff_at=_week(1) - timedelta(days=200),
            home_score=27,
            away_score=24,
            completed=True,
            season_type=NFLSeasonType.POSTSEASON,
        ),
    ]
    with pytest.raises(InsufficientHistoryError):
        compute_team_strength(games, team_id(team), 2025, _week(1))


def test_no_history_at_all_raises_insufficient_history():
    with pytest.raises(InsufficientHistoryError):
        compute_team_strength([], team_id("BUF"), 2025, _week(1))


# --- log5 combination + home field ------------------------------------------


def test_log5_is_exactly_complementary():
    a, b = 0.63, 0.41
    assert combine_win_probabilities_log5(a, b) + combine_win_probabilities_log5(b, a) == pytest.approx(1.0)


def test_log5_of_equal_strengths_is_a_coin_flip():
    assert combine_win_probabilities_log5(0.5, 0.5) == pytest.approx(0.5)


def test_home_field_increases_probability_when_not_neutral():
    raw = 0.5
    assert apply_home_field(raw, neutral_site=False) > raw


def test_home_field_is_not_applied_at_a_neutral_site():
    raw = 0.5
    assert apply_home_field(raw, neutral_site=True) == raw


# --- generate_forecast: end-to-end, symmetry, reproducibility --------------


def _seed_two_teams(store: ResearchStore, *, home: str, away: str, season_year: int) -> None:
    games = []
    for i in range(1, 6):
        games.append(
            make_game(
                event_id=f"{home}-hist-{i}",
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
                event_id=f"{away}-hist-{i}",
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


def test_generate_forecast_is_bit_for_bit_reproducible(tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    _seed_two_teams(store, home="BUF", away="MIA", season_year=2025)
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
    store.write([matchup])
    cutoff = _week(6) - timedelta(hours=24)
    created_at = datetime(2025, 10, 10, tzinfo=timezone.utc)

    first = generate_forecast(store, matchup.id, feature_cutoff_at=cutoff, forecast_created_at=created_at)
    second = generate_forecast(store, matchup.id, feature_cutoff_at=cutoff, forecast_created_at=created_at)

    assert first == second
    assert first.id == second.id


def test_generate_forecast_neutral_site_swap_is_complementary(tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    _seed_two_teams(store, home="BUF", away="MIA", season_year=2025)
    cutoff = _week(6) - timedelta(hours=24)
    created_at = datetime(2025, 10, 10, tzinfo=timezone.utc)

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

    forward_forecast = generate_forecast(store, forward.id, feature_cutoff_at=cutoff, forecast_created_at=created_at)
    reverse_forecast = generate_forecast(store, reverse.id, feature_cutoff_at=cutoff, forecast_created_at=created_at)

    assert float(forward_forecast.home_win_probability) + float(reverse_forecast.home_win_probability) == pytest.approx(1.0, abs=1e-4)
    assert forward_forecast.home_field_applied is False
    assert reverse_forecast.home_field_applied is False


def test_generate_forecast_excludes_espn_predictor_and_odds_fields(tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    _seed_two_teams(store, home="BUF", away="MIA", season_year=2025)
    matchup = make_game(
        event_id="matchup2",
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
    forecast = generate_forecast(
        store, matchup.id, feature_cutoff_at=_week(6) - timedelta(hours=1)
    )
    fields = set(forecast.model_dump())
    assert fields.isdisjoint({"odds", "predictor", "win_probability", "kalshi_price"})


def test_injury_adjustment_inputs_are_loaded_once_per_store_not_once_per_call(tmp_path, monkeypatch):
    """generate_forecast is routinely called hundreds of times in one process
    (a season simulation, a win-totals report) with apply_injury_adjustment
    left at its default. Before caching, each call re-queried and
    re-parsed the full player_game_statline table (tens of thousands of
    rows in production) from scratch -- fine for one call, but this
    measurably hung real report generation once real availability data
    existed (SUD-109's own short-circuit only helps when that table is
    empty). Asserts the fix: repeated calls on the same store reuse one
    load rather than paying for it every time.
    """
    store = ResearchStore(root=tmp_path / "store")
    _seed_two_teams(store, home="BUF", away="MIA", season_year=2025)
    source = RawSnapshotRef(
        provider="espn", path="raw/x.json", source_url="https://example.test/x",
        retrieved_at=_week(1), sha256="0" * 64,
    )
    # One real availability report is enough to take the code path that
    # used to reload the statline table -- it does not need to actually
    # resolve to OUT for this performance characteristic to matter.
    store.write(
        [
            AvailabilityReport(
                id=stable_record_id("availability_report", "espn", "someone", _week(1).isoformat()),
                provider_ids={"espn": "someone"},
                event_time=_week(1),
                retrieved_at=_week(1),
                source_snapshots=(source,),
                player_id=stable_record_id("player", "espn", "someone"),
                team_id=team_id("BUF"),
                game_id=stable_record_id("game", "espn", "BUF-hist-1"),
                report_class=AvailabilityReportClass.INJURY_STATUS,
                status_text="Questionable",
                source_confidence=Decimal("0.6"),
            )
        ]
    )

    call_counts: dict[str, int] = {}
    original_load_all = ResearchStore.load_all

    def counting_load_all(self, entity_type):
        call_counts[entity_type] = call_counts.get(entity_type, 0) + 1
        return original_load_all(self, entity_type)

    monkeypatch.setattr(ResearchStore, "load_all", counting_load_all)

    cutoff = _week(6) - timedelta(hours=1)
    for i in range(5):
        matchup = make_game(
            event_id=f"matchup-cache-{i}", season_year=2025, week=6, home_abbr="BUF", away_abbr="MIA",
            kickoff_at=_week(6), home_score=None, away_score=None, completed=False,
        )
        store.write([matchup])
        forecast = generate_forecast(store, matchup.id, feature_cutoff_at=cutoff)
        assert 0.0 <= float(forecast.home_win_probability) <= 1.0

    assert call_counts.get("player_game_statline", 0) == 1
    assert call_counts.get("availability_report", 0) == 1
