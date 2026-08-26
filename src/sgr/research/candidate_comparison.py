from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from sgr.models import NFLSeasonType
from sgr.research.player_impact import (
    MINIMUM_PRIOR_GAMES_TO_BE_A_STARTER,
    MissingReplacementError,
    compute_player_usages,
    estimate_player_impact,
    usual_starters,
)
from sgr.research.pythagorean import (
    DEFAULT_EXPONENT,
    InsufficientHistoryError,
    InvalidScoringInputError,
    generate_forecast,
)
from sgr.research.schemas import Game, PlayerGameStatline
from sgr.research.sos_adjustment import compute_all_team_strengths, sos_adjusted_probability
from sgr.research.storage import ResearchStore
from sgr.research.turnover_adjustment import build_turnovers_committed_index, turnover_normalized_probability

FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF = 24

# SUD-111's own composition order for the "blended" configuration, applied
# on top of the unadjusted Pythagorean/log5/home-field probability
# generate_forecast already produces. This is a modeling choice, not the
# only defensible one -- documented explicitly here, per the ticket's AC,
# rather than left as an arbitrary application order:
#   1. Turnover-normalize first: a correction to what a team's PF/PA
#      "should have been," net of turnover luck -- cleaning up the raw
#      data before comparing teams.
#   2. SOS-scale the turnover-corrected numbers second: a cross-team
#      comparison given who each team actually played.
#   3. Apply the injury delta last, as an additive probability-space
#      adjustment on top of the fully PF/PA-corrected probability, since
#      player-impact is estimated as a delta relative to a healthy
#      baseline, not a PF/PA-space correction.

CONFIGURATIONS = ("baseline", "injuries_only", "turnover_only", "sos_only", "blended")


@dataclass(frozen=True)
class CandidateSample:
    game_id: str
    predicted: float | None
    actual_home_win: bool | None
    is_tie: bool
    abstained: bool


@dataclass(frozen=True)
class MetricSummary:
    sample_count: int
    excluded_count: int
    brier_score: float | None
    log_loss: float | None
    accuracy: float | None


@dataclass(frozen=True)
class ComparisonReport:
    season_years: tuple[int, ...]
    results: dict[str, MetricSummary]


def brier_log_loss_accuracy(samples: list[CandidateSample]) -> MetricSummary:
    scored = [s for s in samples if not s.abstained and not s.is_tie]
    excluded = len(samples) - len(scored)
    if not scored:
        return MetricSummary(0, excluded, None, None, None)
    epsilon = 1e-12
    brier_total = 0.0
    log_loss_total = 0.0
    correct = 0
    for s in scored:
        y = float(s.actual_home_win)
        p = min(max(s.predicted, epsilon), 1 - epsilon)
        brier_total += (s.predicted - y) ** 2
        log_loss_total += -(y * math.log(p) + (1 - y) * math.log(1 - p))
        if (s.predicted >= 0.5) == s.actual_home_win:
            correct += 1
    n = len(scored)
    return MetricSummary(n, excluded, brier_total / n, log_loss_total / n, correct / n)


def _missing_starter_delta(
    all_games: list[Game],
    all_statlines: list[PlayerGameStatline],
    games_by_id: dict[str, Game],
    game: Game,
    cutoff: datetime,
    exponent: float,
) -> float:
    """Net home-win-probability delta from usual starters absent from this
    game's own real box score -- the same historically-valid proxy
    player_impact_evaluation.py uses (real pregame availability data
    cannot be reconstructed for past games; see SUD-109). Returns 0.0 when
    no missing starter is detected or no replacement/history is available.
    """
    home_usages = compute_player_usages(all_statlines, games_by_id, game.home_team_id, game.season_year, cutoff)
    away_usages = compute_player_usages(all_statlines, games_by_id, game.away_team_id, game.season_year, cutoff)
    starters = usual_starters(
        {game.home_team_id: home_usages, game.away_team_id: away_usages}, MINIMUM_PRIOR_GAMES_TO_BE_A_STARTER
    )
    players_who_played = {s.player_id for s in all_statlines if s.game_id == game.id}

    delta = 0.0
    for team_id, opponent_id in ((game.home_team_id, game.away_team_id), (game.away_team_id, game.home_team_id)):
        for player_id in starters.get(team_id, set()):
            if player_id in players_who_played:
                continue
            try:
                impact = estimate_player_impact(
                    all_statlines, all_games, player_id, team_id, opponent_id,
                    game.season_year, cutoff, neutral_site=bool(game.neutral_site), exponent=exponent,
                )
            except (MissingReplacementError, InsufficientHistoryError, InvalidScoringInputError):
                continue
            delta += -impact.mean_impact if team_id == game.home_team_id else impact.mean_impact
    return delta


