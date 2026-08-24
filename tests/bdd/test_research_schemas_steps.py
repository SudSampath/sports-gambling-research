from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from pydantic import ValidationError
from pytest_bdd import given, scenarios, then, when

from sgr.models import NFLSeasonType
from sgr.research.schemas import (
    Forecast,
    Game,
    IncompatibleSchemaVersionError,
    KalshiEvent,
    KalshiMarket,
    MatchDecision,
    MatchStatus,
    MarketSide,
    OrderBookSnapshot,
    Outcome,
    PaperRecommendation,
    PriceLevel,
    RawSnapshotRef,
    Team,
    TeamStrengthSnapshot,
    load_canonical_record,
    stable_record_id,
)
from sgr.research.storage import ResearchStore


scenarios("../features/research_schemas.feature")


@pytest.fixture
def schema_context(tmp_path):
    return {"store": ResearchStore(tmp_path / "research"), "error": None}


def _examples() -> dict[str, object]:
    retrieved = datetime(2026, 8, 1, 9, tzinfo=timezone.utc)
    feature_cutoff = retrieved + timedelta(hours=1)
    forecast_created = feature_cutoff + timedelta(minutes=5)
    quote_observed = forecast_created + timedelta(minutes=5)
    decision_at = quote_observed + timedelta(minutes=1)
    recommendation_at = decision_at + timedelta(minutes=1)
    kickoff = retrieved + timedelta(days=1)
    settled = kickoff + timedelta(hours=4)
    source = RawSnapshotRef(
        provider="espn",
        path="raw/espn/2026-08-01/" + "a" * 64 + ".json",
        source_url="https://site.api.espn.com/scoreboard?dates=20260802",
        retrieved_at=retrieved,
        sha256="a" * 64,
    )
    sources = (source,)
    home_id = stable_record_id("team", "espn", "26")
    away_id = stable_record_id("team", "espn", "17")
    game_id = stable_record_id("game", "espn", "401900001")
    event_id = stable_record_id("kalshi_event", "KXNFLGAME-26")
    market_id = stable_record_id("kalshi_market", "KXNFLGAME-26-SEA")
    forecast_id = stable_record_id("forecast", game_id, "pythagorean-v1", feature_cutoff)
    quote_id = stable_record_id("orderbook_snapshot", market_id, quote_observed)
    decision_id = stable_record_id("match_decision", game_id, market_id, decision_at)
    recommendation_id = stable_record_id("paper_recommendation", decision_id, quote_id)
    records = [
        Team(
            id=home_id,
            provider_ids={"espn": "26"},
            event_time=retrieved,
            retrieved_at=retrieved,
            source_snapshots=sources,
            abbreviation="SEA",
            display_name="Seattle Seahawks",
        ),
        Game(
            id=game_id,
            provider_ids={"espn": "401900001"},
            event_time=kickoff,
            retrieved_at=retrieved,
            source_snapshots=sources,
            season_year=2026,
            season_type=NFLSeasonType.REGULAR,
            week=1,
            home_team_id=home_id,
            away_team_id=away_id,
            kickoff_at=kickoff,
            status="scheduled",
            completed=False,
            neutral_site=False,
        ),
        TeamStrengthSnapshot(
            id=stable_record_id("team_strength_snapshot", home_id, feature_cutoff),
            provider_ids={"model": "pythagorean-v1"},
            event_time=feature_cutoff,
            retrieved_at=retrieved,
            source_snapshots=sources,
            team_id=home_id,
            season_year=2026,
            through_week=0,
            feature_cutoff_at=feature_cutoff,
            games_played=0,
            points_for=0,
            points_against=0,
            exponent=Decimal("2.37"),
            strength=Decimal("0.50"),
        ),
        KalshiEvent(
            id=event_id,
            provider_ids={"kalshi": "KXNFLGAME-26"},
            event_time=kickoff,
            retrieved_at=retrieved,
            source_snapshots=sources,
            ticker="KXNFLGAME-26",
            title="New England at Seattle",
            scheduled_at=kickoff,
            settlement_source="NFL",
        ),
        KalshiMarket(
            id=market_id,
            provider_ids={"kalshi": "KXNFLGAME-26-SEA"},
            event_time=kickoff,
            retrieved_at=retrieved,
            source_snapshots=sources,
            kalshi_event_id=event_id,
            ticker="KXNFLGAME-26-SEA",
            title="Seattle wins",
            open_at=retrieved,
            close_at=kickoff,
            fee_type="quadratic_with_maker_fees",
            rules_primary="NFL final result; tie pays $0.50",
        ),
        OrderBookSnapshot(
            id=quote_id,
            provider_ids={"kalshi": "KXNFLGAME-26-SEA"},
            event_time=quote_observed,
            retrieved_at=quote_observed,
            source_snapshots=sources,
            market_id=market_id,
            observed_at=quote_observed,
            yes_bids=(PriceLevel(price_dollars=Decimal("0.51"), contracts=Decimal("20")),),
            no_bids=(PriceLevel(price_dollars=Decimal("0.47"), contracts=Decimal("15")),),
        ),
        Forecast(
            id=forecast_id,
            provider_ids={"model": "pythagorean-v1"},
            event_time=forecast_created,
            retrieved_at=forecast_created,
            source_snapshots=sources,
            game_id=game_id,
            model_version="pythagorean-v1",
            feature_cutoff_at=feature_cutoff,
            forecast_created_at=forecast_created,
            home_win_probability=Decimal("0.58"),
            tie_probability=Decimal("0.01"),
            uncertainty=Decimal("0.08"),
            exponent=Decimal("2.37"),
            home_games_played=6,
            away_games_played=6,
            home_shrinkage_weight=Decimal("0.6"),
            away_shrinkage_weight=Decimal("0.6"),
            training_window_start=retrieved,
            home_field_applied=True,
        ),
        MatchDecision(
            id=decision_id,
            provider_ids={"policy": "exact-match-v1"},
            event_time=decision_at,
            retrieved_at=decision_at,
            source_snapshots=sources,
            game_id=game_id,
            market_id=market_id,
            forecast_id=forecast_id,
            decision_at=decision_at,
            status=MatchStatus.ELIGIBLE,
            reason="Exact participants, kickoff, outcome, and rules matched.",
        ),
        PaperRecommendation(
            id=recommendation_id,
            provider_ids={"policy": "paper-edge-v1"},
            event_time=recommendation_at,
            retrieved_at=recommendation_at,
            source_snapshots=sources,
            match_decision_id=decision_id,
            quote_snapshot_id=quote_id,
            recommendation_at=recommendation_at,
            side=MarketSide.YES,
            entry_price_dollars=Decimal("0.53"),
            max_loss_dollars=Decimal("10"),
            net_edge=Decimal("0.03"),
        ),
        Outcome(
            id=stable_record_id("outcome", game_id, market_id),
            provider_ids={"espn": "401900001", "kalshi": "KXNFLGAME-26-SEA"},
            event_time=settled,
            retrieved_at=settled,
            source_snapshots=sources,
            game_id=game_id,
            market_id=market_id,
            settled_at=settled,
            home_score=24,
            away_score=20,
            market_payout_dollars=Decimal("1"),
        ),
    ]
    return {record.entity_type: record for record in records}


