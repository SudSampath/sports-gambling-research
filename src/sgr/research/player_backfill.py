from __future__ import annotations

from dataclasses import dataclass, field

from sgr.connectors.espn import EspnConnector
from sgr.models import NFLSeasonType
from sgr.research.player_data import statline_record
from sgr.research.schemas import Game
from sgr.research.storage import ResearchStore


@dataclass(frozen=True)
class BoxscoreBackfillReport:
    season_years: tuple[int, ...]
    games_considered: int
    games_with_statlines: int
    statlines_written: int
    games_with_zero_statlines: tuple[str, ...]  # ESPN event_ids, explicitly listed per the AC


async def backfill_boxscores(
    connector: EspnConnector,
    store: ResearchStore,
    season_years: list[int],
    *,
    refresh: bool = False,
) -> BoxscoreBackfillReport:
    """Backfill PlayerGameStatline records for every completed regular-season
    game already in the store for season_years.

    Deliberately boxscores only -- SUD-91 established that ESPN's injuries
    field reflects the *current* team report regardless of which event is
    queried, so backfilling injuries against historical games would write
    today's report under a past game_id: exactly the hindsight leakage this
    project's rules prohibit. There is no such problem for boxscores, which
    are genuinely tied to the completed game they describe.

    Cache-first via game_summary()'s own immutable-snapshot behavior, so a
    rerun with refresh=False reuses cached responses and ResearchStore.write's
    deterministic ids prevent duplicate rows either way.
    """
    all_games = [g for g in store.load_all("game") if isinstance(g, Game)]
    games = [
        g
        for g in all_games
        if g.season_type == NFLSeasonType.REGULAR and g.completed and g.season_year in season_years
    ]

    games_with_statlines = 0
    zero_statline_event_ids: list[str] = []
    all_records = []  # accumulated in memory; ResearchStore.write() re-exports the
    # full Parquet table on every call, so writing once at the end (rather than
    # once per game) avoids an O(games^2) rewrite over an 800+ game backfill.

    for game in games:
        event_id = game.provider_ids.get("espn")
        if event_id is None:
            zero_statline_event_ids.append(game.id)
            continue
        statlines, _ = await connector.game_summary(event_id, refresh=refresh)
        if not statlines:
            zero_statline_event_ids.append(event_id)
            continue
        games_with_statlines += 1
        all_records.extend(statline_record(s) for s in statlines)

    if all_records:
        store.write(all_records)

    return BoxscoreBackfillReport(
        season_years=tuple(sorted(set(season_years))),
        games_considered=len(games),
        games_with_statlines=games_with_statlines,
        statlines_written=len(all_records),
        games_with_zero_statlines=tuple(zero_statline_event_ids),
    )
