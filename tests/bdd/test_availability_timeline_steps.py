from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pytest_bdd import given, scenarios, then, when

from sgr.research.availability_timeline import PlayerAvailabilityStatus, resolve_availability
from sgr.research.schemas import (
    AvailabilityCorrectionState,
    AvailabilityReport,
    AvailabilityReportClass,
    RawSnapshotRef,
    stable_record_id,
)

scenarios("../features/availability_timeline.feature")

AS_OF = datetime(2026, 9, 10, 22, 0, tzinfo=timezone.utc)
SOURCE = RawSnapshotRef(
    provider="p",
    path="raw/p/test.json",
    source_url="https://example.test/x",
    retrieved_at=AS_OF,
    sha256="0" * 64,
)


@pytest.fixture
def timeline_context():
    return {"reports": [], "result": None}


def _report(*, provider="a", provider_id="1", report_class, status_text, event_time, retrieved_at=None, correction_state=AvailabilityCorrectionState.ORIGINAL):
    return AvailabilityReport(
        id=stable_record_id("availability_report", provider, provider_id),
        provider_ids={provider: provider_id},
        event_time=event_time,
        retrieved_at=retrieved_at or event_time,
        source_snapshots=(SOURCE,),
        player_id="player:test",
        team_id="team:test",
        game_id="game:test",
        report_class=report_class,
        status_text=status_text,
        source_confidence=Decimal("0.8"),
        correction_state=correction_state,
    )


@given("one gameday-inactive report says a player is inactive")
def one_gameday_inactive(timeline_context):
    timeline_context["reports"] = [
        _report(report_class=AvailabilityReportClass.GAMEDAY_INACTIVE, status_text="Inactive", event_time=AS_OF - timedelta(minutes=80))
    ]


@given("one uncorroborated injury-status report claims a player is out")
def one_uncorroborated_out(timeline_context):
    timeline_context["reports"] = [
        _report(report_class=AvailabilityReportClass.INJURY_STATUS, status_text="Out", event_time=AS_OF - timedelta(hours=1))
    ]


@given("two independent injury-status reports agree a player is questionable")
def two_agreeing_reports(timeline_context):
    timeline_context["reports"] = [
        _report(provider="a", report_class=AvailabilityReportClass.INJURY_STATUS, status_text="Questionable", event_time=AS_OF - timedelta(hours=1)),
        _report(provider="b", report_class=AvailabilityReportClass.INJURY_STATUS, status_text="Questionable", event_time=AS_OF - timedelta(minutes=30)),
    ]


@given("two injury-status reports disagree about the same player")
def two_disagreeing_reports(timeline_context):
    timeline_context["reports"] = [
        _report(provider="a", report_class=AvailabilityReportClass.INJURY_STATUS, status_text="Out", event_time=AS_OF - timedelta(hours=1)),
        _report(provider="b", report_class=AvailabilityReportClass.INJURY_STATUS, status_text="Questionable", event_time=AS_OF - timedelta(minutes=30)),
    ]


@given("an early practice-participation report and a later gameday-inactive report disagree")
def precedence_conflict(timeline_context):
    timeline_context["reports"] = [
        _report(provider="team", report_class=AvailabilityReportClass.PRACTICE_PARTICIPATION, status_text="Did Not Participate", event_time=AS_OF - timedelta(days=2)),
        _report(provider="nfl", report_class=AvailabilityReportClass.GAMEDAY_INACTIVE, status_text="Active", event_time=AS_OF - timedelta(minutes=80)),
    ]


@given("a gameday-inactive report from three days before the decision time")
def stale_report(timeline_context):
    timeline_context["reports"] = [
        _report(report_class=AvailabilityReportClass.GAMEDAY_INACTIVE, status_text="Inactive", event_time=AS_OF - timedelta(days=3))
    ]


@given("a report whose event time is before the cutoff but was retrieved after it")
def retrieved_after_cutoff(timeline_context):
    timeline_context["reports"] = [
        _report(
            report_class=AvailabilityReportClass.GAMEDAY_INACTIVE, status_text="Inactive",
            event_time=AS_OF - timedelta(hours=2), retrieved_at=AS_OF + timedelta(minutes=5),
        )
    ]


@given("a player who was later ruled out but only a questionable report existed at an earlier time")
def later_ruled_out_earlier_questionable(timeline_context):
    timeline_context["earlier_as_of"] = AS_OF - timedelta(hours=6)
    timeline_context["reports"] = [
        _report(report_class=AvailabilityReportClass.INJURY_STATUS, status_text="Questionable", event_time=AS_OF - timedelta(hours=7)),
        # The later ruling exists in the data but is after the earlier decision time.
        _report(report_class=AvailabilityReportClass.GAMEDAY_INACTIVE, status_text="Inactive", event_time=AS_OF - timedelta(minutes=80)),
    ]


@when("availability is resolved at that time")
def resolve_at_as_of(timeline_context):
    as_of = timeline_context.get("earlier_as_of", AS_OF)
    timeline_context["result"] = resolve_availability(timeline_context["reports"], as_of)


@then("the resolved status is inactive")
def status_inactive(timeline_context):
    assert timeline_context["result"].status == PlayerAvailabilityStatus.INACTIVE


@then("the resolved status is tentative, not out")
def status_tentative_not_out(timeline_context):
    assert timeline_context["result"].status == PlayerAvailabilityStatus.TENTATIVE


@then("the resolved status is limited")
def status_limited(timeline_context):
    assert timeline_context["result"].status == PlayerAvailabilityStatus.LIMITED


@then("the resolved status is unknown")
def status_unknown(timeline_context):
    assert timeline_context["result"].status == PlayerAvailabilityStatus.UNKNOWN


@then("the resolution records a conflict reason")
def reason_mentions_conflict(timeline_context):
    assert "conflicting" in timeline_context["result"].reason


@then("the gameday-inactive report's status wins")
def gameday_inactive_wins(timeline_context):
    assert timeline_context["result"].status == PlayerAvailabilityStatus.AVAILABLE


@then("the resolution records a staleness reason")
def reason_mentions_staleness(timeline_context):
    assert "stale" in timeline_context["result"].reason


@then("the resolved status reflects only what was knowable then")
def reflects_only_knowable_then(timeline_context):
    # At the earlier decision time, only the "Questionable" report existed
    # (the later "Inactive" ruling's event_time is still after the earlier
    # as_of), so resolution must reflect that, uncorroborated, not the
    # eventual official ruling.
    assert timeline_context["result"].status == PlayerAvailabilityStatus.TENTATIVE
