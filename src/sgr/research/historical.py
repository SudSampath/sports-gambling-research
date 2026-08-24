from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sgr.connectors.espn import EspnConnector
from sgr.models import NFLGame, NFLSeasonType, NFLTeam
from sgr.research.schemas import Game, RawSnapshotRef, Team, stable_record_id
from sgr.research.storage import ResearchStore

# The NFL has run an 18-week, 17-game-per-team regular season since 2021.
# 32 teams x 17 games / 2 (each game counted once) = 272.
REGULAR_SEASON_WEEKS = range(1, 19)
EXPECTED_TEAM_COUNT = 32
EXPECTED_REGULAR_SEASON_GAMES = 272


class HistoricalIngestError(RuntimeError):
    """Base error for historical season ingestion."""


class SeasonCoverageError(HistoricalIngestError):
    """Raised when a season's captured games fail the completeness quality gate.

    Carries the coverage report so a caller can inspect exactly what was
    missing, duplicated, or inconsistent rather than continuing silently.
    """

    def __init__(self, report: "SeasonCoverageReport") -> None:
        self.report = report
        super().__init__(
            f"Season {report.season_year} failed the coverage quality gate: "
            f"{report.games_captured}/{report.games_expected} games, "
            f"{report.teams_captured}/{report.teams_expected} teams, "
            f"{len(report.duplicate_event_ids)} duplicate event IDs, "
            f"{len(report.inconsistent_event_ids)} inconsistent-field events, "
            f"{len(report.incomplete_event_ids)} incomplete games."
        )


@dataclass(frozen=True)
class SeasonCoverageReport:
    """Explicit coverage/exclusion accounting for one season's regular-season ingest."""

    season_year: int
    require_completed: bool
    games_captured: int
    games_expected: int
    teams_captured: int
    teams_expected: int
    duplicate_event_ids: tuple[str, ...] = field(default_factory=tuple)
    inconsistent_event_ids: tuple[str, ...] = field(default_factory=tuple)
    incomplete_event_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_complete(self) -> bool:
        return (
            self.games_captured == self.games_expected
            and self.teams_captured == self.teams_expected
            and not self.duplicate_event_ids
            and not self.inconsistent_event_ids
            and not self.incomplete_event_ids
        )


def _raw_snapshot_ref(game: NFLGame) -> RawSnapshotRef:
    return RawSnapshotRef(
        provider="espn",
        path=game.raw_snapshot_path,
        source_url=game.source_url,
        retrieved_at=game.retrieved_at,
        sha256=game.raw_snapshot_sha256,
    )


def _team_id(team: NFLTeam) -> str:
    return stable_record_id("team", "espn", team.espn_team_id)


def _team_record(team: NFLTeam, source: RawSnapshotRef, retrieved_at: datetime) -> Team:
    # A team has no natural "event time" of its own; retrieval time is the
    # closest honest stand-in for "known as of."
    return Team(
        id=_team_id(team),
        provider_ids={"espn": team.espn_team_id},
        event_time=retrieved_at,
        retrieved_at=retrieved_at,
        source_snapshots=(source,),
        abbreviation=team.abbreviation,
        display_name=team.name,
    )


def _game_record(game: NFLGame) -> Game:
    source = _raw_snapshot_ref(game)
    return Game(
        id=stable_record_id("game", "espn", game.event_id),
        provider_ids={"espn": game.event_id},
        event_time=game.kickoff,
        retrieved_at=game.retrieved_at,
        source_snapshots=(source,),
        season_year=game.season_year,
        season_type=game.season_type,
        week=game.week,
        home_team_id=_team_id(game.home_team),
        away_team_id=_team_id(game.away_team),
        kickoff_at=game.kickoff,
        status=game.status,
        completed=game.completed,
        neutral_site=game.neutral_site,
        home_score=game.home_score,
        away_score=game.away_score,
    )


