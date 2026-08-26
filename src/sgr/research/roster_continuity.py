from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Iterable

from sgr.connectors.nflverse import NflverseConnector, NflverseCsvSnapshot
from sgr.models import NFLSeasonType
from sgr.research.pythagorean import (
    DEFAULT_EXPONENT,
    SHRINKAGE_PSEUDOGAMES,
    TeamStrength,
    apply_home_field,
    combine_win_probabilities_log5,
    compute_team_strength,
    pythagorean_win_pct,
    shrink_toward_prior,
)
from sgr.research.schemas import Game, RosterContinuitySignal, Team, stable_record_id
from sgr.research.storage import ResearchStore
from sgr.research.win_totals import (
    CONFIDENCE_BAND_Z,
    TeamWinTotalProjection,
    _actual_win_value,
)

MODEL_VERSION = "pythagorean-roster-continuity-v1"

# Fitted with no intercept on 2014-2024 team-season transitions. Each target
# is next-season log scoring-rate change; each feature is the prior season's
# distance from league average multiplied by non-retention. The held-out 2025
# season is not used to choose these values. Refit through the public helper
# below as more completed seasons become available.
OFFENSE_REVERSION_COEFFICIENT = 0.877724
DEFENSE_REVERSION_COEFFICIENT = 1.183390

RETAINED_ROSTER_STATUSES = frozenset({"ACT", "INA", "RES"})
TEAM_ALIASES = {
    "AZ": "ARI",
    "JAC": "JAX",
    "LA": "LAR",
    "OAK": "LV",
    "SD": "LAC",
    "STL": "LAR",
    "WAS": "WSH",
}


class RosterContinuityError(RuntimeError):
    """Base error for the roster-continuity research variant."""


class ContinuitySignalUnavailableError(RosterContinuityError):
    """Raised when a point-in-time eligible signal is unavailable."""


class ContinuityCalibrationError(RosterContinuityError):
    """Raised when calibration data is invalid or crosses the holdout boundary."""


@dataclass(frozen=True)
class ContinuityCalibrationSample:
    season_year: int
    retention: float
    prior_quality_log_ratio: float
    next_season_log_rate_change: float


@dataclass(frozen=True)
class ContinuityCoefficients:
    offense: float
    defense: float
    sample_count: int


@dataclass(frozen=True)
class RosterContinuityProjectionReport:
    season_year: int
    as_of: datetime
    model_version: str
    offense_reversion_coefficient: float
    defense_reversion_coefficient: float
    projections: tuple[RosterContinuityTeamWinTotalProjection, ...]


@dataclass(frozen=True)
class RosterContinuityTeamWinTotalProjection(TeamWinTotalProjection):
    offense_retention: float
    defense_retention: float
    continuity_feature_cutoff_at: datetime


def normalize_team_abbreviation(value: str) -> str:
    abbreviation = value.strip().upper()
    return TEAM_ALIASES.get(abbreviation, abbreviation)


def fit_continuity_coefficient(
    training_samples: Iterable[ContinuityCalibrationSample],
    *,
    heldout_season_years: Iterable[int] = (),
) -> float:
    """Fit a no-intercept reversion coefficient without test-fold leakage."""

    samples = tuple(training_samples)
    heldout = set(heldout_season_years)
    overlap = sorted({sample.season_year for sample in samples} & heldout)
    if overlap:
        raise ContinuityCalibrationError(
            f"Calibration samples overlap held-out seasons: {overlap}."
        )
    if len(samples) < 2:
        raise ContinuityCalibrationError("At least two training samples are required.")
    features = [-(1 - sample.retention) * sample.prior_quality_log_ratio for sample in samples]
    denominator = sum(value * value for value in features)
    if denominator == 0:
        raise ContinuityCalibrationError("Calibration features have zero variance.")
    return sum(x * sample.next_season_log_rate_change for x, sample in zip(features, samples)) / denominator


