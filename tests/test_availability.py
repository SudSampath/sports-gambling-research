from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from sgr.research.availability import (
    ConfirmationStatus,
    confirmation_status,
    is_executable,
)
from sgr.research.schemas import (
    AvailabilityCorrectionState,
    AvailabilityReport,
    AvailabilityReportClass,
    RawSnapshotRef,
    load_canonical_record,
    stable_record_id,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "availability_reports_sample.json"

SOURCE = RawSnapshotRef(
    provider="sample-provider",
    path="raw/sample-provider/test.json",
    source_url="https://example-provider.test/injuries/test",
    retrieved_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
    sha256="0" * 64,
)


def _report(
    *,
    provider: str = "sample-provider",
    provider_id: str = "rpt",
    report_class: AvailabilityReportClass,
    event_time: datetime,
    correction_state: AvailabilityCorrectionState = AvailabilityCorrectionState.ORIGINAL,
    confidence: str = "0.5",
) -> AvailabilityReport:
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
        status_text="Questionable",
        description="test report",
        source_confidence=Decimal(confidence),
        correction_state=correction_state,
    )


# --- credential-free fixture normalization -----------------------------------


def test_fixture_reports_normalize_with_all_required_fields():
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    reports = [load_canonical_record(entry) for entry in raw]
    assert len(reports) == 2
    for report in reports:
        assert isinstance(report, AvailabilityReport)
        assert report.player_id
        assert report.team_id
        assert report.game_id
        assert report.report_class in AvailabilityReportClass
        assert report.status_text
        assert report.event_time is not None  # source-published time
        assert report.retrieved_at is not None
        assert 0 <= report.source_confidence <= 1
        assert report.correction_state in AvailabilityCorrectionState
        assert report.source_snapshots[0].sha256
        assert report.schema_version == "1.0.0"


def test_fixture_has_no_credentials_or_secrets():
    raw = FIXTURE_PATH.read_text(encoding="utf-8").casefold()
    for token in ("api_key", "apikey", "secret", "password", "token", "account_id"):
        assert token not in raw


def test_report_classes_remain_distinct_values():
    assert len(set(AvailabilityReportClass)) == 5
    assert AvailabilityReportClass.GAMEDAY_INACTIVE != AvailabilityReportClass.ROSTER_STATUS
    assert AvailabilityReportClass.INJURY_STATUS != AvailabilityReportClass.PRACTICE_PARTICIPATION


# --- confirmation policy ------------------------------------------------------


def test_no_reports_is_tentative():
    assert confirmation_status([], datetime.now(timezone.utc)) == ConfirmationStatus.TENTATIVE


def test_single_official_gameday_inactive_report_is_confirmed_alone():
    as_of = datetime(2026, 9, 10, 22, 0, tzinfo=timezone.utc)
    report = _report(report_class=AvailabilityReportClass.GAMEDAY_INACTIVE, event_time=as_of - timedelta(minutes=90))
    assert confirmation_status([report], as_of) == ConfirmationStatus.CONFIRMED


def test_single_in_game_incident_report_stays_tentative():
    as_of = datetime(2026, 9, 10, 22, 0, tzinfo=timezone.utc)
    report = _report(report_class=AvailabilityReportClass.IN_GAME_INCIDENT, event_time=as_of - timedelta(minutes=5))
    assert confirmation_status([report], as_of) == ConfirmationStatus.TENTATIVE


def test_high_confidence_alone_cannot_substitute_for_corroboration():
    # source_confidence=1.0 on a lone tentative-class report still cannot promote it.
    as_of = datetime(2026, 9, 10, 22, 0, tzinfo=timezone.utc)
    report = _report(
        report_class=AvailabilityReportClass.INJURY_STATUS,
        event_time=as_of - timedelta(minutes=5),
        confidence="1.0",
    )
    assert confirmation_status([report], as_of) == ConfirmationStatus.TENTATIVE


def test_two_independent_corroborating_reports_within_window_are_confirmed():
    as_of = datetime(2026, 9, 10, 22, 0, tzinfo=timezone.utc)
    reports = [
        _report(
            provider="beat-reporter-a", provider_id="a1",
            report_class=AvailabilityReportClass.INJURY_STATUS,
            event_time=as_of - timedelta(hours=1),
        ),
        _report(
            provider="beat-reporter-b", provider_id="b1",
            report_class=AvailabilityReportClass.PRACTICE_PARTICIPATION,
            event_time=as_of - timedelta(minutes=30),
        ),
    ]
    assert confirmation_status(reports, as_of) == ConfirmationStatus.CONFIRMED


def test_same_provider_repeating_itself_does_not_count_as_corroboration():
    as_of = datetime(2026, 9, 10, 22, 0, tzinfo=timezone.utc)
    reports = [
        _report(provider="beat-reporter-a", provider_id="a1", report_class=AvailabilityReportClass.INJURY_STATUS, event_time=as_of - timedelta(hours=1)),
        _report(provider="beat-reporter-a", provider_id="a2", report_class=AvailabilityReportClass.INJURY_STATUS, event_time=as_of - timedelta(minutes=30)),
    ]
    assert confirmation_status(reports, as_of) == ConfirmationStatus.TENTATIVE


def test_corroborating_reports_outside_the_window_do_not_count():
    as_of = datetime(2026, 9, 10, 22, 0, tzinfo=timezone.utc)
    reports = [
        _report(provider="beat-reporter-a", provider_id="a1", report_class=AvailabilityReportClass.INJURY_STATUS, event_time=as_of - timedelta(hours=10)),
        _report(provider="beat-reporter-b", provider_id="b1", report_class=AvailabilityReportClass.PRACTICE_PARTICIPATION, event_time=as_of - timedelta(minutes=5)),
    ]
    assert confirmation_status(reports, as_of) == ConfirmationStatus.TENTATIVE


def test_stale_official_report_is_not_confirmed():
    as_of = datetime(2026, 9, 10, 22, 0, tzinfo=timezone.utc)
    report = _report(report_class=AvailabilityReportClass.GAMEDAY_INACTIVE, event_time=as_of - timedelta(days=3))
    assert confirmation_status([report], as_of) == ConfirmationStatus.STALE


def test_retracted_official_report_is_excluded_entirely():
    as_of = datetime(2026, 9, 10, 22, 0, tzinfo=timezone.utc)
    report = _report(
        report_class=AvailabilityReportClass.GAMEDAY_INACTIVE,
        event_time=as_of - timedelta(minutes=30),
        correction_state=AvailabilityCorrectionState.RETRACTED,
    )
    assert confirmation_status([report], as_of) == ConfirmationStatus.TENTATIVE


def test_future_reports_do_not_count_as_of_an_earlier_decision_time():
    as_of = datetime(2026, 9, 10, 20, 0, tzinfo=timezone.utc)
    future_report = _report(
        report_class=AvailabilityReportClass.GAMEDAY_INACTIVE, event_time=as_of + timedelta(hours=1)
    )
    assert confirmation_status([future_report], as_of) == ConfirmationStatus.TENTATIVE


@pytest.mark.parametrize("status", [ConfirmationStatus.TENTATIVE, ConfirmationStatus.STALE])
def test_only_confirmed_status_is_executable(status):
    assert is_executable(status) is False


def test_confirmed_status_is_executable():
    assert is_executable(ConfirmationStatus.CONFIRMED) is True
