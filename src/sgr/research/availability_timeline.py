from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sgr.research.availability import ConfirmationStatus, confirmation_status
from sgr.research.schemas import AvailabilityCorrectionState, AvailabilityReport, AvailabilityReportClass
from sgr.research.storage import ResearchStore


class PlayerAvailabilityStatus(StrEnum):
    AVAILABLE = "available"
    LIMITED = "limited"
    DOUBTFUL = "doubtful"
    OUT = "out"
    INACTIVE = "inactive"
    LEFT_GAME = "left_game"
    RETURNED = "returned"
    TENTATIVE = "tentative"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AvailabilityResolution:
    status: PlayerAvailabilityStatus
    as_of: datetime
    confirmation: ConfirmationStatus | None
    contributing_report_ids: tuple[str, ...]
    reason: str | None


# Deterministic precedence across report classes, most decisive first.
# GAMEDAY_INACTIVE and IN_GAME_INCIDENT are necessarily close to or during
# the game itself and override earlier-week signals; ROSTER_STATUS (a team
# transaction) outranks the two practice/injury report classes, which are
# the earliest, least certain signals in the week.
CLASS_PRECEDENCE = (
    AvailabilityReportClass.GAMEDAY_INACTIVE,
    AvailabilityReportClass.IN_GAME_INCIDENT,
    AvailabilityReportClass.ROSTER_STATUS,
    AvailabilityReportClass.INJURY_STATUS,
    AvailabilityReportClass.PRACTICE_PARTICIPATION,
)

# NFL-standard designation text, not vendor-specific -- the league itself
# defines these words in its injury-report policy, so this mapping holds
# regardless of which provider SUD-60's decision matrix eventually selects.
# Unrecognized text intentionally maps to None (contributes no status)
# rather than guessing.
_INJURY_STATUS_MAP: dict[str, PlayerAvailabilityStatus] = {
    "out": PlayerAvailabilityStatus.OUT,
    "doubtful": PlayerAvailabilityStatus.DOUBTFUL,
    "questionable": PlayerAvailabilityStatus.LIMITED,
    "probable": PlayerAvailabilityStatus.LIMITED,
}
_PRACTICE_STATUS_MAP: dict[str, PlayerAvailabilityStatus] = {
    "did not participate": PlayerAvailabilityStatus.DOUBTFUL,
    "limited participation": PlayerAvailabilityStatus.LIMITED,
    "full participation": PlayerAvailabilityStatus.AVAILABLE,
}
_GAMEDAY_INACTIVE_MAP: dict[str, PlayerAvailabilityStatus] = {
    "inactive": PlayerAvailabilityStatus.INACTIVE,
    "active": PlayerAvailabilityStatus.AVAILABLE,
}
_ROSTER_STATUS_MAP: dict[str, PlayerAvailabilityStatus] = {
    "injured reserve": PlayerAvailabilityStatus.INACTIVE,
    "physically unable to perform": PlayerAvailabilityStatus.INACTIVE,
    "non-football injury": PlayerAvailabilityStatus.INACTIVE,
    "reserve/injured": PlayerAvailabilityStatus.INACTIVE,
    # "active" roster status alone says nothing about *this game's*
    # availability -- deliberately not mapped, so it contributes no status
    # rather than a false AVAILABLE signal.
}


def _map_status_text(report_class: AvailabilityReportClass, status_text: str) -> PlayerAvailabilityStatus | None:
    text = status_text.strip().casefold()
    if report_class == AvailabilityReportClass.INJURY_STATUS:
        return _INJURY_STATUS_MAP.get(text)
    if report_class == AvailabilityReportClass.PRACTICE_PARTICIPATION:
        return _PRACTICE_STATUS_MAP.get(text)
    if report_class == AvailabilityReportClass.GAMEDAY_INACTIVE:
        return _GAMEDAY_INACTIVE_MAP.get(text)
    if report_class == AvailabilityReportClass.ROSTER_STATUS:
        return _ROSTER_STATUS_MAP.get(text)
    if report_class == AvailabilityReportClass.IN_GAME_INCIDENT:
        if "return" in text and "question" not in text and "doubt" not in text:
            return PlayerAvailabilityStatus.RETURNED
        if "left" in text or "exit" in text:
            return PlayerAvailabilityStatus.LEFT_GAME
        return PlayerAvailabilityStatus.TENTATIVE
    return None