def calibrate_continuity_coefficients(
    all_games: list[Game],
    signals: Iterable[RosterContinuitySignal],
    training_season_years: Iterable[int],
    *,
    heldout_season_years: Iterable[int] = (),
) -> ContinuityCoefficients:
    """Construct team-season samples and fit offense/defense coefficients.

    For offense, the target is log(next PF/game / prior PF/game). For
    defense it is log(prior PA/game / next PA/game), so improvement remains
    positive in both performance spaces. The predictor in both cases is the
    prior log distance from league average multiplied by non-retention.
    """

    training_years = set(training_season_years)
    heldout_years = set(heldout_season_years)
    overlap = sorted(training_years & heldout_years)
    if overlap:
        raise ContinuityCalibrationError(
            f"Training and held-out seasons overlap: {overlap}."
        )
    signal_tuple = tuple(
        signal for signal in signals if signal.season_year in training_years
    )
    offense_samples: list[ContinuityCalibrationSample] = []
    defense_samples: list[ContinuityCalibrationSample] = []
    for signal in signal_tuple:
        prior_team_games = [
            game
            for game in all_games
            if game.completed
            and game.season_type == NFLSeasonType.REGULAR
            and game.season_year == signal.prior_season_year
            and signal.team_id in {game.home_team_id, game.away_team_id}
        ]
        next_team_games = [
            game
            for game in all_games
            if game.completed
            and game.season_type == NFLSeasonType.REGULAR
            and game.season_year == signal.season_year
            and signal.team_id in {game.home_team_id, game.away_team_id}
        ]
        if not prior_team_games or not next_team_games:
            continue
        prior_for = sum(
            (game.home_score if game.home_team_id == signal.team_id else game.away_score) or 0
            for game in prior_team_games
        ) / len(prior_team_games)
        prior_against = sum(
            (game.away_score if game.home_team_id == signal.team_id else game.home_score) or 0
            for game in prior_team_games
        ) / len(prior_team_games)
        next_for = sum(
            (game.home_score if game.home_team_id == signal.team_id else game.away_score) or 0
            for game in next_team_games
        ) / len(next_team_games)
        next_against = sum(
            (game.away_score if game.home_team_id == signal.team_id else game.home_score) or 0
            for game in next_team_games
        ) / len(next_team_games)
        league_average = _prior_league_points_per_team_game(
            all_games,
            signal.season_year,
            max(game.kickoff_at for game in next_team_games) + timedelta(seconds=1),
        )
        offense_samples.append(
            ContinuityCalibrationSample(
                signal.season_year,
                float(signal.offense_retention),
                math.log(prior_for / league_average),
                math.log(next_for / prior_for),
            )
        )
        defense_samples.append(
            ContinuityCalibrationSample(
                signal.season_year,
                float(signal.defense_retention),
                math.log(league_average / prior_against),
                math.log(prior_against / next_against),
            )
        )
    if len(offense_samples) != len(defense_samples):
        raise ContinuityCalibrationError("Offense and defense calibration samples do not align.")
    return ContinuityCoefficients(
        offense=fit_continuity_coefficient(
            offense_samples, heldout_season_years=heldout_years
        ),
        defense=fit_continuity_coefficient(
            defense_samples, heldout_season_years=heldout_years
        ),
        sample_count=len(offense_samples),
    )


def _as_int(value: str | None) -> int:
    try:
        return int(float(value or "0"))
    except ValueError as error:
        raise RosterContinuityError(f"Snap count must be numeric, got {value!r}.") from error


