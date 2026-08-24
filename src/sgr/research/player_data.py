from __future__ import annotations

from decimal import Decimal

from sgr.models import NFLAthlete, NFLInjuryReport, NFLPlayerStatline, NFLTeam
from sgr.research.schemas import (
    AvailabilityCorrectionState,
    AvailabilityReport,
    AvailabilityReportClass,
    PlayerGameStatline,
    RawSnapshotRef,
    stable_record_id,
)

# ESPN's injuries field does not publish a numeric confidence value for a
# report. Treated as a fixed, documented placeholder pending real
# calibration against outcomes -- not a vendor-asserted number, and
# deliberately not high enough to look authoritative on its own (SUD-60's
# confirmation_status() still requires corroboration for INJURY_STATUS
# regardless of this value).
ESPN_INJURY_REPORT_CONFIDENCE = Decimal("0.6")


def _raw_snapshot_ref(source_url: str, retrieved_at, raw_snapshot_path: str, raw_snapshot_sha256: str) -> RawSnapshotRef:
    return RawSnapshotRef(
        provider="espn",
        path=raw_snapshot_path,
        source_url=source_url,
        retrieved_at=retrieved_at,
        sha256=raw_snapshot_sha256,
    )


def team_id(team: NFLTeam) -> str:
    return stable_record_id("team", "espn", team.espn_team_id)


def player_id(athlete: NFLAthlete) -> str:
    return stable_record_id("player", "espn", athlete.espn_athlete_id)


def game_id(event_id: str) -> str:
    return stable_record_id("game", "espn", event_id)


def statline_record(statline: NFLPlayerStatline) -> PlayerGameStatline:
    source = _raw_snapshot_ref(
        statline.source_url, statline.retrieved_at, statline.raw_snapshot_path, statline.raw_snapshot_sha256
    )
    return PlayerGameStatline(
        id=stable_record_id(
            "player_game_statline", "espn", statline.event_id, statline.athlete.espn_athlete_id, statline.stat_category
        ),
        provider_ids={"espn": statline.athlete.espn_athlete_id},
        event_time=statline.retrieved_at,
        retrieved_at=statline.retrieved_at,
        source_snapshots=(source,),
        player_id=player_id(statline.athlete),
        team_id=team_id(statline.team),
        game_id=game_id(statline.event_id),
        stat_category=statline.stat_category,
        stat_labels=statline.stat_labels,
        stat_values=statline.stat_values,
    )


def injury_report_record(report: NFLInjuryReport) -> AvailabilityReport:
    source = _raw_snapshot_ref(
        report.source_url, report.retrieved_at, report.raw_snapshot_path, report.raw_snapshot_sha256
    )
    return AvailabilityReport(
        # Keyed on retrieved_at, not reported_at: ESPN's own "date" field
        # can stay unchanged across two of our fetches even when nothing
        # has actually updated, but each fetch is still its own
        # observation and must persist as its own record -- keying on our
        # retrieval time is what makes re-fetching append rather than
        # overwrite (SUD-91's AC).
        id=stable_record_id(
            "availability_report", "espn", report.athlete.espn_athlete_id, report.retrieved_at.isoformat()
        ),
        provider_ids={"espn": report.athlete.espn_athlete_id},
        event_time=report.reported_at,
        retrieved_at=report.retrieved_at,
        source_snapshots=(source,),
        player_id=player_id(report.athlete),
        team_id=team_id(report.team),
        game_id=game_id(report.event_id),
        report_class=AvailabilityReportClass.INJURY_STATUS,
        status_text=report.status_text,
        description=None,
        source_confidence=ESPN_INJURY_REPORT_CONFIDENCE,
        correction_state=AvailabilityCorrectionState.ORIGINAL,
    )
