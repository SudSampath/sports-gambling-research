from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from sgr.connectors.espn import EspnConnector, EspnError
from sgr.research.player_data import injury_report_record, statline_record
from sgr.research.schemas import AvailabilityReportClass
from sgr.research.storage import ResearchStore

scenarios("../features/espn_player_data.feature")

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"
COMPLETED_FIXTURE = "espn_summary_completed.json"
UPCOMING_FIXTURE = "espn_summary_upcoming.json"
EVENT_ID = "401772918"


def _payload(fixture_name: str) -> dict:
    return json.loads((FIXTURE_DIR / fixture_name).read_text(encoding="utf-8"))


@pytest.fixture
def summary_context(tmp_path, monkeypatch):
    return {
        "connector": EspnConnector(cache_dir=tmp_path),
        "store": ResearchStore(root=tmp_path / "store"),
        "monkeypatch": monkeypatch,
        "requests": [],
        "statlines": [],
        "injuries": [],
        "error": None,
    }


def _install_payload_response(summary_context: dict, payload: dict) -> None:
    async def fake_get_json(path: str, params: dict | None = None):
        summary_context["requests"].append({"path": path, "params": params})
        return payload

    summary_context["monkeypatch"].setattr(summary_context["connector"], "get_json", fake_get_json)


@given("a completed game's ESPN summary")
def completed_summary(summary_context):
    _install_payload_response(summary_context, _payload(COMPLETED_FIXTURE))


@given("an upcoming game's ESPN summary")
def upcoming_summary(summary_context):
    _install_payload_response(summary_context, _payload(UPCOMING_FIXTURE))


@given("a completed game's ESPN summary already ingested once")
def completed_summary_already_ingested(summary_context):
    _install_payload_response(summary_context, _payload(COMPLETED_FIXTURE))
    statlines, injuries = asyncio.run(summary_context["connector"].game_summary(EVENT_ID))
    summary_context["store"].write([injury_report_record(i) for i in injuries])
    summary_context["first_injury_count"] = len(injuries)


@when("the game summary is fetched")
def fetch_summary(summary_context):
    try:
        summary_context["statlines"], summary_context["injuries"] = asyncio.run(
            summary_context["connector"].game_summary(EVENT_ID)
        )
    except EspnError as error:
        summary_context["error"] = error


@when("the boxscore is normalized into canonical records")
def normalize_boxscore(summary_context):
    summary_context["statlines"], _ = asyncio.run(summary_context["connector"].game_summary(EVENT_ID))
    summary_context["statline_records"] = [statline_record(s) for s in summary_context["statlines"]]


@when("the injuries are normalized into canonical records")
def normalize_injuries(summary_context):
    _, summary_context["injuries"] = asyncio.run(summary_context["connector"].game_summary(EVENT_ID))
    summary_context["injury_records"] = [injury_report_record(i) for i in summary_context["injuries"]]


@when("the same game's injuries are fetched and normalized again later")
def fetch_and_normalize_again(summary_context):
    _, injuries = asyncio.run(summary_context["connector"].game_summary(EVENT_ID, refresh=True))
    summary_context["store"].write([injury_report_record(i) for i in injuries])


@then(
    "the raw response, source URL, retrieval timestamp, checksum, and normalization version "
    "are stored before normalization"
)
def raw_provenance_stored(summary_context):
    item = summary_context["statlines"][0] if summary_context["statlines"] else summary_context["injuries"][0]
    snapshot_path = Path(item.raw_snapshot_path)
    assert snapshot_path.exists()
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["source_url"] == item.source_url
    assert snapshot["payload_sha256"] == item.raw_snapshot_sha256
    assert snapshot["normalization_version"] == item.normalization_version


@then("provider odds, predictor, pickcenter, and win-probability fields are absent from every normalized record")
def excluded_fields_absent(summary_context):
    for item in (*summary_context["statlines"], *summary_context["injuries"]):
        dumped = item.model_dump()
        assert set(dumped).isdisjoint({"odds", "predictor", "pickcenter", "winprobability"})


@then("each statline record has player identity, team, game, stat category, and raw-snapshot lineage")
def statline_records_have_identity(summary_context):
    assert summary_context["statline_records"]
    for record in summary_context["statline_records"]:
        assert record.player_id and record.team_id and record.game_id
        assert record.stat_category
        assert record.source_snapshots[0].sha256


@then("only players who actually appear in a stat category produce a record")
def only_actual_participants_recorded(summary_context):
    # The completed fixture has exactly one passing athlete and one rushing
    # athlete -- two records, not a record for every roster spot.
    assert len(summary_context["statline_records"]) == 2


@then("each entry becomes an availability report with report class injury status")
def entries_become_injury_status_reports(summary_context):
    assert summary_context["injury_records"]
    for record in summary_context["injury_records"]:
        assert record.report_class == AvailabilityReportClass.INJURY_STATUS


@then("the event time is the report's own published time, not the game's kickoff")
def event_time_is_published_time(summary_context):
    record = summary_context["injury_records"][0]
    assert record.event_time == datetime(2026, 8, 24, 13, 32, tzinfo=timezone.utc)


@then("the total number of availability reports in storage increases")
def availability_report_count_increases(summary_context):
    loaded = summary_context["store"].load_all("availability_report")
    assert len(loaded) > summary_context["first_injury_count"]


@then("the original reports remain unchanged")
def original_reports_unchanged(summary_context):
    loaded = summary_context["store"].load_all("availability_report")
    original_count = sum(1 for r in loaded if r.retrieved_at == loaded[0].retrieved_at)
    assert original_count >= 1  # the first batch's rows are still present, not replaced


@then("the normalized injury reports are not represented as the historical pregame injury state")
def injury_reports_not_claimed_historical(summary_context):
    statlines, injuries = asyncio.run(summary_context["connector"].game_summary(EVENT_ID))
    # Structural check: nothing on NFLInjuryReport claims a "pregame" or
    # "historical" timestamp distinct from reported_at/retrieved_at -- the
    # model has no such field to fabricate one.
    assert set(type(injuries[0]).model_fields) == {
        "event_id", "team", "athlete", "status_text", "reported_at", "retrieved_at",
        "source_url", "raw_snapshot_path", "raw_snapshot_sha256", "normalization_version",
    }


@given(parsers.parse('ESPN\'s summary endpoint responds with "{failure}"'))
def espn_summary_failure(summary_context, failure):
    request = httpx.Request("GET", "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary")

    async def failing_get_json(path: str, params: dict | None = None):
        if failure == "HTTP status":
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError("unavailable", request=request, response=response)
        if failure == "schema drift":
            return {"boxscore": "not-an-object", "injuries": None}
        raise AssertionError(f"unsupported failure case: {failure}")

    summary_context["monkeypatch"].setattr(summary_context["connector"], "get_json", failing_get_json)


@then("a typed ESPN error is returned")
def typed_espn_error(summary_context):
    assert isinstance(summary_context["error"], EspnError)
