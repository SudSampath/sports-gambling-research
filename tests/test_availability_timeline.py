from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from sgr.research.availability_timeline import (
    PlayerAvailabilityStatus,
    availability_reports_as_of,
    resolve_availability,
)
from sgr.research.storage import ResearchStore
from sgr.research.schemas import (
    AvailabilityCorrectionState,
    AvailabilityReport,
    AvailabilityReportClass,
    RawSnapshotRef,
    stable_record_id,
)

SOURCE = RawSnapshotRef(
    provider="p",
    path="raw/p/test.json",
    source_url="https://example.test/x",
    retrieved_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
    sha256="0" * 64,
)

AS_OF = datetime(2026, 9, 10, 22, 0, tzinfo=timezone.utc)


def _report(
    *,
    provider="a",
    provider_id="1",
    report_class,
    status_text,
    event_time,
    retrieved_at=None,
    correction_state=AvailabilityCorrectionState.ORIGINAL,
    player_id="player:test",
):
    return AvailabilityReport(
        id=stable_record_id("availability_report", provider, provider_id),
        provider_ids={provider: provider_id},
        event_time=event_time,
        retrieved_at=retrieved_at or event_time,
        source_snapshots=(SOURCE,),
        player_id=player_id,
        team_id="team:test",
        game_id="game:test",
        report_class=report_class,
        status_text=status_text,
        source_confidence=Decimal("0.8"),
        correction_state=correction_state,
    )


def test_no_reports_resolves_unknown():
    result = resolve_availability([], AS_OF)
    assert result.status == PlayerAvailabilityStatus.UNKNOWN
    assert result.reason is not None


def test_gameday_inactive_resolves_inactive():
    reports = [_report(report_class=AvailabilityReportClass.GAMEDAY_INACTIVE, status_text="Inactive", event_time=AS_OF - timedelta(minutes=80))]
    result = resolve_availability(reports, AS_OF)
    assert result.status == PlayerAvailabilityStatus.INACTIVE
    assert result.reason is None


def test_single_uncorroborated_injury_status_resolves_tentative_not_the_claimed_status():
    reports = [_report(report_class=AvailabilityReportClass.INJURY_STATUS, status_text="Out", event_time=AS_OF - timedelta(hours=1))]
    result = resolve_availability(reports, AS_OF)
    # Even though the single report says "Out", the honest resolved status
    # is generically TENTATIVE, not OUT, because it is not yet corroborated.
    assert result.status == PlayerAvailabilityStatus.TENTATIVE


def test_two_corroborating_reports_resolve_the_agreed_status():
    reports = [
        _report(provider="a", report_class=AvailabilityReportClass.INJURY_STATUS, status_text="Questionable", event_time=AS_OF - timedelta(hours=1)),
        _report(provider="b", report_class=AvailabilityReportClass.INJURY_STATUS, status_text="Questionable", event_time=AS_OF - timedelta(minutes=30)),
    ]
    result = resolve_availability(reports, AS_OF)
    assert result.status == PlayerAvailabilityStatus.LIMITED
    assert result.confirmation is not None


def test_conflicting_reports_abstain_rather_than_guess():
    reports = [
        _report(provider="a", report_class=AvailabilityReportClass.INJURY_STATUS, status_text="Out", event_time=AS_OF - timedelta(hours=1)),
        _report(provider="b", report_class=AvailabilityReportClass.INJURY_STATUS, status_text="Questionable", event_time=AS_OF - timedelta(minutes=30)),
    ]
    result = resolve_availability(reports, AS_OF)
    assert result.status == PlayerAvailabilityStatus.UNKNOWN
    assert "conflicting" in result.reason


def test_stale_gameday_inactive_resolves_unknown():
    reports = [_report(report_class=AvailabilityReportClass.GAMEDAY_INACTIVE, status_text="Inactive", event_time=AS_OF - timedelta(days=3))]
    result = resolve_availability(reports, AS_OF)
    assert result.status == PlayerAvailabilityStatus.UNKNOWN
    assert "stale" in result.reason


