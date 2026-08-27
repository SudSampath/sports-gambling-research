from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sgr.connectors.espn import EspnConnector
from sgr.models import NFLGame, NFLSeasonType, NFLTeam
from sgr.research.schemas import Game, RawSnapshotRef, Team, stable_record_id
from sgr.research.storage import ResearchStore

# The NFL has run an 18-week, 17-game-per-team regular season since 2021
# (32 teams x 17 games / 2 = 272). 1999-2020 ran a 17-week, 16-game-per-team
# season (32 x 16 / 2 = 256) -- SUD-122 extends ingestion back to 1999, so
# both eras must be handled explicitly rather than assuming the current one.
SEVENTEEN_GAME_ERA_START_YEAR = 2021

# The Houston Texans joined the league in 2002 as the 32nd franchise; the
# Cleveland Browns' 1999 re-establishment already restored the count to 31
# for 1999-2001. Getting this wrong makes a correct 31-team, 248-game 2000
# or 2001 ingest look like a coverage failure.
THIRTY_TWO_TEAM_ERA_START_YEAR = 2002
EXPECTED_TEAM_COUNT = 32

# One-off historical exceptions to the schedule-derived expected game count,
# by season_year.
# 2001: verified live (SUD-122) that ESPN's site API scoreboard archive
# simply does not return a Dallas-Seattle game anywhere in its 2001
# regular-season weeks -- every other of the 31 teams shows exactly 16
# games played; only DAL and SEA each show 15. This is an absence in ESPN's
# own historical archive, not a filtering bug here (confirmed: the game is
# not present in the raw per-week payloads at all, under any week/status),
# and not recoverable from this data source.
# 2022: the Week 17 Bills-Bengals game was suspended after Damar Hamlin's
# on-field cardiac arrest (2023-01-02) and never resumed; the NFL ruled it
# would not be replayed, so that season's official regular season is 271
# games, not the era-standard 272 -- a real schedule fact, not a
# data-quality defect this ingest should keep failing on.
SEASON_GAME_COUNT_EXCEPTIONS = {2001: 247, 2022: 271}

# ESPN's historical archive keeps a permanent placeholder event, under its
# original event ID, for a game that was rescheduled to a different date (and
# a different event ID) or officially cancelled -- it never transitions to
# STATUS_FINAL. Found live: 2014 week 12 BUF@NYJ (STATUS_POSTPONED, moved to
# Detroit and replayed under a different event ID after the "Snowvember"
# lake-effect blizzard); 2017 week 1 MIA@TB (STATUS_POSTPONED, replayed in
# week 11 under a different event ID after Hurricane Irma); 2022 week 17
# CIN@BUF (STATUS_CANCELED, the Damar Hamlin game, never replayed at all).
# These are excluded from both the captured and expected counts for a
# require_completed pass -- they are not a data-quality defect to keep
# failing on, and the real replayed game (a different event ID) is still
# captured normally.
PERMANENTLY_UNCOMPLETED_STATUSES = frozenset({"STATUS_POSTPONED", "STATUS_CANCELED"})


def expected_regular_season_weeks(season_year: int) -> range:
    return range(1, 19) if season_year >= SEVENTEEN_GAME_ERA_START_YEAR else range(1, 18)


def expected_regular_season_games(season_year: int) -> int:
    if season_year in SEASON_GAME_COUNT_EXCEPTIONS:
        return SEASON_GAME_COUNT_EXCEPTIONS[season_year]
    games_per_team = 17 if season_year >= SEVENTEEN_GAME_ERA_START_YEAR else 16
    return expected_team_count(season_year) * games_per_team // 2


def expected_team_count(season_year: int) -> int:
    return 32 if season_year >= THIRTY_TWO_TEAM_ERA_START_YEAR else 31


# Kept for existing 17-game/32-team-era (2021+) callers/tests; new code
# should call expected_regular_season_games/expected_team_count(season_year)
# so pre-2021/pre-2002 seasons are correct.
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
            f"{len(report.incomplete_event_ids)} incomplete games "
            f"({len(report.rescheduled_or_canceled_event_ids)} rescheduled/canceled excluded)."
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
    rescheduled_or_canceled_event_ids: tuple[str, ...] = field(default_factory=tuple)

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
    for week in expected_regular_season_weeks(season_year):
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
    are already rejected earlier, inside EspnConnector's normalization. A
    game rescheduled to a different date (a different event ID) or
    officially cancelled keeps a permanent STATUS_POSTPONED/STATUS_CANCELED
    placeholder under its original event ID in ESPN's historical archive
    (PERMANENTLY_UNCOMPLETED_STATUSES); for a require_completed pass those
    placeholders are excluded from the accepted set entirely rather than
    counted as a game that failed to complete -- the real game, if replayed,
    is still captured normally under its own event ID.
    """
    accepted: list[NFLGame] = []
    seen_event_ids: set[str] = set()
    duplicate_event_ids: list[str] = []
    inconsistent_event_ids: list[str] = []
    rescheduled_or_canceled_event_ids: list[str] = []

    for game in games:
        if game.season_year != season_year or game.season_type != NFLSeasonType.REGULAR:
            inconsistent_event_ids.append(game.event_id)
            continue
        if game.event_id in seen_event_ids:
            duplicate_event_ids.append(game.event_id)
            continue
        seen_event_ids.add(game.event_id)
        if require_completed and game.status in PERMANENTLY_UNCOMPLETED_STATUSES:
            rescheduled_or_canceled_event_ids.append(game.event_id)
            continue
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
        games_expected=expected_regular_season_games(season_year),
        teams_captured=len(teams),
        teams_expected=expected_team_count(season_year),
        duplicate_event_ids=tuple(duplicate_event_ids),
        inconsistent_event_ids=tuple(inconsistent_event_ids),
        incomplete_event_ids=tuple(incomplete_event_ids),
        rescheduled_or_canceled_event_ids=tuple(rescheduled_or_canceled_event_ids),
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
