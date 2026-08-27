from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pytest_bdd import given, scenarios, then, when

from sgr.research.context_effects import (
    combined_adjusted_probability,
    dome_adjusted_probability,
    rest_adjusted_probability,
    rest_days_differential,
)
from sgr.research.context_effects_evaluation import run_context_effects_evaluation
from sgr.research.evaluation import TrainTestLeakageError
from sgr.research.schemas import GameContext, RawSnapshotRef, stable_record_id
from sgr.research.storage import ResearchStore

scenarios("../features/context_effects.feature")

RETRIEVED_AT = datetime(2026, 8, 27, tzinfo=timezone.utc)
_SOURCE = RawSnapshotRef(
    provider="nflverse",
    path=".cache/nflverse/games/games.csv",
    source_url="https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv",
    retrieved_at=RETRIEVED_AT,
    sha256="b" * 64,
)


def _context(
    game_id: str = "game:abc",
    *,
    home_rest_days: int = 7,
    away_rest_days: int = 7,
    roof: str | None = "outdoors",
) -> GameContext:
    return GameContext(
        id=stable_record_id("game_context", "nflverse", game_id),
        provider_ids={"nflverse": game_id},
        event_time=RETRIEVED_AT,
        retrieved_at=RETRIEVED_AT,
        source_snapshots=(_SOURCE,),
        game_id=game_id,
        season_year=2024,
        home_rest_days=home_rest_days,
        away_rest_days=away_rest_days,
        divisional_game=False,
        roof=roof,
        surface="grass",
    )


@pytest.fixture
def context_effects_context():
    return {}


@given("a game where the home team has more rest than the away team")
def more_home_rest(context_effects_context):
    context_effects_context["context"] = _context(home_rest_days=10, away_rest_days=6)


@when("the rest-days differential is computed")
def compute_rest_differential(context_effects_context):
    context_effects_context["differential"] = rest_days_differential(context_effects_context["context"])


@then("the differential is positive")
def differential_positive(context_effects_context):
    assert context_effects_context["differential"] > 0


@given("a game with no roof information")
def unknown_roof(context_effects_context):
    context_effects_context["context"] = _context(roof=None)


@when("the dome-adjusted probability is computed")
def compute_dome_adjusted(context_effects_context):
    context_effects_context["adjusted"] = dome_adjusted_probability(
        0.6, context_effects_context["context"], dome_coefficient=0.5
    )


@then("the adjusted probability exactly equals the baseline")
def adjusted_equals_baseline(context_effects_context):
    assert context_effects_context["adjusted"] == pytest.approx(0.6)


@given("a baseline probability of one half")
def baseline_half(context_effects_context):
    context_effects_context["baseline"] = 0.5
    context_effects_context["context"] = _context(home_rest_days=10, away_rest_days=6)


@when("the rest-adjusted probability is computed with a positive rest coefficient and more home rest")
def compute_rest_adjusted_positive(context_effects_context):
    context_effects_context["adjusted"] = rest_adjusted_probability(
        context_effects_context["baseline"], context_effects_context["context"], rest_coefficient=0.05
    )


@then("the adjusted probability exceeds one half")
def adjusted_exceeds_half(context_effects_context):
    assert context_effects_context["adjusted"] > 0.5


@given("a baseline probability and a game with unknown roof but a rest advantage")
def baseline_and_unknown_roof_with_rest(context_effects_context):
    context_effects_context["baseline"] = 0.55
    context_effects_context["context"] = _context(home_rest_days=10, away_rest_days=6, roof=None)


@when("the combined-adjusted probability is computed")
def compute_combined(context_effects_context):
    context_effects_context["combined"] = combined_adjusted_probability(
        context_effects_context["baseline"], context_effects_context["context"],
        rest_coefficient=0.05, dome_coefficient=0.5,
    )
    context_effects_context["rest_only"] = rest_adjusted_probability(
        context_effects_context["baseline"], context_effects_context["context"], rest_coefficient=0.05
    )


@then("it exactly equals the rest-only-adjusted probability")
def combined_equals_rest_only(context_effects_context):
    assert context_effects_context["combined"] == pytest.approx(context_effects_context["rest_only"])


@given("overlapping training and test season years")
def overlapping_years(context_effects_context):
    context_effects_context["training_years"] = [2022, 2023]
    context_effects_context["test_years"] = [2023]


@when("context-effects coefficients are selected on the training fold")
def select_with_overlap(context_effects_context, tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    try:
        run_context_effects_evaluation(
            store, context_effects_context["training_years"], context_effects_context["test_years"]
        )
    except Exception as error:
        context_effects_context["error"] = error


@then("the selection is rejected for train-test leakage")
def rejected_for_leakage(context_effects_context):
    assert isinstance(context_effects_context.get("error"), TrainTestLeakageError)
