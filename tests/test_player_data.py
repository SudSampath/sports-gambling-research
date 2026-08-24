from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sgr.models import NFLAthlete, NFLInjuryReport, NFLPlayerStatline, NFLTeam
from sgr.research.player_data import game_id, injury_report_record, player_id, statline_record, team_id
from sgr.research.schemas import AvailabilityReportClass
from sgr.research.storage import ResearchStore

TEAM = NFLTeam(espn_team_id="2", abbreviation="BUF", name="Buffalo Bills")
ATHLETE = NFLAthlete(espn_athlete_id="3915411", display_name="Ty Johnson", position="RB")


def _statline(retrieved_at: datetime) -> NFLPlayerStatline:
    return NFLPlayerStatline(
        event_id="401772918",
        team=TEAM,
        athlete=ATHLETE,
        stat_category="rushing",
        stat_labels=("CAR", "YDS"),
        stat_values=("10", "40"),
        retrieved_at=retrieved_at,
        source_url="https://example.test/summary?event=401772918",
        raw_snapshot_path="raw/espn/event-401772918/x.json",
        raw_snapshot_sha256="0" * 64,
        normalization_version="espn-summary-v1",
    )


def _injury(reported_at: datetime, retrieved_at: datetime) -> NFLInjuryReport:
    return NFLInjuryReport(
        event_id="401772918",
        team=TEAM,
        athlete=ATHLETE,
        status_text="Questionable",
        reported_at=reported_at,
        retrieved_at=retrieved_at,
        source_url="https://example.test/summary?event=401772918",
        raw_snapshot_path="raw/espn/event-401772918/x.json",
        raw_snapshot_sha256="0" * 64,
        normalization_version="espn-summary-v1",
    )


def test_statline_record_converts_all_fields():
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    record = statline_record(_statline(now))
    assert record.player_id == player_id(ATHLETE)
    assert record.team_id == team_id(TEAM)
    assert record.game_id == game_id("401772918")
    assert record.stat_category == "rushing"
    assert record.stat_labels == ("CAR", "YDS")
    assert record.stat_values == ("10", "40")
    assert record.source_snapshots[0].sha256 == "0" * 64


def test_injury_report_record_uses_espns_reported_time_as_event_time():
    reported = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
    retrieved = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)
    record = injury_report_record(_injury(reported, retrieved))
    assert record.event_time == reported
    assert record.retrieved_at == retrieved
    assert record.report_class == AvailabilityReportClass.INJURY_STATUS
    assert record.status_text == "Questionable"


def test_refetching_the_same_report_appends_rather_than_overwrites(tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    reported = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
    first_retrieval = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)
    second_retrieval = first_retrieval + timedelta(hours=6)

    first = injury_report_record(_injury(reported, first_retrieval))
    second = injury_report_record(_injury(reported, second_retrieval))  # same underlying report, refetched later

    assert first.id != second.id  # distinct records despite identical reported_at/status_text
    store.write([first])
    store.write([second])

    loaded = store.load_all("availability_report")
    assert len(loaded) == 2
    assert {r.retrieved_at for r in loaded} == {first_retrieval, second_retrieval}


def test_statline_and_injury_share_consistent_team_and_game_ids():
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    statline = statline_record(_statline(now))
    injury = injury_report_record(_injury(now, now))
    assert statline.team_id == injury.team_id
    assert statline.game_id == injury.game_id
    assert statline.player_id == injury.player_id
