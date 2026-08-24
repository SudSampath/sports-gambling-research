from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sgr.models import NFLSeasonType
from sgr.research.evaluation import brier_score, calibration_bins, log_loss, GameSample
from sgr.research.pythagorean import (
    CALIBRATION_VERSION_UNCALIBRATED,
    DEFAULT_EXPONENT,
    HOME_FIELD_LOGIT_BUMP,
    MODEL_VERSION,
    SHRINKAGE_PSEUDOGAMES,
    InsufficientHistoryError,
    InvalidScoringInputError,
    TeamStrength,
    apply_home_field,
    combine_win_probabilities_log5,
    logit,
    points_for_against,
    pythagorean_win_pct,
    shrink_toward_prior,
    sigmoid,
    team_games,
)
from sgr.research.schemas import Forecast, Game, stable_record_id
from sgr.research.storage import ResearchStore

# Platt scaling: a 2-parameter logistic recalibration of the raw
# log5+home-field probability, fit by Newton-Raphson on a training fold.
# CALIBRATION_VERSION_UNCALIBRATED (imported above) is also this module's
# mandatory fallback when calibration is rejected for insufficient data.
CALIBRATION_VERSION_PLATT = "platt-v1"

# How much a team's own full prior season is trusted before regressing
# toward that season's league-average scoring rate. A whole prior season
# (~17 games) is worth more evidence than a handful of current-season
# games, so this is deliberately larger than SHRINKAGE_PSEUDOGAMES.
LEAGUE_SHRINKAGE_PSEUDOGAMES = 8

# AC: "a small-sample method is rejected or pooled rather than fit to
# unstable probability bins." Below this many training-fold samples,
# calibration fitting is skipped entirely in favor of the uncalibrated
# fallback.
MINIMUM_CALIBRATION_SAMPLES = 50

# AC: calibration must not be selected if it introduces material
# overconfidence, even if it improves Brier/log loss. A calibration bin
# with at least this many samples may not disagree with its own actual
# rate by more than this margin.
MAX_CALIBRATION_GAP = 0.25
MIN_BIN_COUNT_FOR_OVERCONFIDENCE_CHECK = 5

TIE_SETTLEMENT_VALUE = Decimal("0.5")


class CalibrationError(RuntimeError):
    """Base error for the probability calibration layer."""


# --- tie-settlement-consistent expected payout -------------------------------


def expected_contract_payout(home_win_probability: Decimal, tie_probability: Decimal) -> Decimal:
    """Expected payout of the home-side winner contract: pays 1 if home
    wins, Kalshi's documented $0.50 if tie, 0 if away wins."""
    return home_win_probability * Decimal("1") + tie_probability * TIE_SETTLEMENT_VALUE


def away_expected_contract_payout(home_win_probability: Decimal, tie_probability: Decimal) -> Decimal:
    away_win_probability = Decimal("1") - home_win_probability - tie_probability
    return away_win_probability * Decimal("1") + tie_probability * TIE_SETTLEMENT_VALUE


# --- league-average-regressed team strength ----------------------------------


def league_average_points_per_team_game(all_games: list[Game], season_year: int) -> float | None:
    """A season's league-wide average points per team-game. Points-for and
    points-against average to the same number league-wide by construction
    (every point scored by one team is conceded by another), so one number
    serves as the regression target for both."""
    games = [
        g for g in all_games
        if g.season_type == NFLSeasonType.REGULAR and g.completed and g.season_year == season_year
    ]
    if not games:
        return None
    total_points = sum((g.home_score or 0) + (g.away_score or 0) for g in games)
    return total_points / (len(games) * 2)