def build_roster_continuity_signals(
    snap_snapshot: NflverseCsvSnapshot,
    roster_snapshot: NflverseCsvSnapshot,
    teams: Iterable[Team],
    season_year: int,
    *,
    feature_cutoff_at: datetime,
    roster_source_kind: str,
) -> tuple[RosterContinuitySignal, ...]:
    """Normalize nflverse rows into team-level, snap-weighted retention.

    Players count as retained only when they have the same franchise and a
    roster status that can actually participate (active, inactive, or
    reserve). Practice-squad/development, cut, retired, and exempt rows are
    intentionally excluded.
    """

    if feature_cutoff_at.tzinfo is None or feature_cutoff_at.utcoffset() is None:
        raise ValueError("feature_cutoff_at must be timezone-aware.")
    if roster_source_kind not in {"historical_week1", "current"}:
        raise ValueError("roster_source_kind must be historical_week1 or current.")

    prior_season = season_year - 1
    eligible_players: dict[str, set[str]] = {}
    for row in roster_snapshot.rows:
        if _as_int(row.get("season")) != season_year:
            continue
        if roster_source_kind == "historical_week1":
            if row.get("game_type", "").upper() != "REG" or _as_int(row.get("week")) != 1:
                continue
        if row.get("status", "").strip().upper() not in RETAINED_ROSTER_STATUSES:
            continue
        player_id = row.get("pfr_id", "").strip()
        team = normalize_team_abbreviation(row.get("team", ""))
        if player_id and team:
            eligible_players.setdefault(team, set()).add(player_id)

    snap_totals: dict[str, dict[str, dict[str, int]]] = {}
    for row in snap_snapshot.rows:
        if _as_int(row.get("season")) != prior_season or row.get("game_type", "").upper() != "REG":
            continue
        player_id = row.get("pfr_player_id", "").strip()
        team = normalize_team_abbreviation(row.get("team", ""))
        if not player_id or not team:
            continue
        player_totals = snap_totals.setdefault(team, {}).setdefault(
            player_id, {"offense": 0, "defense": 0}
        )
        player_totals["offense"] += _as_int(row.get("offense_snaps"))
        player_totals["defense"] += _as_int(row.get("defense_snaps"))

    teams_by_abbreviation = {normalize_team_abbreviation(team.abbreviation): team for team in teams}
    retrieved_at = max(snap_snapshot.source.retrieved_at, roster_snapshot.source.retrieved_at)
    signals: list[RosterContinuitySignal] = []
    for abbreviation, players in sorted(snap_totals.items()):
        canonical_team = teams_by_abbreviation.get(abbreviation)
        if canonical_team is None:
            continue
        offense_total = sum(player["offense"] for player in players.values())
        defense_total = sum(player["defense"] for player in players.values())
        if offense_total <= 0 or defense_total <= 0:
            continue
        retained = eligible_players.get(abbreviation, set())
        offense_retained = sum(
            player["offense"] for player_id, player in players.items() if player_id in retained
        )
        defense_retained = sum(
            player["defense"] for player_id, player in players.items() if player_id in retained
        )
        signals.append(
            RosterContinuitySignal(
                id=stable_record_id(
                    "roster_continuity_signal",
                    abbreviation,
                    season_year,
                    feature_cutoff_at.isoformat(),
                ),
                provider_ids={"nflverse": f"{abbreviation}:{season_year}"},
                event_time=feature_cutoff_at,
                retrieved_at=retrieved_at,
                source_snapshots=(snap_snapshot.source, roster_snapshot.source),
                team_id=canonical_team.id,
                season_year=season_year,
                prior_season_year=prior_season,
                feature_cutoff_at=feature_cutoff_at,
                offense_snaps_total=offense_total,
                offense_snaps_retained=offense_retained,
                defense_snaps_total=defense_total,
                defense_snaps_retained=defense_retained,
                offense_retention=Decimal(offense_retained) / Decimal(offense_total),
                defense_retention=Decimal(defense_retained) / Decimal(defense_total),
                roster_source_kind=roster_source_kind,
            )
        )
    if not signals:
        raise ContinuitySignalUnavailableError(
            f"No roster-continuity signals could be built for season {season_year}."
        )
    return tuple(signals)


async def ingest_roster_continuity(
    connector: NflverseConnector,
    store: ResearchStore,
    season_year: int,
    *,
    feature_cutoff_at: datetime,
    historical_week1: bool,
    refresh: bool = False,
) -> tuple[RosterContinuitySignal, ...]:
    snap_snapshot = await connector.snap_counts(season_year - 1, refresh=refresh)
    roster_snapshot = (
        await connector.weekly_rosters(season_year, refresh=refresh)
        if historical_week1
        else await connector.current_rosters(season_year, refresh=refresh)
    )
    signals = build_roster_continuity_signals(
        snap_snapshot,
        roster_snapshot,
        (team for team in store.load_all("team") if isinstance(team, Team)),
        season_year,
        feature_cutoff_at=(
            feature_cutoff_at
            if historical_week1
            else max(snap_snapshot.source.retrieved_at, roster_snapshot.source.retrieved_at)
        ),
        roster_source_kind="historical_week1" if historical_week1 else "current",
    )
    store.write(signals)
    return signals


