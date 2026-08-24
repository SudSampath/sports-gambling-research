from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from sgr.research.schemas import AvailabilityCorrectionState, AvailabilityReport, AvailabilityReportClass

# Official-source classes: authoritative on their own, no corroboration
# needed -- these come from the team or league directly (a published
# gameday inactive list, an official roster move), not from an observer.
OFFICIAL_REPORT_CLASSES = frozenset(
    {AvailabilityReportClass.GAMEDAY_INACTIVE, AvailabilityReportClass.ROSTER_STATUS}
)

# Tentative-by-default classes: a single report of these can never promote
# to CONFIRMED on its own -- this is what makes "an injury timeout,
# broadcast description, or single unconfirmed report ... remains tentative"
# (SUD-60's AC) true by construction rather than by convention.
CORROBORATION_REQUIRED_CLASSES = frozenset(
    {
        AvailabilityReportClass.INJURY_STATUS,
        AvailabilityReportClass.PRACTICE_PARTICIPATION,
        AvailabilityReportClass.IN_GAME_INCIDENT,
    }
)

# How close together two corroborating reports must land to count as
# independent confirmation of the same status, rather than two providers
# echoing the same stale rumor at very different times.
CORROBORATION_WINDOW = timedelta(hours=2)

# Beyond this age, even an otherwise-confirmed report is stale rather than
# actionable. Freshness is a separate axis from source count -- a
# well-corroborated report from three days ago is not current evidence.
MAX_CONFIRMED_AGE = timedelta(hours=24)


class ConfirmationStatus(StrEnum):
    TENTATIVE = "tentative"
    CONFIRMED = "confirmed"
    STALE = "stale"  # was confirmable, but too old as of the decision time


def _provider_key(report: AvailabilityReport) -> frozenset[str]:
    # Distinctness is by *source* (the provider_ids key, e.g. "beat-reporter-a"),
    # not by the full (source, per-report-id) pair -- two different reports
    # from the same source naturally have different report IDs, so keying on
    # the pair would let one source "corroborate" itself by filing a second,
    # differently-numbered report.
    return frozenset(report.provider_ids.keys())


def confirmation_status(
    reports: list[AvailabilityReport],
    as_of: datetime,
    *,
    corroboration_window: timedelta = CORROBORATION_WINDOW,
    max_confirmed_age: timedelta = MAX_CONFIRMED_AGE,
) -> ConfirmationStatus:
    """Predeclared confirmation policy for one player's availability as of `as_of`.

    Only reports with event_time <= as_of AND retrieved_at <= as_of, with a
    non-retracted correction_state, are considered -- a report whose
    claimed event_time is early but that wasn't actually retrieved until
    after `as_of` still cannot count, since it wasn't knowable at decision
    time. A retraction removes a report from consideration entirely, not
    just flags it.

    Rules, in order:
    1. No eligible reports at all -> TENTATIVE (nothing to act on).
    2. Any OFFICIAL_REPORT_CLASSES report is confirmed on its own.
    3. Otherwise, at least two reports from CORROBORATION_REQUIRED_CLASSES,
       from distinct providers, published within corroboration_window of
       each other, are required to promote to CONFIRMED. source_confidence
       is metadata for downstream weighting, never a substitute for
       corroboration -- a single high-confidence report still cannot
       promote itself.
    4. Whatever would otherwise be CONFIRMED is demoted to STALE if its
       most recent contributing report is older than max_confirmed_age as
       of `as_of`.
    """
    eligible = [
        r
        for r in reports
        if r.event_time <= as_of and r.retrieved_at <= as_of and r.correction_state != AvailabilityCorrectionState.RETRACTED
    ]
    if not eligible:
        return ConfirmationStatus.TENTATIVE

    official = [r for r in eligible if r.report_class in OFFICIAL_REPORT_CLASSES]
    if official:
        most_recent = max(official, key=lambda r: r.event_time)
        if as_of - most_recent.event_time > max_confirmed_age:
            return ConfirmationStatus.STALE
        return ConfirmationStatus.CONFIRMED

    corroborated = sorted(
        (r for r in eligible if r.report_class in CORROBORATION_REQUIRED_CLASSES),
        key=lambda r: r.event_time,
    )
    for report in corroborated:
        window = [
            other
            for other in corroborated
            if abs(other.event_time - report.event_time) <= corroboration_window
        ]
        if len({_provider_key(r) for r in window}) >= 2:
            most_recent = max(window, key=lambda r: r.event_time)
            if as_of - most_recent.event_time > max_confirmed_age:
                return ConfirmationStatus.STALE
            return ConfirmationStatus.CONFIRMED

    return ConfirmationStatus.TENTATIVE


def is_executable(status: ConfirmationStatus) -> bool:
    """AC: only a confirmed, fresh status can back an executable
    recommendation -- both TENTATIVE and STALE cannot."""
    return status == ConfirmationStatus.CONFIRMED