def compute_calibrated_team_strength(
    all_games: list[Game],
    team_id: str,
    season_year: int,
    feature_cutoff_at: datetime,
    *,
    exponent: float = DEFAULT_EXPONENT,
    current_pseudogames: int = SHRINKAGE_PSEUDOGAMES,
    league_pseudogames: int = LEAGUE_SHRINKAGE_PSEUDOGAMES,
) -> TeamStrength:
    """Like pythagorean.compute_team_strength, but the prior-season rate is
    itself first regressed toward that season's league average before being
    used as the current season's shrinkage target -- SUD-39's "prior-season
    strength is shrunk toward league average with a predeclared sample-size
    rule," layered on top of SUD-25's current-vs-prior shrinkage rather than
    replacing it.
    """
    eligible = [
        g for g in all_games
        if g.season_type == NFLSeasonType.REGULAR and g.completed and g.kickoff_at < feature_cutoff_at
    ]
    current_season_games = team_games([g for g in eligible if g.season_year == season_year], team_id)
    prior_season_games = team_games([g for g in eligible if g.season_year == season_year - 1], team_id)

    if not current_season_games and not prior_season_games:
        raise InsufficientHistoryError(
            f"No completed regular-season history is available for team {team_id} "
            f"before {feature_cutoff_at.isoformat()}."
        )

    current_pf, current_pa = points_for_against(current_season_games, team_id)
    prior_pf, prior_pa = points_for_against(prior_season_games, team_id)
    current_games_played = len(current_season_games)
    prior_games_played = len(prior_season_games)

    current_ppg_for = current_pf / current_games_played if current_games_played else None
    current_ppg_against = current_pa / current_games_played if current_games_played else None
    prior_ppg_for = prior_pf / prior_games_played if prior_games_played else None
    prior_ppg_against = prior_pa / prior_games_played if prior_games_played else None

    # Regress the team's own prior season toward that season's league
    # average before it becomes the current season's shrinkage target: the
    # team's full prior season is "current_value" here, being blended
    # toward the league-wide rate ("prior_value") with league_pseudogames
    # controlling how much a single team-season is trusted vs. reverting to
    # the mean.
    league_avg = league_average_points_per_team_game(all_games, season_year - 1)
    if prior_ppg_for is not None and league_avg is not None:
        prior_ppg_for, _ = shrink_toward_prior(
            prior_ppg_for, prior_games_played, league_avg, pseudogames=league_pseudogames
        )
        prior_ppg_against, _ = shrink_toward_prior(
            prior_ppg_against, prior_games_played, league_avg, pseudogames=league_pseudogames
        )

    blended_for, weight = shrink_toward_prior(
        current_ppg_for, current_games_played, prior_ppg_for,
        pseudogames=current_pseudogames,
    )
    blended_against, _ = shrink_toward_prior(
        current_ppg_against, current_games_played, prior_ppg_against,
        pseudogames=current_pseudogames,
    )

    strength = pythagorean_win_pct(blended_for, blended_against, exponent)

    training_window_start = min(
        (g.kickoff_at for g in (*current_season_games, *prior_season_games)),
        default=feature_cutoff_at,
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


# --- Platt scaling: 2-parameter logistic recalibration -----------------------


def fit_platt_scaling(
    raw_probabilities: list[float], outcomes: list[bool], *, iterations: int = 50, l2_penalty: float = 1e-3
) -> tuple[float, float]:
    """Fit calibrated = sigmoid(a + b*logit(raw)) via Newton-Raphson (IRLS)
    with a small L2 penalty on b, minimizing log loss on the given
    (training-fold-only) samples. Every coefficient is learned only from
    whatever the caller passes in -- this function has no notion of time
    and enforces no fold boundary itself.

    l2_penalty on the slope only (not the intercept) keeps the fit
    well-posed when raw scores are perfectly or near-perfectly separable --
    the unregularized MLE for logistic regression is undefined in that case
    (the slope diverges to infinity chasing zero training loss), which is a
    realistic scenario here whenever one team's games all point the same
    way in a small fold. Small enough to leave well-behaved fits (ample,
    non-separable data) essentially unchanged.
    """
    if len(raw_probabilities) != len(outcomes) or len(raw_probabilities) < 2:
        raise CalibrationError("Platt scaling needs at least two matched (probability, outcome) samples.")

    x = [logit(min(max(p, 1e-6), 1 - 1e-6)) for p in raw_probabilities]
    y = [1.0 if o else 0.0 for o in outcomes]
    a, b = 0.0, 1.0
    for _ in range(iterations):
        preds = [sigmoid(a + b * xi) for xi in x]
        grad_a = sum(p - yi for p, yi in zip(preds, y))
        grad_b = sum((p - yi) * xi for p, yi, xi in zip(preds, y, x)) + l2_penalty * b
        weights = [p * (1 - p) for p in preds]
        h_aa = sum(weights) or 1e-9
        h_ab = sum(wi * xi for wi, xi in zip(weights, x))
        h_bb = sum(wi * xi * xi for wi, xi in zip(weights, x)) + l2_penalty
        determinant = h_aa * h_bb - h_ab * h_ab
        if abs(determinant) < 1e-12:
            break
        delta_a = (h_bb * grad_a - h_ab * grad_b) / determinant
        delta_b = (h_aa * grad_b - h_ab * grad_a) / determinant
        a -= delta_a
        b -= delta_b
    return a, b


def apply_platt_scaling(raw_probability: float, a: float, b: float) -> float:
    return sigmoid(a + b * logit(min(max(raw_probability, 1e-6), 1 - 1e-6)))


# --- calibration method selection --------------------------------------------


@dataclass(frozen=True)
class CalibrationChoice:
    calibration_version: str
    platt_a: float | None
    platt_b: float | None
    fit_sample_count: int
    validation_brier_uncalibrated: float | None
    validation_brier_calibrated: float | None
    rejected_reason: str | None


def _raw_forecast_probability(
    all_games: list[Game], game: Game, feature_cutoff_at: datetime, exponent: float
) -> float:
    home_strength = compute_calibrated_team_strength(
        all_games, game.home_team_id, game.season_year, feature_cutoff_at, exponent=exponent
    )
    away_strength = compute_calibrated_team_strength(
        all_games, game.away_team_id, game.season_year, feature_cutoff_at, exponent=exponent
    )
    raw = combine_win_probabilities_log5(home_strength.strength, away_strength.strength)
    return apply_home_field(raw, neutral_site=bool(game.neutral_site))


def select_calibration_method(
    store: ResearchStore,
    training_season_years: list[int],
    *,
    exponent: float = DEFAULT_EXPONENT,
    minimum_samples: int = MINIMUM_CALIBRATION_SAMPLES,
) -> CalibrationChoice:
    """Choose between Platt scaling and the uncalibrated fallback using only
    training_season_years, with an internal fit/validation split carried out
    entirely inside those years -- the walk-forward test fold used later for
    reporting (SUD-38) is never referenced here.

    Fits Platt scaling on all but the most recent training year, validates
    on that most recent training year, and adopts it only if it both beats
    the uncalibrated baseline's Brier score and does not introduce a
    calibration-bin gap larger than MAX_CALIBRATION_GAP.
    """
    years = sorted(set(training_season_years))
    if len(years) < 2:
        return CalibrationChoice(
            CALIBRATION_VERSION_UNCALIBRATED, None, None, 0, None, None,
            "At least two training seasons are required to fit and internally validate calibration.",
        )

    fit_years, validation_year = years[:-1], years[-1]
    all_games = [g for g in store.load_all("game") if isinstance(g, Game)]

    def _samples_for(season_years: list[int]) -> tuple[list[float], list[bool], list[GameSample]]:
        games = sorted(
            (
                g for g in all_games
                if g.season_type == NFLSeasonType.REGULAR and g.completed and g.season_year in season_years
            ),
            key=lambda g: g.kickoff_at,
        )
        raw_probs, outcomes, samples = [], [], []
        for game in games:
            if game.home_score == game.away_score:
                continue  # ties excluded from calibration fitting/scoring, same as evaluation
            cutoff = game.kickoff_at
            try:
                raw = _raw_forecast_probability(all_games, game, cutoff, exponent)
            except (InsufficientHistoryError, InvalidScoringInputError):
                continue
            win = game.home_score > game.away_score
            raw_probs.append(raw)
            outcomes.append(win)
            samples.append(GameSample(game.id, game.season_year, game.week, game.kickoff_at, raw, win, False, False, None))
        return raw_probs, outcomes, samples

    fit_raw, fit_outcomes, _ = _samples_for(fit_years)
    if len(fit_raw) < minimum_samples:
        return CalibrationChoice(
            CALIBRATION_VERSION_UNCALIBRATED, None, None, len(fit_raw), None, None,
            f"Only {len(fit_raw)} training samples available; below the {minimum_samples}-sample "
            "minimum, so calibration is pooled into the uncalibrated fallback rather than fit.",
        )

    platt_a, platt_b = fit_platt_scaling(fit_raw, fit_outcomes)

    validation_raw, validation_outcomes, validation_samples = _samples_for([validation_year])
    if not validation_samples:
        return CalibrationChoice(
            CALIBRATION_VERSION_UNCALIBRATED, platt_a, platt_b, len(fit_raw), None, None,
            "No internal validation-year samples were scorable; falling back to uncalibrated.",
        )

    uncalibrated_brier = brier_score(validation_samples)
    calibrated_samples = [
        GameSample(s.game_id, s.season_year, s.week, s.kickoff_at,
                   apply_platt_scaling(s.predicted_home_win_probability, platt_a, platt_b),
                   s.actual_home_win, False, False, None)
        for s in validation_samples
    ]
    calibrated_brier = brier_score(calibrated_samples)

    if calibrated_brier is None or uncalibrated_brier is None or calibrated_brier >= uncalibrated_brier:
        return CalibrationChoice(
            CALIBRATION_VERSION_UNCALIBRATED, platt_a, platt_b, len(fit_raw),
            uncalibrated_brier, calibrated_brier,
            "Platt scaling did not improve validation-year Brier score over the uncalibrated baseline.",
        )

    max_gap = max(
        (abs(b.mean_predicted - b.actual_win_rate) for b in calibration_bins(calibrated_samples)
         if b.count >= MIN_BIN_COUNT_FOR_OVERCONFIDENCE_CHECK),
        default=0.0,
    )
    if max_gap > MAX_CALIBRATION_GAP:
        return CalibrationChoice(
            CALIBRATION_VERSION_UNCALIBRATED, platt_a, platt_b, len(fit_raw),
            uncalibrated_brier, calibrated_brier,
            f"Platt-calibrated probabilities showed a {max_gap:.2f} calibration-bin gap, "
            f"exceeding the {MAX_CALIBRATION_GAP} material-overconfidence threshold.",
        )

    return CalibrationChoice(
        CALIBRATION_VERSION_PLATT, platt_a, platt_b, len(fit_raw),
        uncalibrated_brier, calibrated_brier, None,
    )


# --- calibrated forecast generation ------------------------------------------


def generate_calibrated_forecast(
    store: ResearchStore,
    game_id: str,
    calibration: CalibrationChoice,
    *,
    feature_cutoff_at: datetime,
    exponent: float = DEFAULT_EXPONENT,
    forecast_created_at: datetime | None = None,
) -> Forecast:
    """Generate a Forecast using league-average-regressed team strength and,
    if `calibration` selected it, Platt-scaled probabilities. Kalshi price
    is never read here, matching SUD-25's ordering rule.
    """
    game = store.load("game", game_id)
    if not isinstance(game, Game):
        raise InvalidScoringInputError(f"{game_id} is not a game record.")

    all_games = [g for g in store.load_all("game") if isinstance(g, Game)]

    home_strength = compute_calibrated_team_strength(
        all_games, game.home_team_id, game.season_year, feature_cutoff_at, exponent=exponent
    )
    away_strength = compute_calibrated_team_strength(
        all_games, game.away_team_id, game.season_year, feature_cutoff_at, exponent=exponent
    )

    raw_home_win = combine_win_probabilities_log5(home_strength.strength, away_strength.strength)
    home_field_applied = not bool(game.neutral_site)
    home_win_probability = apply_home_field(raw_home_win, neutral_site=bool(game.neutral_site))

    if calibration.calibration_version == CALIBRATION_VERSION_PLATT:
        home_win_probability = apply_platt_scaling(home_win_probability, calibration.platt_a, calibration.platt_b)

    min_games_played = min(home_strength.current_games_played, away_strength.current_games_played)
    uncertainty = 1 / ((min_games_played + 1) ** 0.5)

    created_at = forecast_created_at or datetime.now(timezone.utc)
    training_window_start = min(home_strength.training_window_start, away_strength.training_window_start)
    forecast_id = stable_record_id(
        "forecast", game.id, calibration.calibration_version, feature_cutoff_at.isoformat()
    )

    return Forecast(
        id=forecast_id,
        provider_ids={"model": MODEL_VERSION, "calibration": calibration.calibration_version},
        event_time=feature_cutoff_at,
        retrieved_at=created_at,
        source_snapshots=game.source_snapshots,
        game_id=game.id,
        model_version=MODEL_VERSION,
        feature_cutoff_at=feature_cutoff_at,
        forecast_created_at=created_at,
        home_win_probability=Decimal(str(round(home_win_probability, 6))),
        tie_probability=Decimal("0"),
        uncertainty=Decimal(str(round(min(uncertainty, 1.0), 6))),
        exponent=Decimal(str(exponent)),
        home_games_played=home_strength.current_games_played,
        away_games_played=away_strength.current_games_played,
        home_shrinkage_weight=Decimal(str(round(home_strength.shrinkage_weight, 6))),
        away_shrinkage_weight=Decimal(str(round(away_strength.shrinkage_weight, 6))),
        training_window_start=training_window_start,
        home_field_applied=home_field_applied,
        calibration_version=calibration.calibration_version,
        abstained=False,
    )