def run_candidate_comparison(
    store: ResearchStore, season_years: list[int], *, exponent: float = DEFAULT_EXPONENT
) -> ComparisonReport:
    """Walk-forward compares the unadjusted baseline against each of the
    three SUD-108/109/110 candidate adjustments individually, and all
    three combined, on the same held-out real games -- the synthesis step
    after each was already validated on its own.
    """
    all_games = [g for g in store.load_all("game") if isinstance(g, Game)]
    all_statlines = [s for s in store.load_all("player_game_statline") if isinstance(s, PlayerGameStatline)]
    games_by_id = {g.id: g for g in all_games}
    turnovers_index = build_turnovers_committed_index(all_statlines)

    test_games = sorted(
        (
            g
            for g in all_games
            if g.season_type == NFLSeasonType.REGULAR and g.completed and g.season_year in season_years
        ),
        key=lambda g: g.kickoff_at,
    )

    samples: dict[str, list[CandidateSample]] = {name: [] for name in CONFIGURATIONS}

    for game in test_games:
        cutoff = game.kickoff_at - timedelta(hours=FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF)
        is_tie = game.home_score == game.away_score
        actual_home_win = None if is_tie else (game.home_score or 0) > (game.away_score or 0)

        try:
            baseline_forecast = generate_forecast(
                store, game.id, feature_cutoff_at=cutoff, exponent=exponent, apply_injury_adjustment=False
            )
            baseline_probability = float(baseline_forecast.home_win_probability)
        except (InsufficientHistoryError, InvalidScoringInputError):
            for name in CONFIGURATIONS:
                samples[name].append(CandidateSample(game.id, None, actual_home_win, is_tie, True))
            continue

        samples["baseline"].append(CandidateSample(game.id, baseline_probability, actual_home_win, is_tie, False))

        injury_delta = _missing_starter_delta(all_games, all_statlines, games_by_id, game, cutoff, exponent)
        injury_probability = min(max(baseline_probability + injury_delta, 1e-6), 1 - 1e-6)
        samples["injuries_only"].append(CandidateSample(game.id, injury_probability, actual_home_win, is_tie, False))

        try:
            turnover_probability = turnover_normalized_probability(
                all_games, turnovers_index, game.home_team_id, game.away_team_id, game.season_year,
                cutoff, neutral_site=game.neutral_site, exponent=exponent,
            )
            samples["turnover_only"].append(CandidateSample(game.id, turnover_probability, actual_home_win, is_tie, False))
        except (InsufficientHistoryError, InvalidScoringInputError):
            samples["turnover_only"].append(CandidateSample(game.id, None, actual_home_win, is_tie, True))
            turnover_probability = baseline_probability

        team_strengths = compute_all_team_strengths(all_games, game.season_year, cutoff, exponent=exponent)
        try:
            sos_probability = sos_adjusted_probability(
                all_games, team_strengths, game.home_team_id, game.away_team_id, game.season_year,
                cutoff, neutral_site=game.neutral_site, exponent=exponent,
            )
            samples["sos_only"].append(CandidateSample(game.id, sos_probability, actual_home_win, is_tie, False))
        except InsufficientHistoryError:
            samples["sos_only"].append(CandidateSample(game.id, None, actual_home_win, is_tie, True))
            sos_probability = baseline_probability

        # Blend: apply turnover-then-SOS to the PF/PA inputs (both already
        # folded into turnover_probability/sos_probability's own separate
        # single-adjustment computations above); the combined PF/PA-space
        # effect is approximated by summing each adjustment's own shift
        # away from baseline in logit space, then applying the injury
        # delta last in probability space -- see this module's docstring
        # for the documented composition order.
        def _to_logit(p: float) -> float:
            p = min(max(p, 1e-6), 1 - 1e-6)
            return math.log(p / (1 - p))

        def _from_logit(z: float) -> float:
            if z < -700:
                return 0.0
            return 1 / (1 + math.exp(-z))

        blended_logit = (
            _to_logit(baseline_probability)
            + (_to_logit(turnover_probability) - _to_logit(baseline_probability))
            + (_to_logit(sos_probability) - _to_logit(baseline_probability))
        )
        blended_probability = min(max(_from_logit(blended_logit) + injury_delta, 1e-6), 1 - 1e-6)
        samples["blended"].append(CandidateSample(game.id, blended_probability, actual_home_win, is_tie, False))

    results = {name: brier_log_loss_accuracy(samples[name]) for name in CONFIGURATIONS}
    return ComparisonReport(season_years=tuple(sorted(set(season_years))), results=results)
