from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sgr.models import NFLSeasonType
from sgr.research.candidate_comparison import CandidateSample, MetricSummary, brier_log_loss_accuracy
from sgr.research.pythagorean import (
    DEFAULT_EXPONENT,
    SHRINKAGE_PSEUDOGAMES,
    InsufficientHistoryError,
    InvalidScoringInputError,
    TeamStrength,
    apply_home_field,
    combine_win_probabilities_log5,
    generate_forecast,
    points_for_against,
    pythagorean_win_pct,
    shrink_toward_prior,
    team_games,
)
from sgr.research.schemas import Game
from sgr.research.significance import fisher_exact_p_value, mcnemar_p_value
from sgr.research.storage import ResearchStore

FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF = 24

# Calibrated by calibrate_prior_season_shrinkage(store, [(2023, 2024), (2024, 2025)])
# -- the closed-form OLS slope of (a team's next-season PPG) on (that same
# team's this-season PPG), fit separately for points-for and points-against
# on this project's own real data. A slope of 1.0 would mean a season
# carries over to the next one completely unchanged (no regression to the
# mean needed); a slope of 0.0 would mean it carries over not at all (full
# regression to league average). See the SUD-112 PR for the fitted values
# and the early-season-specific comparison against the unshrunk baseline.
DEFAULT_PRIOR_SHRINKAGE_FOR = 0.3331739443424684
DEFAULT_PRIOR_SHRINKAGE_AGAINST = 0.25624831456214603


def league_average_ppg(all_games: list[Game], season_year: int) -> tuple[float, float]:
    """(league-average points-for, league-average points-against) per team
    per game, across every team's completed regular-season games in
    season_year. By construction these two numbers are equal (every point
    scored by one team is a point allowed by another across a whole
    season) -- computed separately anyway as a real check on that, not
    assumed.
    """
    season_games = [
        g for g in all_games if g.season_type == NFLSeasonType.REGULAR and g.completed and g.season_year == season_year
    ]
    team_ids = sorted({g.home_team_id for g in season_games} | {g.away_team_id for g in season_games})
    if not team_ids:
        raise InsufficientHistoryError(f"No completed regular-season games found for {season_year}.")

    total_for = 0.0
    total_against = 0.0
    counted_teams = 0
    for team_id in team_ids:
        games = team_games(season_games, team_id)
        if not games:
            continue
        pf, pa = points_for_against(games, team_id)
        total_for += pf / len(games)
        total_against += pa / len(games)
        counted_teams += 1
    return total_for / counted_teams, total_against / counted_teams


def calibrate_prior_season_shrinkage(
    store: ResearchStore, season_pairs: list[tuple[int, int]]
) -> tuple[float, float]:
    """Fit how much of a team's points-for/against carries over to the next
    season: the closed-form OLS slope of next-season PPG on this-season
    PPG, across every team present in both seasons of each (this_year,
    next_year) pair.

    This is a season-level calibration (whole completed seasons in, whole
    completed seasons out) rather than a point-in-time one -- unlike
    SUD-105/110's game-level calibrations, the question here ("how well
    does a complete season predict the next complete season") is itself a
    season-level question, so there is no point-in-time cutoff to enforce
    within a training pair. Same closed-form-over-library preference as
    fit_exponent/calibrate_home_field_margin_points/
    calibrate_points_per_turnover_margin.
    """
    all_games = [g for g in store.load_all("game") if isinstance(g, Game)]

    for_xs: list[float] = []
    for_ys: list[float] = []
    against_xs: list[float] = []
    against_ys: list[float] = []

    for this_year, next_year in season_pairs:
        this_games = [g for g in all_games if g.season_type == NFLSeasonType.REGULAR and g.completed and g.season_year == this_year]
        next_games = [g for g in all_games if g.season_type == NFLSeasonType.REGULAR and g.completed and g.season_year == next_year]
        this_team_ids = {g.home_team_id for g in this_games} | {g.away_team_id for g in this_games}
        next_team_ids = {g.home_team_id for g in next_games} | {g.away_team_id for g in next_games}

        for team_id in sorted(this_team_ids & next_team_ids):
            this_team_games = team_games(this_games, team_id)
            next_team_games = team_games(next_games, team_id)
            if not this_team_games or not next_team_games:
                continue
            this_pf, this_pa = points_for_against(this_team_games, team_id)
            next_pf, next_pa = points_for_against(next_team_games, team_id)
            for_xs.append(this_pf / len(this_team_games))
            for_ys.append(next_pf / len(next_team_games))
            against_xs.append(this_pa / len(this_team_games))
            against_ys.append(next_pa / len(next_team_games))

    def _ols_slope(xs: list[float], ys: list[float]) -> float:
        if len(xs) < 2:
            raise InsufficientHistoryError("Not enough team-season pairs to calibrate a shrinkage slope.")
        mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
        covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        variance = sum((x - mean_x) ** 2 for x in xs)
        if variance == 0:
            raise InsufficientHistoryError("No variance in this-season PPG; cannot fit a slope.")
        return covariance / variance

    return _ols_slope(for_xs, for_ys), _ols_slope(against_xs, against_ys)


