from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sgr.research.availability_timeline import PlayerAvailabilityStatus, resolve_availability
from sgr.research.player_impact import (
    MINIMUM_PRIOR_GAMES_TO_BE_A_STARTER,
    MissingReplacementError,
    compute_player_usages,
    estimate_player_impact,
    usual_starters,
)
from sgr.research.pythagorean import InsufficientHistoryError, InvalidScoringInputError
from sgr.research.schemas import AvailabilityReport, Game, PlayerGameStatline
from sgr.research.storage import ResearchStore

# Only these two statuses trigger the adjustment. estimate_player_impact
# produces a single binary "with player vs. with replacement" delta, not a
# probabilistic blend across outcomes -- applying it on a merely
# QUESTIONABLE/LIMITED/TENTATIVE signal would overstate confidence in a
# player actually sitting out. DOUBTFUL is deliberately excluded too: NFL
# usage of "Doubtful" itself only implies roughly a 25% chance of playing,
# not a settled no, so it stays out of the trigger set until there's reason
# to model that probability explicitly rather than treat it as binary.
UNAVAILABLE_STATUSES = frozenset({PlayerAvailabilityStatus.OUT, PlayerAvailabilityStatus.INACTIVE})


@dataclass(frozen=True)
class PlayerAdjustment:
    player_id: str
    team_id: str
    primary_category: str
    signed_win_probability_delta: float  # already sign-flipped to the home team's perspective


@dataclass(frozen=True)
class InjuryAdjustmentResult:
    home_win_probability_delta: float
    adjustments: tuple[PlayerAdjustment, ...]


_NO_ADJUSTMENT = InjuryAdjustmentResult(0.0, ())


def compute_injury_adjustment(
    store: ResearchStore,
    all_games: list[Game],
    all_statlines: list[PlayerGameStatline],
    all_availability_reports: list[AvailabilityReport],
    home_team_id: str,
    away_team_id: str,
    season_year: int,
    feature_cutoff_at: datetime,
    *,
    neutral_site: bool,
    exponent: float,
) -> InjuryAdjustmentResult:
    """The net home-win-probability delta from every usual starter on either
    team who is, as of feature_cutoff_at, resolved as OUT or INACTIVE.

    "Usual starter" uses the exact same definition (usual_starters) as the
    standalone missing-starter evaluation harness in
    player_impact_evaluation.py, so the two stay comparable. Availability is
    resolved via resolve_availability (SUD-61), which requires two
    independent corroborating sources -- or an official-class report -- to
    confirm a non-official status. With only ESPN as a single provider
    (SUD-91), an ordinary weekly injury-report entry alone will resolve to
    TENTATIVE, not a confirmed OUT, and this function will not act on it.
    That is the existing corroboration policy working as designed, not a
    bug in this function; it stays dormant for live data until a second
    provider is added or an official-class signal (e.g. a gameday inactive
    list) is ingested.
    """
    games_by_id = {g.id: g for g in all_games}
    home_usages = compute_player_usages(all_statlines, games_by_id, home_team_id, season_year, feature_cutoff_at)
    away_usages = compute_player_usages(all_statlines, games_by_id, away_team_id, season_year, feature_cutoff_at)
    starters = usual_starters(
        {home_team_id: home_usages, away_team_id: away_usages}, MINIMUM_PRIOR_GAMES_TO_BE_A_STARTER
    )

    reports_by_player: dict[str, list[AvailabilityReport]] = {}
    for report in all_availability_reports:
        if report.event_time <= feature_cutoff_at and report.retrieved_at <= feature_cutoff_at:
            reports_by_player.setdefault(report.player_id, []).append(report)

    adjustments: list[PlayerAdjustment] = []
    total_delta = 0.0
    for team_id, opponent_id in ((home_team_id, away_team_id), (away_team_id, home_team_id)):
        for player_id in starters.get(team_id, set()):
            resolution = resolve_availability(reports_by_player.get(player_id, []), feature_cutoff_at)
            if resolution.status not in UNAVAILABLE_STATUSES:
                continue
            try:
                impact = estimate_player_impact(
                    all_statlines, all_games, player_id, team_id, opponent_id,
                    season_year, feature_cutoff_at, neutral_site=neutral_site, exponent=exponent,
                )
            except (MissingReplacementError, InsufficientHistoryError, InvalidScoringInputError):
                continue
            # impact.mean_impact = P(with player) - P(with replacement) for
            # `team_id`, always >= 0 for a real usual starter vs. their
            # backup. A missing HOME starter should lower home_win_probability
            # (negative delta); a missing AWAY starter should raise it
            # (positive delta) -- the sign flip below is what makes
            # `home_win_probability + total_delta` in generate_forecast
            # correct in both cases, not the opposite.
            signed_delta = -impact.mean_impact if team_id == home_team_id else impact.mean_impact
            total_delta += signed_delta
            adjustments.append(
                PlayerAdjustment(
                    player_id=player_id, team_id=team_id,
                    primary_category=impact.primary_category,
                    signed_win_probability_delta=signed_delta,
                )
            )

    if not adjustments:
        return _NO_ADJUSTMENT
    return InjuryAdjustmentResult(home_win_probability_delta=total_delta, adjustments=tuple(adjustments))