def resolve_availability(
    reports: list[AvailabilityReport], as_of: datetime
) -> AvailabilityResolution:
    """Resolve one player's availability as of `as_of` from all reports about them.

    Only reports with event_time <= as_of AND retrieved_at <= as_of, with a
    non-retracted correction_state, are visible -- a report whose claimed
    event_time is early but that we did not actually retrieve until after
    `as_of` still cannot influence the result, since we did not know it at
    decision time. No report published or retrieved after `as_of` can
    influence the result, and historical replay never substitutes a later
    (e.g. current) status for an earlier decision.

    1. No visible reports at all -> UNKNOWN.
    2. Take the highest-precedence report class (CLASS_PRECEDENCE) present
       among visible reports.
    3. Map each of that class's report texts to a PlayerAvailabilityStatus.
       If they disagree, abstain with UNKNOWN and an explicit conflict
       reason rather than picking one -- this is what makes "unresolved
       conflicts produce an abstention reason" true by construction.
    4. Reuse SUD-60's confirmation_status on that class's reports:
       - STALE -> UNKNOWN (the confirmable information is too old to treat
         as current, distinct from never having been reported at all).
       - TENTATIVE -> TENTATIVE status, regardless of what the single,
         uncorroborated report claimed -- we don't yet trust *which*
         status is true, so the honest output is generically tentative.
       - CONFIRMED -> the mapped status from step 3.
    5. If nothing in the top class maps to a recognized status (unrecognized
       text only) -> UNKNOWN.
    """
    eligible = [
        r
        for r in reports
        if r.event_time <= as_of and r.retrieved_at <= as_of and r.correction_state != AvailabilityCorrectionState.RETRACTED
    ]
    if not eligible:
        return AvailabilityResolution(PlayerAvailabilityStatus.UNKNOWN, as_of, None, (), "no reports as of this timestamp")

    top_class = next((c for c in CLASS_PRECEDENCE if any(r.report_class == c for r in eligible)), None)
    class_reports = [r for r in eligible if r.report_class == top_class]

    mapped = {(_map_status_text(r.report_class, r.status_text), r.id) for r in class_reports}
    mapped_statuses = {status for status, _ in mapped if status is not None}
    contributing_ids = tuple(sorted(r.id for r in class_reports))

    if not mapped_statuses:
        return AvailabilityResolution(
            PlayerAvailabilityStatus.UNKNOWN, as_of, None, contributing_ids,
            f"no recognized status text among {top_class.value} reports",
        )
    if len(mapped_statuses) > 1:
        return AvailabilityResolution(
            PlayerAvailabilityStatus.UNKNOWN, as_of, None, contributing_ids,
            f"conflicting {top_class.value} reports: {sorted(s.value for s in mapped_statuses)}",
        )

    status = next(iter(mapped_statuses))
    confirmation = confirmation_status(class_reports, as_of)

    if confirmation == ConfirmationStatus.STALE:
        return AvailabilityResolution(
            PlayerAvailabilityStatus.UNKNOWN, as_of, confirmation, contributing_ids,
            f"{top_class.value} reports are stale as of this timestamp",
        )
    if confirmation == ConfirmationStatus.TENTATIVE:
        return AvailabilityResolution(
            PlayerAvailabilityStatus.TENTATIVE, as_of, confirmation, contributing_ids,
            f"{top_class.value} status is not yet corroborated",
        )
    return AvailabilityResolution(status, as_of, confirmation, contributing_ids, None)


def availability_reports_as_of(store: ResearchStore, player_id: str, as_of: datetime) -> list[AvailabilityReport]:
    """Load one player's reports visible as of `as_of` from local storage.

    Loads every AvailabilityReport (there is no player-indexed column to
    filter on in ResearchStore's generic table shape) and filters in
    Python, matching the same pattern SUD-25's compute_team_strength uses
    for Game records.
    """
    all_reports = [r for r in store.load_all("availability_report") if isinstance(r, AvailabilityReport)]
    return [
        r
        for r in all_reports
        if r.player_id == player_id and r.event_time <= as_of and r.retrieved_at <= as_of
    ]