def select_roster_continuity_signal(
    signals: Iterable[RosterContinuitySignal],
    team_id: str,
    season_year: int,
    feature_cutoff_at: datetime,
) -> RosterContinuitySignal:
    candidates = [
        signal
        for signal in signals
        if signal.team_id == team_id
        and signal.season_year == season_year
        and signal.feature_cutoff_at <= feature_cutoff_at
    ]
    if not candidates:
        raise ContinuitySignalUnavailableError(
            f"No roster-continuity signal for team {team_id}, season {season_year}, "
            f"at or before {feature_cutoff_at.isoformat()}."
        )
    return max(candidates, key=lambda signal: signal.feature_cutoff_at)


def _prior_league_points_per_team_game(
    all_games: list[Game], season_year: int, feature_cutoff_at: datetime
) -> float:
    prior_games = [
        game
        for game in all_games
        if game.season_type == NFLSeasonType.REGULAR
        and game.season_year == season_year - 1
        and game.completed
        and game.kickoff_at < feature_cutoff_at
    ]
    if not prior_games:
        raise ContinuitySignalUnavailableError(
            f"No completed {season_year - 1} games are available for league-average scoring."
        )
    return sum((game.home_score or 0) + (game.away_score or 0) for game in prior_games) / (2 * len(prior_games))


def compute_team_strength_with_roster_continuity(
    all_games: list[Game],
    signals: Iterable[RosterContinuitySignal],
    team_id: str,
    season_year: int,
    feature_cutoff_at: datetime,
    *,
    exponent: float = DEFAULT_EXPONENT,
    shrinkage_pseudogames: int = SHRINKAGE_PSEUDOGAMES,
    offense_coefficient: float = OFFENSE_REVERSION_COEFFICIENT,
    defense_coefficient: float = DEFENSE_REVERSION_COEFFICIENT,
) -> TeamStrength:
    """Apply continuity only to the prior-season scoring rates.

    Full retention is an exact no-op. With turnover, unusually strong or
    weak prior rates are pulled toward the prior league average. The normal
    current/prior shrinkage then makes the effect fade as current games accrue.
    """

    baseline = compute_team_strength(
        all_games,
        team_id,
        season_year,
        feature_cutoff_at,
        exponent=exponent,
        shrinkage_pseudogames=shrinkage_pseudogames,
    )
    if not baseline.prior_games_played:
        return baseline
    signal = select_roster_continuity_signal(signals, team_id, season_year, feature_cutoff_at)
    league_average = _prior_league_points_per_team_game(all_games, season_year, feature_cutoff_at)
    prior_for = baseline.prior_points_for / baseline.prior_games_played
    prior_against = baseline.prior_points_against / baseline.prior_games_played

    offense_quality = math.log(prior_for / league_average)
    defense_quality = math.log(league_average / prior_against)
    adjusted_prior_for = prior_for * math.exp(
        -offense_coefficient * (1 - float(signal.offense_retention)) * offense_quality
    )
    adjusted_prior_against = prior_against * math.exp(
        defense_coefficient * (1 - float(signal.defense_retention)) * defense_quality
    )
    current_for = (
        baseline.current_points_for / baseline.current_games_played
        if baseline.current_games_played
        else None
    )
    current_against = (
        baseline.current_points_against / baseline.current_games_played
        if baseline.current_games_played
        else None
    )
    blended_for, weight = shrink_toward_prior(
        current_for,
        baseline.current_games_played,
        adjusted_prior_for,
        pseudogames=shrinkage_pseudogames,
    )
    blended_against, _ = shrink_toward_prior(
        current_against,
        baseline.current_games_played,
        adjusted_prior_against,
        pseudogames=shrinkage_pseudogames,
    )
    return TeamStrength(
        team_id=baseline.team_id,
        season_year=baseline.season_year,
        feature_cutoff_at=baseline.feature_cutoff_at,
        exponent=baseline.exponent,
        current_games_played=baseline.current_games_played,
        current_points_for=baseline.current_points_for,
        current_points_against=baseline.current_points_against,
        prior_season_year=baseline.prior_season_year,
        prior_games_played=baseline.prior_games_played,
        prior_points_for=baseline.prior_points_for,
        prior_points_against=baseline.prior_points_against,
        shrinkage_weight=weight,
        strength=pythagorean_win_pct(blended_for, blended_against, exponent),
        training_window_start=baseline.training_window_start,
    )


