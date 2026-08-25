from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from sgr.models import NFLSeasonType
from sgr.research.margin import (
    DEFAULT_HOME_FIELD_MARGIN_POINTS,
    FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF,
    compute_expected_margin,
)
from sgr.research.pythagorean import DEFAULT_EXPONENT, InsufficientHistoryError, InvalidScoringInputError
from sgr.research.schemas import Game
from sgr.research.storage import ResearchStore

MODEL_VERSION = "margin-v1"


@dataclass(frozen=True)
class MarginSample:
    game_id: str
    season_year: int
    week: int
    kickoff_at: datetime
    predicted_margin: float | None
    actual_margin: float | None
    abstained: bool
    abstain_reason: str | None


@dataclass(frozen=True)
class MarginMetricSet:
    sample_count: int
    excluded_count: int
    mean_absolute_error: float | None
    root_mean_squared_error: float | None
    residual_variance: float | None
    residual_stdev: float | None


@dataclass(frozen=True)
class MarginWalkForwardReport:
    model_version: str
    exponent: float
    home_field_margin_points: float
    dataset_checksum: str
    season_years: tuple[int, ...]
    overall: MarginMetricSet
    baseline_overall: dict[str, MarginMetricSet]
    samples: tuple[MarginSample, ...]


def mean_absolute_error(samples: list[MarginSample]) -> float | None:
    scored = [s for s in samples if not s.abstained]
    if not scored:
        return None
    return sum(abs(s.predicted_margin - s.actual_margin) for s in scored) / len(scored)


def root_mean_squared_error(samples: list[MarginSample]) -> float | None:
    scored = [s for s in samples if not s.abstained]
    if not scored:
        return None
    return math.sqrt(sum((s.predicted_margin - s.actual_margin) ** 2 for s in scored) / len(scored))


def residual_variance(samples: list[MarginSample]) -> float | None:
    """Variance of (actual - predicted) across scored samples -- the real,
    measured residual spread a margin confidence interval is built from, not
    an invented number (SUD-105's own AC)."""
    scored = [s for s in samples if not s.abstained]
    if len(scored) < 2:
        return None
    residuals = [s.actual_margin - s.predicted_margin for s in scored]
    mean_residual = sum(residuals) / len(residuals)
    return sum((r - mean_residual) ** 2 for r in residuals) / (len(residuals) - 1)


def _build_metric_set(samples: list[MarginSample]) -> MarginMetricSet:
    excluded = [s for s in samples if s.abstained]
    variance = residual_variance(samples)
    return MarginMetricSet(
        sample_count=len(samples) - len(excluded),
        excluded_count=len(excluded),
        mean_absolute_error=mean_absolute_error(samples),
        root_mean_squared_error=root_mean_squared_error(samples),
        residual_variance=variance,
        residual_stdev=math.sqrt(variance) if variance is not None else None,
    )


def _dataset_checksum(games: list[Game]) -> str:
    digest = hashlib.sha256()
    for game in sorted(games, key=lambda g: g.id):
        digest.update(game.id.encode())
        digest.update(game.source_snapshots[0].sha256.encode())
    return digest.hexdigest()


def home_field_only_margin_baseline(neutral_site: bool | None, home_field_margin_points: float) -> float:
    """Naive baseline: predict a constant home-field-only margin (or 0 at a
    neutral site), ignoring team identity entirely -- the margin-space
    analogue of evaluation.py's home_field_only_probability."""
    return 0.0 if neutral_site else home_field_margin_points


def always_zero_margin_baseline(neutral_site: bool | None) -> float:
    """Naive baseline: always predict a tie (0 margin)."""
    return 0.0


def run_margin_walk_forward_evaluation(
    store: ResearchStore,
    season_years: list[int],
    *,
    exponent: float = DEFAULT_EXPONENT,
    home_field_margin_points: float = DEFAULT_HOME_FIELD_MARGIN_POINTS,
    include_baselines: bool = True,
) -> MarginWalkForwardReport:
    """Chronologically score every regular-season game in season_years by
    expected margin, walk-forward, mirroring evaluation.py's win-probability
    harness: each test prediction is generated with a feature cutoff
    strictly before its own game, inheriting compute_team_strength's
    point-in-time boundary with no separate leakage control of its own.
    """
    all_games = [g for g in store.load_all("game") if isinstance(g, Game)]
    test_games = sorted(
        (
            g
            for g in all_games
            if g.season_type == NFLSeasonType.REGULAR
            and g.completed
            and g.season_year in season_years
        ),
        key=lambda g: g.kickoff_at,
    )

    samples: list[MarginSample] = []
    baseline_samples: dict[str, list[MarginSample]] = {
        "home_field_only": [],
        "always_zero": [],
    }

    for game in test_games:
        cutoff = game.kickoff_at - timedelta(hours=FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF)
        actual_margin = float((game.home_score or 0) - (game.away_score or 0))

        try:
            estimate = compute_expected_margin(
                store, game.id, feature_cutoff_at=cutoff, exponent=exponent,
                home_field_margin_points=home_field_margin_points,
            )
            samples.append(
                MarginSample(
                    game.id, game.season_year, game.week, game.kickoff_at,
                    estimate.expected_margin, actual_margin, False, None,
                )
            )
        except (InsufficientHistoryError, InvalidScoringInputError) as error:
            samples.append(
                MarginSample(
                    game.id, game.season_year, game.week, game.kickoff_at,
                    None, actual_margin, True, type(error).__name__,
                )
            )

        if include_baselines:
            baseline_samples["home_field_only"].append(
                MarginSample(
                    game.id, game.season_year, game.week, game.kickoff_at,
                    home_field_only_margin_baseline(game.neutral_site, home_field_margin_points),
                    actual_margin, False, None,
                )
            )
            baseline_samples["always_zero"].append(
                MarginSample(
                    game.id, game.season_year, game.week, game.kickoff_at,
                    always_zero_margin_baseline(game.neutral_site), actual_margin, False, None,
                )
            )

    baseline_overall = (
        {name: _build_metric_set(bsamples) for name, bsamples in baseline_samples.items()}
        if include_baselines
        else {}
    )

    return MarginWalkForwardReport(
        model_version=MODEL_VERSION,
        exponent=exponent,
        home_field_margin_points=home_field_margin_points,
        dataset_checksum=_dataset_checksum(test_games),
        season_years=tuple(sorted(set(season_years))),
        overall=_build_metric_set(samples),
        baseline_overall=baseline_overall,
        samples=tuple(samples),
    )