@given("canonical examples for every Phase 1 entity")
def canonical_examples(schema_context):
    schema_context["records"] = _examples()


@then("all ten required entity schemas are present")
def all_required_schemas(schema_context):
    assert set(schema_context["records"]) == {
        "team",
        "game",
        "team_strength_snapshot",
        "kalshi_event",
        "kalshi_market",
        "orderbook_snapshot",
        "forecast",
        "match_decision",
        "paper_recommendation",
        "outcome",
    }


@then("every canonical entity has stable identity timestamps and raw provenance")
def shared_contract(schema_context):
    for record in schema_context["records"].values():
        if record.entity_type == "team":
            assert record.id == stable_record_id("team", "espn", "26")
        else:
            assert record.id
        assert record.provider_ids
        assert record.event_time.tzinfo is not None
        assert record.retrieved_at.tzinfo is not None
        assert record.schema_version == "1.0.0"
        assert len(record.source_snapshots[0].sha256) == 64


@given("a canonical team payload")
def canonical_team_payload(schema_context):
    schema_context["team_payload"] = _examples()["team"].model_dump(mode="json")


@when("credential or account data is added to the payload")
def add_private_fields(schema_context):
    payload = dict(schema_context["team_payload"])
    payload["api_key"] = "must-not-be-stored"
    failures = []
    try:
        Team.model_validate(payload)
    except ValidationError as error:
        failures.append(error)
    payload = dict(schema_context["team_payload"])
    payload["provider_ids"] = {"account_id": "private"}
    try:
        Team.model_validate(payload)
    except ValidationError as error:
        failures.append(error)
    schema_context["private_failures"] = failures