def compute_team_strength_with_prior_shrinkage(
    all_games: list[Game],
    team_id: str,
    season_year: int,
    feature_cutoff_at: datetime,
    *,
    exponent: float = DEFAULT_EXPONENT,
    shrinkage_pseudogames: int = SHRINKAGE_PSEUDOGAMES,
    prior_shrinkage_for: float = DEFAULT_PRIOR_SHRINKAGE_FOR,
    prior_shrinkage_against: float = DEFAULT_PRIOR_SHRINKAGE_AGAINST,
) -> TeamStrength:
    """Candidate variant of compute_team_strength (SUD-112): before blending
    prior-season PPG with current-season PPG (the existing shrink_toward_prior
    step, unchanged), the prior season's own PPG-for/PPG-against is first
    regressed toward that prior season's own league-average -- a team
    coming off an extreme season is not carried into the new season at
    exactly that extreme. At current_games_played > 0 this converges to the
    same answer as compute_team_strength as the blend weight shifts onto
    real current-season evidence; the two functions only meaningfully
    differ early in a season, which is the case this ticket targets.
    """
    eligible = [
        g for g in all_games if g.season_type == NFLSeasonType.REGULAR and g.completed and g.kickoff_at < feature_cutoff_at
    ]
    current_season_games = team_games([g for g in eligible if g.season_year == season_year], team_id)
    prior_season_games = team_games([g for g in eligible if g.season_year == season_year - 1], team_id)

    if not current_season_games and not prior_season_games:
        raise InsufficientHistoryError(
            f"No completed regular-season history is available for team {team_id} before {feature_cutoff_at.isoformat()}."
        )

    current_pf, current_pa = points_for_against(current_season_games, team_id)
    prior_pf, prior_pa = points_for_against(prior_season_games, team_id)
    current_games_played = len(current_season_games)
    prior_games_played = len(prior_season_games)

    current_ppg_for = current_pf / current_games_played if current_games_played else None
    current_ppg_against = current_pa / current_games_played if current_games_played else None
    prior_ppg_for = prior_pf / prior_games_played if prior_games_played else None
    prior_ppg_against = prior_pa / prior_games_played if prior_games_played else None

    if prior_ppg_for is not None and prior_ppg_against is not None:
        league_avg_for, league_avg_against = league_average_ppg(all_games, season_year - 1)
        prior_ppg_for = league_avg_for + prior_shrinkage_for * (prior_ppg_for - league_avg_for)
        prior_ppg_against = league_avg_against + prior_shrinkage_against * (prior_ppg_against - league_avg_against)

    blended_for, weight = shrink_toward_prior(
        current_ppg_for, current_games_played, prior_ppg_for, pseudogames=shrinkage_pseudogames
    )
    blended_against, _ = shrink_toward_prior(
        current_ppg_against, current_games_played, prior_ppg_against, pseudogames=shrinkage_pseudogames
    )

    strength = pythagorean_win_pct(blended_for, blended_against, exponent)
    training_window_start = min(
        (g.kickoff_at for g in (*current_season_games, *prior_season_games)), default=feature_cutoff_at
    )

    return TeamStrength(
        team_id=team_id,
        season_year=season_year,
        feature_cutoff_at=feature_cutoff_at,
        exponent=exponent,
        current_games_played=current_games_played,
        current_points_for=current_pf,
        current_points_against=current_pa,
        prior_season_year=season_year - 1 if prior_games_played else None,
        prior_games_played=prior_games_played,
        prior_points_for=prior_pf,
        prior_points_against=prior_pa,
        shrinkage_weight=weight,
        strength=strength,
        training_window_start=training_window_start,
    )