def test_higher_precedence_class_wins_over_lower():
    reports = [
        _report(provider="team", report_class=AvailabilityReportClass.PRACTICE_PARTICIPATION, status_text="Did Not Participate", event_time=AS_OF - timedelta(days=2)),
        _report(provider="nfl", report_class=AvailabilityReportClass.GAMEDAY_INACTIVE, status_text="Active", event_time=AS_OF - timedelta(minutes=80)),
    ]
    result = resolve_availability(reports, AS_OF)
    # The gameday-inactive report (higher precedence) says Active; the
    # earlier, lower-precedence practice report is superseded, not blended.
    assert result.status == PlayerAvailabilityStatus.AVAILABLE


def test_retracted_report_is_excluded_from_resolution():
    reports = [
        _report(
            report_class=AvailabilityReportClass.GAMEDAY_INACTIVE, status_text="Inactive",
            event_time=AS_OF - timedelta(minutes=80), correction_state=AvailabilityCorrectionState.RETRACTED,
        )
    ]
    result = resolve_availability(reports, AS_OF)
    assert result.status == PlayerAvailabilityStatus.UNKNOWN


def test_report_published_after_as_of_is_invisible():
    reports = [_report(report_class=AvailabilityReportClass.GAMEDAY_INACTIVE, status_text="Inactive", event_time=AS_OF + timedelta(minutes=5))]
    result = resolve_availability(reports, AS_OF)
    assert result.status == PlayerAvailabilityStatus.UNKNOWN


def test_report_retrieved_after_as_of_is_invisible_even_if_event_time_is_earlier():
    # Claims to be about an event before the cutoff, but we didn't actually
    # learn about it (retrieve it) until after -- still cannot count.
    reports = [
        _report(
            report_class=AvailabilityReportClass.GAMEDAY_INACTIVE, status_text="Inactive",
            event_time=AS_OF - timedelta(hours=2), retrieved_at=AS_OF + timedelta(minutes=5),
        )
    ]
    result = resolve_availability(reports, AS_OF)
    assert result.status == PlayerAvailabilityStatus.UNKNOWN


def test_unrecognized_status_text_resolves_unknown_rather_than_guessing():
    reports = [_report(report_class=AvailabilityReportClass.INJURY_STATUS, status_text="Day-to-day (unofficial)", event_time=AS_OF - timedelta(hours=1))]
    result = resolve_availability(reports, AS_OF)
    assert result.status == PlayerAvailabilityStatus.UNKNOWN
    assert "no recognized status" in result.reason


def test_in_game_incident_left_and_returned_are_distinguished():
    left = resolve_availability(
        [_report(report_class=AvailabilityReportClass.IN_GAME_INCIDENT, status_text="Left the game with an injury", event_time=AS_OF - timedelta(minutes=10))],
        AS_OF,
    )
    assert left.status == PlayerAvailabilityStatus.TENTATIVE  # single report, uncorroborated

    returned_reports = [
        _report(provider="a", report_class=AvailabilityReportClass.IN_GAME_INCIDENT, status_text="Returned to the game", event_time=AS_OF - timedelta(minutes=10)),
        _report(provider="b", report_class=AvailabilityReportClass.IN_GAME_INCIDENT, status_text="Returned to the game", event_time=AS_OF - timedelta(minutes=5)),
    ]
    returned = resolve_availability(returned_reports, AS_OF)
    assert returned.status == PlayerAvailabilityStatus.RETURNED


def test_store_backed_query_hides_reports_after_the_cutoff(tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    visible = _report(
        provider="a", report_class=AvailabilityReportClass.INJURY_STATUS, status_text="Out",
        event_time=AS_OF - timedelta(hours=2),
    )
    future = _report(
        provider="b", report_class=AvailabilityReportClass.GAMEDAY_INACTIVE, status_text="Inactive",
        event_time=AS_OF + timedelta(hours=1),
    )
    other_player = _report(
        provider="c", report_class=AvailabilityReportClass.INJURY_STATUS, status_text="Out",
        event_time=AS_OF - timedelta(hours=2), player_id="player:other",
    )
    store.write([visible, future, other_player])

    reports = availability_reports_as_of(store, "player:test", AS_OF)
    assert [r.id for r in reports] == [visible.id]
