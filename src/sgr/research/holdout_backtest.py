from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime

from sgr.research.evaluation import GameSample, brier_score, log_loss, run_walk_forward_evaluation
from sgr.research.pythagorean import DEFAULT_EXPONENT
from sgr.research.schemas import Game, Team
from sgr.research.storage import ResearchStore

DEFAULT_HOLDOUT_FRACTION = 0.6
DEFAULT_HOLDOUT_SEED = 20260824


def select_holdout_game_ids(game_ids: list[str], *, holdout_fraction: float, seed: int) -> set[str]:
    """A seeded, reproducible random sample of game_ids.

    Sorted before shuffling so the result depends only on the seed and the
    (order-independent) set of game_ids, not on whatever order the caller
    happened to pass them in.
    """
    if not 0 < holdout_fraction <= 1:
        raise ValueError("holdout_fraction must be in (0, 1].")
    ordered = sorted(set(game_ids))
    rng = random.Random(seed)
    rng.shuffle(ordered)
    cutoff = round(len(ordered) * holdout_fraction)
    return set(ordered[:cutoff])


@dataclass(frozen=True)
class ScorecardRow:
    game_id: str
    season_year: int
    week: int
    kickoff_at: datetime
    home_team: str
    away_team: str
    predicted_home_win_probability: float
    actual_home_win: bool
    correct: bool


@dataclass(frozen=True)
class HoldoutBacktestReport:
    """Note: this project's Pythagorean baseline has no free parameters fit
    per run (a fixed, already-validated exponent -- SUD-25/38), so "holdout"
    here does not mean "excluded from what the model can see" the way it
    would for a fitted ML model. Every game's forecast already only uses
    strictly-prior real data via the existing point-in-time cutoff,
    regardless of which side of the split it falls on. The split controls
    which games get a scorecard row printed, not what information the
    model is allowed to use.
    """

    season_years: tuple[int, ...]
    holdout_fraction: float
    seed: int
    rows: tuple[ScorecardRow, ...]
    holdout_game_count: int
    holdout_brier: float | None
    holdout_log_loss: float | None
    holdout_accuracy: float | None
    full_game_count: int
    full_brier: float | None
    full_log_loss: float | None
    full_accuracy: float | None


def _accuracy(samples: list[GameSample]) -> float | None:
    scored = [s for s in samples if not s.abstained and not s.is_tie]
    if not scored:
        return None
    correct = sum(1 for s in scored if (s.predicted_home_win_probability >= 0.5) == s.actual_home_win)
    return correct / len(scored)


def run_holdout_backtest(
    store: ResearchStore,
    season_years: list[int],
    *,
    holdout_fraction: float = DEFAULT_HOLDOUT_FRACTION,
    seed: int = DEFAULT_HOLDOUT_SEED,
    exponent: float = DEFAULT_EXPONENT,
) -> HoldoutBacktestReport:
    """Predicted-vs-actual scorecard for a reproducible random subset of
    real games, alongside the same metrics over the full game set for
    comparison. Reuses SUD-38's walk-forward evaluation for the actual
    point-in-time forecasting and full-set metrics rather than
    re-implementing forecast generation -- this ticket only adds the
    holdout selection and the readable, named-team scorecard on top.

    Ties and abstained games are excluded from both the scorecard and the
    metrics, same as SUD-38 -- there is no well-defined "correct side of
    50%" for a tie, and an abstained game has no prediction to score.
    """
    full_report = run_walk_forward_evaluation(store, season_years, exponent=exponent, include_baselines=False)
    scored_samples = [s for s in full_report.samples if not s.abstained and not s.is_tie]

    holdout_ids = select_holdout_game_ids(
        [s.game_id for s in scored_samples], holdout_fraction=holdout_fraction, seed=seed
    )
    holdout_samples = [s for s in scored_samples if s.game_id in holdout_ids]

    teams = {t.id: t for t in store.load_all("team") if isinstance(t, Team)}
    games_by_id = {g.id: g for g in store.load_all("game") if isinstance(g, Game)}

    rows = []
    for sample in sorted(holdout_samples, key=lambda s: s.kickoff_at):
        game = games_by_id.get(sample.game_id)
        home = teams.get(game.home_team_id) if game else None
        away = teams.get(game.away_team_id) if game else None
        predicted = sample.predicted_home_win_probability
        rows.append(
            ScorecardRow(
                game_id=sample.game_id,
                season_year=sample.season_year,
                week=sample.week,
                kickoff_at=sample.kickoff_at,
                home_team=home.abbreviation if home else (game.home_team_id if game else "?"),
                away_team=away.abbreviation if away else (game.away_team_id if game else "?"),
                predicted_home_win_probability=predicted,
                actual_home_win=sample.actual_home_win,
                correct=(predicted >= 0.5) == sample.actual_home_win,
            )
        )

    return HoldoutBacktestReport(
        season_years=full_report.season_years,
        holdout_fraction=holdout_fraction,
        seed=seed,
        rows=tuple(rows),
        holdout_game_count=len(rows),
        holdout_brier=brier_score(holdout_samples),
        holdout_log_loss=log_loss(holdout_samples),
        holdout_accuracy=_accuracy(holdout_samples),
        full_game_count=len(scored_samples),
        full_brier=full_report.overall.brier_score,
        full_log_loss=full_report.overall.log_loss,
        full_accuracy=_accuracy(full_report.samples),
    )