def prior_shrunk_probability(
    all_games: list[Game],
    home_team_id: str,
    away_team_id: str,
    season_year: int,
    feature_cutoff_at: datetime,
    *,
    neutral_site: bool | None,
    exponent: float = DEFAULT_EXPONENT,
    prior_shrinkage_for: float = DEFAULT_PRIOR_SHRINKAGE_FOR,
    prior_shrinkage_against: float = DEFAULT_PRIOR_SHRINKAGE_AGAINST,
) -> float:
    """Baseline candidate (SUD-112): the win probability the existing log5 +
    home-field pipeline would produce using compute_team_strength_with_prior_shrinkage
    instead of compute_team_strength -- isolates whether regressing the
    prior season toward league-average helps, holding everything else
    constant, the same isolation discipline evaluation.py's other
    baselines use.
    """
    home_strength = compute_team_strength_with_prior_shrinkage(
        all_games, home_team_id, season_year, feature_cutoff_at,
        exponent=exponent, prior_shrinkage_for=prior_shrinkage_for, prior_shrinkage_against=prior_shrinkage_against,
    )
    away_strength = compute_team_strength_with_prior_shrinkage(
        all_games, away_team_id, season_year, feature_cutoff_at,
        exponent=exponent, prior_shrinkage_for=prior_shrinkage_for, prior_shrinkage_against=prior_shrinkage_against,
    )
    raw = combine_win_probabilities_log5(home_strength.strength, away_strength.strength)
    return apply_home_field(raw, neutral_site=bool(neutral_site))


@dataclass(frozen=True)
class SignificanceResult:
    """Whether an observed accuracy difference between two predictors on
    the same games is distinguishable from chance. Both tests are exact
    (not chi-square approximations), appropriate for the small held-out
    samples a single real NFL season produces. Fisher's exact treats the
    two predictors' correct/incorrect counts as independent (a common,
    convenient approximation); McNemar's is the statistically correct
    test for this actually-paired case (same games, two predictors) --
    both are reported rather than picking one silently."""

    baseline_correct: int
    candidate_correct: int
    sample_count: int
    fisher_exact_p_value: float | None
    mcnemar_p_value: float | None


def _significance(baseline: list[CandidateSample], candidate: list[CandidateSample]) -> SignificanceResult:
    paired = [
        ((b.predicted >= 0.5) == b.actual_home_win, (c.predicted >= 0.5) == c.actual_home_win)
        for b, c in zip(baseline, candidate)
        if not b.abstained and not b.is_tie and not c.abstained and not c.is_tie
    ]
    n = len(paired)
    baseline_correct = sum(1 for b, _ in paired if b)
    candidate_correct = sum(1 for _, c in paired if c)
    if n == 0:
        return SignificanceResult(0, 0, 0, None, None)
    only_baseline_correct = sum(1 for b, c in paired if b and not c)
    only_candidate_correct = sum(1 for b, c in paired if c and not b)
    return SignificanceResult(
        baseline_correct=baseline_correct,
        candidate_correct=candidate_correct,
        sample_count=n,
        fisher_exact_p_value=fisher_exact_p_value(
            baseline_correct, n - baseline_correct, candidate_correct, n - candidate_correct
        ),
        mcnemar_p_value=mcnemar_p_value(only_baseline_correct, only_candidate_correct),
    )


