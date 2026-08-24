from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from sgr.connectors.espn import (
    EspnConnector,
    EspnRequestError,
    EspnSchemaError,
    PointInTimeDataUnavailableError,
)


scenarios("../features/espn_ingestion.feature")

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"
COMPLETED_FIXTURE = "espn_scoreboard_2025-09-07.json"
COMPLETED_DATE = date(2025, 9, 7)
COMPLETED_SOURCE_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
    "scoreboard?dates=20250907"
)
FIXTURE_DATES = {
    COMPLETED_FIXTURE: COMPLETED_DATE,
    "espn_scoreboard_2026-09-09-scheduled.json": date(2026, 9, 9),
}


def _payload(fixture_name: str = COMPLETED_FIXTURE) -> dict:
    return json.loads((FIXTURE_DIR / fixture_name).read_text(encoding="utf-8"))


@pytest.fixture
def espn_context(tmp_path, monkeypatch):
    return {
        "connector": EspnConnector(cache_dir=tmp_path),
        "monkeypatch": monkeypatch,
        "requests": [],
        "games": [],
        "error": None,
    }


def _install_payload_response(espn_context: dict, payload: dict) -> None:
    async def fake_get_json(path: str, params: dict | None = None):
        espn_context["requests"].append({"path": path, "params": params})
        return deepcopy(payload)

    espn_context["monkeypatch"].setattr(
        espn_context["connector"], "get_json", fake_get_json
    )


@given(parsers.parse('the "{fixture_name}" ESPN scoreboard fixture'))
def given_scoreboard_fixture(espn_context, fixture_name):
    espn_context["fixture_name"] = fixture_name
    espn_context["payload"] = _payload(fixture_name)
    _install_payload_response(espn_context, espn_context["payload"])


@when(parsers.parse('I request ESPN games by "{scope}"'))
def request_games_by_scope(espn_context, scope):
    connector = espn_context["connector"]
    payload = espn_context["payload"]
    event = payload["events"][0]
    season_year = event["season"]["year"]
    week = event["week"]["number"]
    if scope == "date":
        request = connector.games_for_date(
            FIXTURE_DATES[espn_context["fixture_name"]], refresh=True
        )
    elif scope == "week":
        request = connector.games_for_week(season_year, week, refresh=True)
    elif scope == "season":
        request = connector.games_for_season(season_year, refresh=True)
    else:
        raise AssertionError(f"Unsupported BDD query scope: {scope}")
    espn_context["scope"] = scope
    espn_context["games"] = asyncio.run(request)


@then(
    parsers.parse(
        'one canonical game is returned for season {season_year:d} '
        'type "{season_type}" week {week:d}'
    )
)
def canonical_game_identity(espn_context, season_year, season_type, week):
    assert len(espn_context["games"]) == 1
    game = espn_context["games"][0]
    assert game.season_year == season_year
    assert game.season_type == season_type
    assert game.week == week


@then(
    parsers.parse(
        'its status is "{status}" with completed "{completed}" '
        'and neutral site "{neutral_site}"'
    )
)
def canonical_game_status(espn_context, status, completed, neutral_site):
    game = espn_context["games"][0]
    assert game.status == status
    assert game.completed is (completed == "true")
    assert game.neutral_site is (neutral_site == "true")
    if game.completed:
        assert game.home_score is not None
        assert game.away_score is not None
    else:
        assert game.home_score is None
        assert game.away_score is None


@then("the raw response provenance is retained before normalization")
def raw_response_provenance(espn_context):
    game = espn_context["games"][0]
    snapshot_path = Path(game.raw_snapshot_path)
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    canonical_payload = json.dumps(
        snapshot["payload"], sort_keys=True, separators=(",", ":")
    )
    assert snapshot_path.exists()
    assert game.source_url == snapshot["source_url"]
    assert game.retrieved_at == datetime.fromisoformat(snapshot["retrieved_at"])
    assert game.raw_snapshot_sha256 == snapshot["payload_sha256"]
    assert game.raw_snapshot_sha256 == hashlib.sha256(canonical_payload.encode()).hexdigest()
    assert game.normalization_version == EspnConnector.NORMALIZATION_VERSION
    assert snapshot["normalization_version"] == EspnConnector.NORMALIZATION_VERSION


@then("provider odds and predictor fields are absent from canonical games")
def provider_predictions_are_excluded(espn_context):
    canonical_fields = set(espn_context["games"][0].model_dump())
    assert canonical_fields.isdisjoint({"odds", "predictor", "win_probability"})