async def fetch_regular_season(
    connector: EspnConnector,
    season_year: int,
    *,
    refresh: bool = False,
) -> list[NFLGame]:
    """Fetch one regular season by iterating explicit weeks.

    ESPN's scoreboard endpoint has no reliable whole-season query: a bare
    ``dates=<year>`` filter is a calendar-year filter, not an NFL-season
    filter. Live testing against 2023 showed it leaking the prior season's
    January tail (349 events, 34 "teams" including Pro Bowl placeholders)
    while still being unable to guarantee coverage of a season's own late
    weeks that fall in the next calendar year. ``games_for_week`` pins
    ``seasontype`` and ``week`` explicitly and was verified correct across
    week 1, 17, and 18 (including the January-dated week 18 games), so
    iterating it directly avoids ``games_for_season``'s ambiguity by
    construction. ``EspnConnector.games_for_season`` itself is not fixed
    here — it is out of this ticket's scope, flagged as a known issue.
    """
    games: list[NFLGame] = []
    for week in REGULAR_SEASON_WEEKS:
        games.extend(
            await connector.games_for_week(
                season_year,
                week,
                season_type=NFLSeasonType.REGULAR,
                refresh=refresh,
            )
        )
    return games


def build_coverage_report(
    games: list[NFLGame],
    season_year: int,
    *,
    require_completed: bool,
) -> tuple[list[NFLGame], SeasonCoverageReport]:
    """Deduplicate and validate a season's games, returning the accepted set and its report.

    Duplicate event IDs and games with inconsistent season/week metadata are
    excluded from the accepted set and listed on the report rather than
    silently kept — "impossible scores" and non-boolean completion states
    are already rejected earlier, inside EspnConnector's normalization.
    """
    accepted: list[NFLGame] = []
    seen_event_ids: set[str] = set()
    duplicate_event_ids: list[str] = []
    inconsistent_event_ids: list[str] = []

    for game in games:
        if game.season_year != season_year or game.season_type != NFLSeasonType.REGULAR:
            inconsistent_event_ids.append(game.event_id)
            continue
        if game.event_id in seen_event_ids:
            duplicate_event_ids.append(game.event_id)
            continue
        seen_event_ids.add(game.event_id)
        accepted.append(game)

    teams = {game.home_team.espn_team_id for game in accepted} | {
        game.away_team.espn_team_id for game in accepted
    }
    incomplete_event_ids = (
        [game.event_id for game in accepted if not game.completed] if require_completed else []
    )

    report = SeasonCoverageReport(
        season_year=season_year,
        require_completed=require_completed,
        games_captured=len(accepted),
        games_expected=EXPECTED_REGULAR_SEASON_GAMES,
        teams_captured=len(teams),
        teams_expected=EXPECTED_TEAM_COUNT,
        duplicate_event_ids=tuple(duplicate_event_ids),
        inconsistent_event_ids=tuple(inconsistent_event_ids),
        incomplete_event_ids=tuple(incomplete_event_ids),
    )
    return accepted, report


def write_season(store: ResearchStore, games: list[NFLGame]) -> None:
    """Convert accepted games to canonical Team/Game records and persist them."""
    teams_by_id: dict[str, Team] = {}
    game_records: list[Game] = []
    for game in games:
        source = _raw_snapshot_ref(game)
        for team in (game.home_team, game.away_team):
            teams_by_id[_team_id(team)] = _team_record(team, source, game.retrieved_at)
        game_records.append(_game_record(game))

    records = [*teams_by_id.values(), *game_records]
    if records:
        store.write(records)


async def ingest_regular_season(
    connector: EspnConnector,
    store: ResearchStore,
    season_year: int,
    *,
    require_completed: bool,
    refresh: bool = False,
) -> SeasonCoverageReport:
    """Ingest one regular season end to end: fetch, validate, persist.

    Raises SeasonCoverageError (carrying the coverage report) rather than
    writing a partial or inconsistent season, so downstream model
    evaluation cannot silently continue on incomplete data.
    """
    raw_games = await fetch_regular_season(connector, season_year, refresh=refresh)
    accepted, report = build_coverage_report(
        raw_games, season_year, require_completed=require_completed
    )
    if not report.is_complete:
        raise SeasonCoverageError(report)
    write_season(store, accepted)
    return report