@then("canonical validation rejects the private fields")
def reject_private_fields(schema_context):
    assert len(schema_context["private_failures"]) == 2


@when("the canonical records are written to local analytical storage")
def write_canonical_records(schema_context):
    schema_context["store"].write(schema_context["records"].values())
    recommendation = schema_context["records"]["paper_recommendation"]
    schema_context["lineage"] = schema_context["store"].lineage_for_recommendation(
        recommendation.id
    )


@then("the recommendation lineage joins game forecast market quote decision and outcome")
def lineage_is_queryable(schema_context):
    lineage = schema_context["lineage"]
    assert lineage.forecast.game_id == lineage.game.id
    assert lineage.match_decision.market_id == lineage.market.id
    assert lineage.recommendation.quote_snapshot_id == lineage.quote.id
    assert lineage.outcome is not None
    assert lineage.outcome.game_id == lineage.game.id


@then("feature forecast quote decision kickoff and settlement times remain distinct")
def lineage_times_are_distinct(schema_context):
    lineage = schema_context["lineage"]
    times = {
        lineage.forecast.feature_cutoff_at,
        lineage.forecast.forecast_created_at,
        lineage.quote.observed_at,
        lineage.match_decision.decision_at,
        lineage.game.kickoff_at,
        lineage.outcome.settled_at,
    }
    assert len(times) == 6


@given("a canonical game payload from an unregistered old schema")
def old_game_payload(schema_context):
    payload = _examples()["game"].model_dump(mode="json")
    payload["schema_version"] = "0.9.0"
    schema_context["old_payload"] = payload


@when("the canonical reader loads the old artifact")
def load_old_artifact(schema_context):
    try:
        load_canonical_record(schema_context["old_payload"])
    except Exception as error:
        schema_context["error"] = error


@then("a typed incompatible schema version error is returned")
def incompatible_version_error(schema_context):
    assert isinstance(schema_context["error"], IncompatibleSchemaVersionError)


@given("a credential-free raw ESPN payload")
def raw_espn_payload(schema_context):
    schema_context["raw_payload"] = {"events": [{"id": "401900001"}]}
    schema_context["retrieved_at"] = datetime(2026, 8, 1, 9, tzinfo=timezone.utc)


@when("the raw payload and canonical records are retained locally")
def retain_raw_and_canonical(schema_context):
    store = schema_context["store"]
    kwargs = {
        "source_url": "https://site.api.espn.com/scoreboard?dates=20260802",
        "retrieved_at": schema_context["retrieved_at"],
    }
    first = store.retain_raw_snapshot("espn", schema_context["raw_payload"], **kwargs)
    first_bytes = (store.root / first.path).read_bytes()
    second = store.retain_raw_snapshot("espn", schema_context["raw_payload"], **kwargs)
    store.write(_examples().values())
    schema_context.update(
        {"first_ref": first, "second_ref": second, "first_bytes": first_bytes}
    )


@then("the raw snapshot is content addressed and not overwritten")
def raw_snapshot_is_immutable(schema_context):
    store = schema_context["store"]
    assert schema_context["first_ref"] == schema_context["second_ref"]
    assert (store.root / schema_context["first_ref"].path).read_bytes() == schema_context["first_bytes"]
    assert schema_context["first_ref"].sha256 in schema_context["first_ref"].path


@then("DuckDB and Parquet contain queryable canonical tables")
def local_tables_are_queryable(schema_context):
    store = schema_context["store"]
    with duckdb.connect(str(store.database_path), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM games").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM forecasts").fetchone()[0] == 1
    assert (store.canonical_dir / "games.parquet").exists()
    assert (store.canonical_dir / "paper_recommendations.parquet").exists()


@then("generated research data is excluded from Git")
def generated_data_is_ignored():
    gitignore = (Path(__file__).parents[2] / ".gitignore").read_text(encoding="utf-8")
    assert "/data/" in gitignore
    assert "/.research/" in gitignore
