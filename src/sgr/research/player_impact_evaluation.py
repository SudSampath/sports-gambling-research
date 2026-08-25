from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sgr.models import NFLSeasonType
from sgr.research.evaluation import FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF, GameSample, brier_score, log_loss
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
from sgr.research.storage import ResearchStore

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class MissingStarterSample:
    game_id: str
    team_id: str
    player_id: str
    primary_category: str
    impact_mean: float
    impact_stdev: float


@dataclass(frozen=True)
class ImpactEvaluationReport:
    season_years: tuple[int, ...]
    games_considered: int
    games_with_missing_starters: int
    samples: tuple[MissingStarterSample, ...]
    baseline_brier: float | None
    adjusted_brier: float | None
    baseline_log_loss: float | None
    adjusted_log_loss: float | None
    position_sample_counts: dict[str, int]


async def evaluate_player_impact_on_missing_starters(
    store: ResearchStore,
    season_years: list[int],
    *,
    exponent: float | None = None,
) -> ImpactEvaluationReport:
    """Walk-forward evaluation isolating the impact-estimation step itself.

    For each game, "usual starters" are determined from each team's own
    prior-game usage only (point-in-time correct). Whether a usual starter
    is actually missing from *this* game's real box score is then checked
    -- using the actual outcome to select interesting test cases is
    standard for evaluation (SUD-38 does the same), not leakage, because
    the missing-starter detection never feeds back into estimate_player_impact's
    own inputs, which only ever see games before the cutoff.

    This does not validate live pregame injury detection (SUD-60/61/91
    already established ESPN's injuries feed can't be replayed
    historically) -- it isolates a narrower, still meaningful question:
    given that a key player didn't play, does adjusting the forecast by
    their estimated impact improve accuracy over the plain baseline.
    """
    exponent = DEFAULT_EXPONENT if exponent is None else exponent
    all_games = sorted(
        (g for g in store.load_all("game") if isinstance(g, Game)),
        key=lambda g: g.kickoff_at,
    )
    all_statlines = [s for s in store.load_all("player_game_statline") if isinstance(s, PlayerGameStatline)]
    games_by_id = {g.id: g for g in all_games}

    test_games = [
        g
        for g in all_games
        if g.season_type == NFLSeasonType.REGULAR and g.completed and g.season_year in season_years
    ]

    samples: list[MissingStarterSample] = []
    baseline_outcomes: list[tuple[float, bool]] = []
    adjusted_outcomes: list[tuple[float, bool]] = []
    games_with_missing_starters = 0

    for game in test_games:
        if game.home_score == game.away_score:
            continue  # ties excluded, same as SUD-38
        cutoff = game.kickoff_at - timedelta(hours=FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF)

        try:
            baseline_forecast = generate_forecast(store, game.id, feature_cutoff_at=cutoff, exponent=exponent)
        except (InsufficientHistoryError, InvalidScoringInputError):
            continue
        baseline_probability = float(baseline_forecast.home_win_probability)

        home_usages = compute_player_usages(all_statlines, games_by_id, game.home_team_id, game.season_year, cutoff)
        away_usages = compute_player_usages(all_statlines, games_by_id, game.away_team_id, game.season_year, cutoff)
        starters = usual_starters(
            {game.home_team_id: home_usages, game.away_team_id: away_usages},
            MINIMUM_PRIOR_GAMES_TO_BE_A_STARTER,
        )

        this_game_statlines = [s for s in all_statlines if s.game_id == game.id]
        players_who_played = {s.player_id for s in this_game_statlines}

        adjustment = 0.0
        game_samples: list[MissingStarterSample] = []
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
                signed_impact = impact.mean_impact if team_id == game.home_team_id else -impact.mean_impact
                adjustment += signed_impact
                game_samples.append(
                    MissingStarterSample(
                        game_id=game.id, team_id=team_id, player_id=player_id,
                        primary_category=impact.primary_category,
                        impact_mean=impact.mean_impact, impact_stdev=impact.impact_stdev,
                    )
                )

        if not game_samples:
            continue

        games_with_missing_starters += 1
        samples.extend(game_samples)
        actual_home_win = game.home_score > game.away_score
        baseline_outcomes.append((baseline_probability, actual_home_win))
        adjusted_probability = min(max(baseline_probability - adjustment, 1e-6), 1 - 1e-6)
        adjusted_outcomes.append((adjusted_probability, actual_home_win))

    def _samples(outcomes: list[tuple[float, bool]]) -> list[GameSample]:
        # game_id/season_year/week/kickoff_at are placeholders here: brier_score
        # and log_loss only read predicted probability, actual outcome, is_tie,
        # and abstained, so identity fields are irrelevant to this aggregate.
        return [
            GameSample("evaluation-sample", 0, 0, EPOCH, p, y, False, False, None)
            for p, y in outcomes
        ]

    baseline_samples = _samples(baseline_outcomes)
    adjusted_samples = _samples(adjusted_outcomes)
    position_counts: dict[str, int] = {}
    for sample in samples:
        position_counts[sample.primary_category] = position_counts.get(sample.primary_category, 0) + 1

    return ImpactEvaluationReport(
        season_years=tuple(sorted(set(season_years))),
        games_considered=len(test_games),
        games_with_missing_starters=games_with_missing_starters,
        samples=tuple(samples),
        baseline_brier=brier_score(baseline_samples),
        adjusted_brier=brier_score(adjusted_samples),
        baseline_log_loss=log_loss(baseline_samples),
        adjusted_log_loss=log_loss(adjusted_samples),
        position_sample_counts=position_counts,
    )