def roster_continuity_probability(
    all_games: list[Game],
    signals: Iterable[RosterContinuitySignal],
    home_team_id: str,
    away_team_id: str,
    season_year: int,
    feature_cutoff_at: datetime,
    *,
    neutral_site: bool | None,
    exponent: float = DEFAULT_EXPONENT,
) -> float:
    signal_tuple = tuple(signals)
    home = compute_team_strength_with_roster_continuity(
        all_games, signal_tuple, home_team_id, season_year, feature_cutoff_at, exponent=exponent
    )
    away = compute_team_strength_with_roster_continuity(
        all_games, signal_tuple, away_team_id, season_year, feature_cutoff_at, exponent=exponent
    )
    raw = combine_win_probabilities_log5(home.strength, away.strength)
    return apply_home_field(raw, neutral_site=bool(neutral_site))


def project_season_win_totals_with_roster_continuity(
    store: ResearchStore,
    season_year: int,
    *,
    as_of: datetime,
    exponent: float = DEFAULT_EXPONENT,
) -> RosterContinuityProjectionReport:
    all_games = [game for game in store.load_all("game") if isinstance(game, Game)]
    teams = {team.id: team for team in store.load_all("team") if isinstance(team, Team)}
    signals = [
        signal
        for signal in store.load_all("roster_continuity_signal")
        if isinstance(signal, RosterContinuitySignal)
    ]
    season_games = [
        game
        for game in all_games
        if game.season_type == NFLSeasonType.REGULAR and game.season_year == season_year
    ]
    team_ids = sorted({game.home_team_id for game in season_games} | {game.away_team_id for game in season_games})
    projections: list[RosterContinuityTeamWinTotalProjection] = []
    for team_id in team_ids:
        team_games = [
            game for game in season_games if team_id in {game.home_team_id, game.away_team_id}
        ]
        completed = [game for game in team_games if game.completed and game.kickoff_at < as_of]
        remaining = [game for game in team_games if not (game.completed and game.kickoff_at < as_of)]
        wins_so_far = sum(_actual_win_value(game, team_id) for game in completed)
        expected_additional = 0.0
        variance = 0.0
        for game in remaining:
            home_probability = roster_continuity_probability(
                all_games,
                signals,
                game.home_team_id,
                game.away_team_id,
                season_year,
                as_of,
                neutral_site=game.neutral_site,
                exponent=exponent,
            )
            probability = home_probability if game.home_team_id == team_id else 1 - home_probability
            expected_additional += probability
            variance += probability * (1 - probability)
        expected_total = wins_so_far + expected_additional
        standard_deviation = math.sqrt(variance)
        team = teams.get(team_id)
        continuity_signal = select_roster_continuity_signal(
            signals, team_id, season_year, as_of
        )
        projections.append(
            RosterContinuityTeamWinTotalProjection(
                team_id=team_id,
                abbreviation=team.abbreviation if team else team_id,
                season_year=season_year,
                games_played=len(completed),
                wins_so_far=wins_so_far,
                games_remaining=len(remaining),
                expected_additional_wins=expected_additional,
                expected_total_wins=expected_total,
                remaining_win_variance=variance,
                confidence_low=max(wins_so_far, expected_total - CONFIDENCE_BAND_Z * standard_deviation),
                confidence_high=min(
                    wins_so_far + len(remaining),
                    expected_total + CONFIDENCE_BAND_Z * standard_deviation,
                ),
                offense_retention=float(continuity_signal.offense_retention),
                defense_retention=float(continuity_signal.defense_retention),
                continuity_feature_cutoff_at=continuity_signal.feature_cutoff_at,
            )
        )
    projections.sort(key=lambda projection: projection.expected_total_wins, reverse=True)
    return RosterContinuityProjectionReport(
        season_year=season_year,
        as_of=as_of,
        model_version=MODEL_VERSION,
        offense_reversion_coefficient=OFFENSE_REVERSION_COEFFICIENT,
        defense_reversion_coefficient=DEFENSE_REVERSION_COEFFICIENT,
        projections=tuple(projections),
    )
