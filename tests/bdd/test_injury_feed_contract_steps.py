from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pytest_bdd import given, scenarios, then, when

from sgr.research.availability import ConfirmationStatus, confirmation_status, is_executable
from sgr.research.schemas import (
    AvailabilityCorrectionState,
    AvailabilityReport,
    AvailabilityReportClass,
    RawSnapshotRef,
    load_canonical_record,
    stable_record_id,
)

scenarios("../features/injury_feed_contract.feature")

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "availability_reports_sample.json"
AS_OF = datetime(2026, 9, 10, 22, 0, tzinfo=timezone.utc)

SOURCE = RawSnapshotRef(
    provider="sample-provider",
    path="raw/sample-provider/test.json",
    source_url="https://example-provider.test/injuries/test",
    retrieved_at=AS_OF,
    sha256="0" * 64,
)


@pytest.fixture
def injury_context():
    return {"reports": [], "status": None, "raw_fixture_text": None, "normalized": []}


def _report(*, provider="p", provider_id="1", report_class, event_time, correction_state=AvailabilityCorrectionState.ORIGINAL):
    return AvailabilityReport(
        id=stable_record_id("availability_report", provider, provider_id),
        provider_ids={provider: provider_id},
        event_time=event_time,
        retrieved_at=event_time,
        source_snapshots=(SOURCE,),
        player_id="player:test",
        team_id="team:test",
        game_id="game:test",
        report_class=report_class,
        status_text="status",
        description="test",
        source_confidence=Decimal("0.5"),
        correction_state=correction_state,
    )


@given("the credential-free availability-report fixture")
def the_fixture(injury_context):
    injury_context["raw_fixture_text"] = FIXTURE_PATH.read_text(encoding="utf-8")


@when("the fixture entries are normalized")
def normalize_fixture(injury_context):
    raw = json.loads(injury_context["raw_fixture_text"])
    injury_context["normalized"] = [load_canonical_record(entry) for entry in raw]


@then(
    "each report retains player ID, team ID, game ID, report class, status text, source-published "
    "time, retrieval time, source confidence, correction state, raw checksum, and schema version"
)
def each_report_has_required_fields(injury_context):
    for report in injury_context["normalized"]:
        assert isinstance(report, AvailabilityReport)
        assert report.player_id and report.team_id and report.game_id
        assert report.report_class in AvailabilityReportClass
        assert report.status_text
        assert report.event_time is not None
        assert report.retrieved_at is not None
        assert 0 <= report.source_confidence <= 1
        assert report.correction_state in AvailabilityCorrectionState
        assert report.source_snapshots[0].sha256
        assert report.schema_version


@then("the fixture contains no credential or secret token")
def fixture_has_no_credentials(injury_context):
    lowered = injury_context["raw_fixture_text"].casefold()
    for token in ("api_key", "apikey", "secret", "password", "token", "account_id"):
        assert token not in lowered


@then("all five report classes are distinct values")
def five_distinct_classes(injury_context):
    assert len(set(AvailabilityReportClass)) == 5


@given("one in-game-incident report with no corroboration")
def one_in_game_incident_report(injury_context):
    injury_context["reports"] = [
        _report(report_class=AvailabilityReportClass.IN_GAME_INCIDENT, event_time=AS_OF - timedelta(minutes=5))
    ]


@given("one gameday-inactive report from an official source")
def one_official_report(injury_context):
    injury_context["reports"] = [
        _report(report_class=AvailabilityReportClass.GAMEDAY_INACTIVE, event_time=AS_OF - timedelta(minutes=90))
    ]


@given("two injury-status reports from different providers close together in time")
def two_reports_different_providers(injury_context):
    injury_context["reports"] = [
        _report(provider="a", provider_id="1", report_class=AvailabilityReportClass.INJURY_STATUS, event_time=AS_OF - timedelta(hours=1)),
        _report(provider="b", provider_id="1", report_class=AvailabilityReportClass.PRACTICE_PARTICIPATION, event_time=AS_OF - timedelta(minutes=30)),
    ]


@given("two injury-status reports from the same provider")
def two_reports_same_provider(injury_context):
    injury_context["reports"] = [
        _report(provider="a", provider_id="1", report_class=AvailabilityReportClass.INJURY_STATUS, event_time=AS_OF - timedelta(hours=1)),
        _report(provider="a", provider_id="2", report_class=AvailabilityReportClass.INJURY_STATUS, event_time=AS_OF - timedelta(minutes=30)),
    ]


@given("one gameday-inactive report three days old")
def one_stale_official_report(injury_context):
    injury_context["reports"] = [
        _report(report_class=AvailabilityReportClass.GAMEDAY_INACTIVE, event_time=AS_OF - timedelta(days=3))
    ]


@given("one retracted gameday-inactive report")
def one_retracted_report(injury_context):
    injury_context["reports"] = [
        _report(
            report_class=AvailabilityReportClass.GAMEDAY_INACTIVE,
            event_time=AS_OF - timedelta(minutes=30),
            correction_state=AvailabilityCorrectionState.RETRACTED,
        )
    ]


@when("confirmation is evaluated")
def evaluate_confirmation(injury_context):
    injury_context["status"] = confirmation_status(injury_context["reports"], AS_OF)


@then("the status is tentative")
def status_is_tentative(injury_context):
    assert injury_context["status"] == ConfirmationStatus.TENTATIVE


@then("the status is confirmed")
def status_is_confirmed(injury_context):
    assert injury_context["status"] == ConfirmationStatus.CONFIRMED


@then("the status is stale")
def status_is_stale(injury_context):
    assert injury_context["status"] == ConfirmationStatus.STALE


@then("a tentative status cannot back an executable recommendation")
@then("a stale status cannot back an executable recommendation")
def status_not_executable(injury_context):
    assert is_executable(injury_context["status"]) is False


@then("a confirmed status can back an executable recommendation")
def status_is_executable(injury_context):
    assert is_executable(injury_context["status"]) is True