@then(parsers.parse('the "{scope}" provider query is explicit'))
def provider_query_is_explicit(espn_context, scope):
    assert len(espn_context["requests"]) == 1
    params = espn_context["requests"][0]["params"]
    assert params["dates"]
    if scope == "date":
        assert set(params) == {"dates"}
    elif scope == "week":
        assert params["seasontype"] == 2
        assert params["week"] == 1
        assert params["limit"] == 100
    else:
        assert params["seasontype"] == 2
        assert "week" not in params
        assert params["limit"] == 1000


@given(parsers.parse("the completed ESPN fixture encoded as season type {espn_type:d}"))
def completed_fixture_for_season_phase(espn_context, espn_type):
    payload = _payload()
    payload["events"][0]["season"]["type"] = espn_type
    espn_context["payload"] = payload
    _install_payload_response(espn_context, payload)


@when("I request the encoded game by date")
def request_encoded_game(espn_context):
    espn_context["games"] = asyncio.run(
        espn_context["connector"].games_for_date(COMPLETED_DATE, refresh=True)
    )


@then(parsers.parse('the canonical season type is "{season_type}"'))
def canonical_season_type(espn_context, season_type):
    assert espn_context["games"][0].season_type == season_type


@given("a completed ESPN response is already cached")
def completed_response_is_cached(espn_context):
    connector = espn_context["connector"]
    connector._write_snapshot(
        COMPLETED_DATE,
        _payload(),
        COMPLETED_SOURCE_URL,
        datetime(2025, 9, 7, 18, tzinfo=timezone.utc),
    )

    async def unexpected_request(*_args, **_kwargs):
        raise AssertionError("A cache hit must not make a live ESPN request.")

    espn_context["monkeypatch"].setattr(connector, "get_json", unexpected_request)


@when("I request the cached game date")
def request_cached_game(espn_context):
    espn_context["games"] = asyncio.run(
        espn_context["connector"].games_for_date(COMPLETED_DATE)
    )


@then("the cached game is returned without a live ESPN request")
def cached_game_is_returned(espn_context):
    assert [game.event_id for game in espn_context["games"]] == ["401772918"]
    assert espn_context["requests"] == []


@given("two completed snapshots retrieved at different times")
def two_completed_snapshots(espn_context):
    connector = espn_context["connector"]
    first = _payload()
    second = _payload()
    first_retrieval = datetime(2025, 9, 7, 12, tzinfo=timezone.utc)
    second_retrieval = first_retrieval + timedelta(hours=2)
    second["events"][0]["competitions"][0]["competitors"][0]["score"] = "41"
    second["events"][0]["competitions"][0]["competitors"][1]["score"] = "42"
    connector._write_snapshot(COMPLETED_DATE, first, COMPLETED_SOURCE_URL, first_retrieval)
    connector._write_snapshot(COMPLETED_DATE, second, COMPLETED_SOURCE_URL, second_retrieval)
    espn_context["prediction_at"] = first_retrieval + timedelta(minutes=30)


@when("I request games at a time between the two snapshots")
def request_between_snapshots(espn_context):
    espn_context["games"] = asyncio.run(
        espn_context["connector"].games_for_date(
            COMPLETED_DATE, prediction_at=espn_context["prediction_at"]
        )
    )


@then("only the older eligible snapshot is normalized")
def older_snapshot_is_selected(espn_context):
    game = espn_context["games"][0]
    assert game.retrieved_at == datetime(2025, 9, 7, 12, tzinfo=timezone.utc)
    assert (game.away_score, game.home_score) == (40, 41)


@given("no ESPN snapshots are cached")
def no_snapshots_cached(espn_context):
    assert not list(Path(espn_context["connector"].cache_dir).glob("**/*.json"))


@when("I request a historical game at a prediction timestamp")
def request_missing_historical_snapshot(espn_context):
    try:
        asyncio.run(
            espn_context["connector"].games_for_date(
                COMPLETED_DATE,
                prediction_at=datetime(2025, 9, 7, 12, tzinfo=timezone.utc),
            )
        )
    except Exception as error:  # asserted by the following BDD step
        espn_context["error"] = error


@then("a typed point-in-time unavailable error is returned")
def typed_point_in_time_error(espn_context):
    assert isinstance(espn_context["error"], PointInTimeDataUnavailableError)


