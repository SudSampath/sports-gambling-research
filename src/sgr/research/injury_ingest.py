from __future__ import annotations

from dataclasses import dataclass

from sgr.connectors.espn import EspnConnector
from sgr.models import NFLSeasonType
from sgr.research.player_data import injury_report_record
from sgr.research.schemas import Game
from sgr.research.storage import ResearchStore


@dataclass(frozen=True)
class InjuryIngestReport:
    season_year: int
    games_considered: int
    games_with_injury_entries: int
    reports_written: int


async def ingest_current_injuries(
    connector: EspnConnector,
    store: ResearchStore,
    season_year: int,
    *,
    refresh: bool = False,
) -> InjuryIngestReport:
    """Fetch and persist ESPN's *current* injury report for every not-yet-completed
    regular-season game in season_year.

    Deliberately restricted to not-yet-completed games -- the mirror image of
    player_backfill.py's own restriction to boxscores only. ESPN's injuries
    field always reflects today's team report regardless of which event_id is
    queried (established in EspnConnector.game_summary's own docstring), so
    writing it against an already-completed game would misrepresent today's
    report as having been knowable at that past game's kickoff: exactly the
    hindsight leakage this project's rules prohibit. Point-in-time injury
    history can only be built going forward, one real fetch at a time.
    """
    all_games = [g for g in store.load_all("game") if isinstance(g, Game)]
    games = [
        g
        for g in all_games
        if g.season_type == NFLSeasonType.REGULAR and g.season_year == season_year and not g.completed
    ]

    games_with_injury_entries = 0
    all_records = []
    for game in games:
        event_id = game.provider_ids.get("espn")
        if event_id is None:
            continue
        _, injuries = await connector.game_summary(event_id, refresh=refresh)
        if not injuries:
            continue
        games_with_injury_entries += 1
        all_records.extend(injury_report_record(report) for report in injuries)

    if all_records:
        store.write(all_records)

    return InjuryIngestReport(
        season_year=season_year,
        games_considered=len(games),
        games_with_injury_entries=games_with_injury_entries,
        reports_written=len(all_records),
    )