@dataclass(frozen=True)
class PriorShrinkageComparisonReport:
    season_years: tuple[int, ...]
    week1_baseline: MetricSummary
    week1_prior_shrunk: MetricSummary
    full_season_baseline: MetricSummary
    full_season_prior_shrunk: MetricSummary
    week1_significance: SignificanceResult
    full_season_significance: SignificanceResult


def run_prior_shrinkage_comparison(
    store: ResearchStore,
    season_years: list[int],
    *,
    exponent: float = DEFAULT_EXPONENT,
    prior_shrinkage_for: float = DEFAULT_PRIOR_SHRINKAGE_FOR,
    prior_shrinkage_against: float = DEFAULT_PRIOR_SHRINKAGE_AGAINST,
) -> PriorShrinkageComparisonReport:
    """Walk-forward compares the unadjusted baseline against the shrunk-prior
    candidate, broken out for Week 1 (the regime this ticket targets --
    every team has current_games_played=0 at a Week 1 cutoff by
    construction, so this is the "zero games played" comparison the AC
    calls for) separately from the full season (so a real improvement at
    Week 1 can be checked against not quietly making the rest of the
    season worse).
    """
    all_games = [g for g in store.load_all("game") if isinstance(g, Game)]
    test_games = sorted(
        (
            g
            for g in all_games
            if g.season_type == NFLSeasonType.REGULAR and g.completed and g.season_year in season_years
        ),
        key=lambda g: g.kickoff_at,
    )

    week1_baseline: list[CandidateSample] = []
    week1_shrunk: list[CandidateSample] = []
    full_baseline: list[CandidateSample] = []
    full_shrunk: list[CandidateSample] = []

    for game in test_games:
        cutoff = game.kickoff_at - timedelta(hours=FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF)
        is_tie = game.home_score == game.away_score
        actual_home_win = None if is_tie else (game.home_score or 0) > (game.away_score or 0)

        try:
            baseline_forecast = generate_forecast(
                store, game.id, feature_cutoff_at=cutoff, exponent=exponent, apply_injury_adjustment=False
            )
            baseline_probability = float(baseline_forecast.home_win_probability)
            baseline_sample = CandidateSample(game.id, baseline_probability, actual_home_win, is_tie, False)
        except (InsufficientHistoryError, InvalidScoringInputError):
            baseline_sample = CandidateSample(game.id, None, actual_home_win, is_tie, True)

        try:
            shrunk_probability = prior_shrunk_probability(
                all_games, game.home_team_id, game.away_team_id, game.season_year, cutoff,
                neutral_site=game.neutral_site, exponent=exponent,
                prior_shrinkage_for=prior_shrinkage_for, prior_shrinkage_against=prior_shrinkage_against,
            )
            shrunk_sample = CandidateSample(game.id, shrunk_probability, actual_home_win, is_tie, False)
        except (InsufficientHistoryError, InvalidScoringInputError):
            shrunk_sample = CandidateSample(game.id, None, actual_home_win, is_tie, True)

        full_baseline.append(baseline_sample)
        full_shrunk.append(shrunk_sample)
        if game.week == 1:
            week1_baseline.append(baseline_sample)
            week1_shrunk.append(shrunk_sample)

    return PriorShrinkageComparisonReport(
        season_years=tuple(sorted(set(season_years))),
        week1_baseline=brier_log_loss_accuracy(week1_baseline),
        week1_prior_shrunk=brier_log_loss_accuracy(week1_shrunk),
        full_season_baseline=brier_log_loss_accuracy(full_baseline),
        full_season_prior_shrunk=brier_log_loss_accuracy(full_shrunk),
        week1_significance=_significance(week1_baseline, week1_shrunk),
        full_season_significance=_significance(full_baseline, full_shrunk),
    )