@given("a cached ESPN snapshot whose payload was modified after capture")
def corrupt_snapshot(espn_context):
    snapshot = espn_context["connector"]._write_snapshot(
        COMPLETED_DATE,
        _payload(),
        COMPLETED_SOURCE_URL,
        datetime(2025, 9, 7, 18, tzinfo=timezone.utc),
    )
    path = Path(snapshot["snapshot_path"])
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["payload"]["events"][0]["id"] = "changed-after-capture"
    path.write_text(json.dumps(raw), encoding="utf-8")


@when("I request the corrupt cached game")
def request_corrupt_snapshot(espn_context):
    _capture_cached_error(espn_context)


@given("a cached ESPN payload with an unsupported event schema")
def schema_drifted_snapshot(espn_context):
    espn_context["connector"]._write_snapshot(
        COMPLETED_DATE,
        {"events": [{"id": "schema-drift"}]},
        COMPLETED_SOURCE_URL,
        datetime(2025, 9, 7, 18, tzinfo=timezone.utc),
    )


@when("I request the schema-drifted cached game")
def request_schema_drifted_snapshot(espn_context):
    _capture_cached_error(espn_context)


def _capture_cached_error(espn_context: dict) -> None:
    try:
        asyncio.run(espn_context["connector"].games_for_date(COMPLETED_DATE))
    except Exception as error:  # asserted by the following BDD step
        espn_context["error"] = error


@then("a typed ESPN schema error is returned")
def typed_schema_error(espn_context):
    assert isinstance(espn_context["error"], EspnSchemaError)


@given("a completed ESPN snapshot at a fixed retrieval timestamp")
def fixed_timestamp_snapshot(espn_context):
    espn_context["retrieved_at"] = datetime(2025, 9, 7, 18, tzinfo=timezone.utc)
    snapshot = espn_context["connector"]._write_snapshot(
        COMPLETED_DATE,
        _payload(),
        COMPLETED_SOURCE_URL,
        espn_context["retrieved_at"],
    )
    espn_context["snapshot_path"] = Path(snapshot["snapshot_path"])
    espn_context["original_bytes"] = espn_context["snapshot_path"].read_bytes()


@when("a different payload is stored at the same retrieval timestamp")
def conflicting_snapshot_write(espn_context):
    changed = _payload()
    changed["events"][0]["id"] = "conflicting-event"
    try:
        espn_context["connector"]._write_snapshot(
            COMPLETED_DATE,
            changed,
            COMPLETED_SOURCE_URL,
            espn_context["retrieved_at"],
        )
    except Exception as error:  # asserted by the following BDD step
        espn_context["error"] = error


@then("a typed immutable snapshot error is returned")
def immutable_snapshot_error(espn_context):
    assert isinstance(espn_context["error"], EspnSchemaError)
    assert "Refusing to overwrite immutable ESPN snapshot" in str(espn_context["error"])


@then("the original snapshot remains valid")
def original_snapshot_remains_valid(espn_context):
    assert espn_context["snapshot_path"].read_bytes() == espn_context["original_bytes"]
    games = asyncio.run(espn_context["connector"].games_for_date(COMPLETED_DATE))
    assert [game.event_id for game in games] == ["401772918"]


@given("a cached ESPN payload with a non-boolean completed flag")
def non_boolean_completed_flag(espn_context):
    payload = _payload()
    payload["events"][0]["competitions"][0]["status"]["type"]["completed"] = "false"
    espn_context["connector"]._write_snapshot(
        COMPLETED_DATE,
        payload,
        COMPLETED_SOURCE_URL,
        datetime(2025, 9, 7, 18, tzinfo=timezone.utc),
    )


@given(parsers.parse('ESPN fails with "{failure}"'))
def espn_provider_failure(espn_context, failure):
    request = httpx.Request("GET", COMPLETED_SOURCE_URL)

    async def failed_request(*_args, **_kwargs):
        if failure == "HTTP status":
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError("unavailable", request=request, response=response)
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if failure == "TLS failure":
            raise httpx.ConnectError("certificate verification failed", request=request)
        if failure == "invalid JSON":
            raise ValueError("invalid JSON")
        raise AssertionError(f"Unsupported BDD provider failure: {failure}")

    espn_context["monkeypatch"].setattr(
        espn_context["connector"], "get_json", failed_request
    )


@when("I refresh a game date")
def refresh_game_date(espn_context):
    try:
        asyncio.run(
            espn_context["connector"].games_for_date(COMPLETED_DATE, refresh=True)
        )
    except Exception as error:  # asserted by the following BDD step
        espn_context["error"] = error


@then("a typed ESPN request error is returned")
def typed_request_error(espn_context):
    assert isinstance(espn_context["error"], EspnRequestError)
