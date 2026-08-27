from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Iterable, Literal

from sgr.connectors.nflverse import NflverseConnector, NflverseCsvSnapshot
from sgr.research.schemas import ClosingLine, Game, stable_record_id
from sgr.research.storage import ResearchStore

REGULAR_SEASON_GAME_TYPE = "REG"
MIN_SEASON_YEAR = 1999


class ClosingLineError(RuntimeError):
    """Base error for closing-line ingestion."""


class ClosingLineRangeError(ClosingLineError):
    """Raised for an invalid requested season range."""


@dataclass(frozen=True)
class SeasonCoverage:
    """Field coverage for one season's REG-season closing lines."""

    season_year: int
    games_in_source: int
    spread_coverage: int
    total_coverage: int
    moneyline_coverage: int


@dataclass(frozen=True)
class ClosingLineCoverageReport:
    season_years: tuple[int, ...]
    games_written: int
    matched_to_local_game: int
    unmatched_espn_ids: tuple[str, ...]
    by_season: dict[int, SeasonCoverage] = field(default_factory=dict)


def _decimal_or_none(value: str | None) -> Decimal | None:
    text = (value or "").strip()
    if not text or text.upper() == "NA":
        return None
    try:
        return Decimal(text)
    except InvalidOperation as error:
        raise ClosingLineError(f"Non-numeric closing-line value: {value!r}.") from error


def _int_or_none(value: str | None) -> int | None:
    text = (value or "").strip()
    if not text or text.upper() == "NA":
        return None
    try:
        # nflverse moneylines are integers stored as plain text (e.g. "-148");
        # float() first tolerates a stray ".0" without accepting fractional odds.
        return int(float(text))
    except ValueError as error:
        raise ClosingLineError(f"Non-numeric moneyline value: {value!r}.") from error


def build_closing_lines(
    games_snapshot: NflverseCsvSnapshot,
    season_years: Iterable[int],
    *,
    existing_game_ids: frozenset[str] = frozenset(),
) -> tuple[tuple[ClosingLine, ...], ClosingLineCoverageReport]:
    """Normalize nflverse's whole-history games.csv into ClosingLine records.

    Each row is joined to a canonical Game by recomputing the same
    ``stable_record_id("game", "espn", <espn event id>)`` that ESPN
    ingestion (SUD-23/35) assigns to that game -- an exact-identity join,
    not a fuzzy team/week match, since nflverse's own ``espn`` column
    carries the same ESPN event ID. This lets closing lines be written for
    seasons before their ESPN Game records exist locally (only 2023-2026
    are ingested as of SUD-119); the join simply resolves once older
    seasons are backfilled (tracked under SUD-122's expanded historical
    dataset, not repeated here). ``existing_game_ids`` is used only to
    report how many written lines already resolve to a local Game -- it
    never gates whether a ClosingLine is written.

    Only REG-season_type rows are accepted, matching the "completed
    regular-season games" scope. Rows with no espn ID are excluded and
    listed by their nflverse game_id rather than silently dropped.
    """

    requested_years = sorted(set(season_years))
    if any(year < MIN_SEASON_YEAR for year in requested_years):
        raise ClosingLineRangeError(f"Season years must be >= {MIN_SEASON_YEAR}.")

    records: list[ClosingLine] = []
    unmatched_espn_ids: list[str] = []
    coverage_counts: dict[int, dict[str, int]] = {
        year: {"games": 0, "spread": 0, "total": 0, "moneyline": 0} for year in requested_years
    }
    matched_to_local_game = 0

    for row in games_snapshot.rows:
        if row.get("game_type", "").strip().upper() != REGULAR_SEASON_GAME_TYPE:
            continue
        try:
            season_year = int((row.get("season") or "").strip())
        except ValueError:
            continue
        if season_year not in coverage_counts:
            continue
        espn_id = (row.get("espn") or "").strip()
        if not espn_id:
            unmatched_espn_ids.append(row.get("game_id") or "<unknown>")
            continue

        counts = coverage_counts[season_year]
        counts["games"] += 1

        home_spread = _decimal_or_none(row.get("spread_line"))
        total_points = _decimal_or_none(row.get("total_line"))
        home_moneyline = _int_or_none(row.get("home_moneyline"))
        away_moneyline = _int_or_none(row.get("away_moneyline"))
        if home_spread is not None:
            counts["spread"] += 1
        if total_points is not None:
            counts["total"] += 1
        if home_moneyline is not None and away_moneyline is not None:
            counts["moneyline"] += 1

        game_id = stable_record_id("game", "espn", espn_id)
        if game_id in existing_game_ids:
            matched_to_local_game += 1
        records.append(
            ClosingLine(
                id=stable_record_id("closing_line", "nflverse", espn_id),
                provider_ids={"nflverse": row.get("game_id") or espn_id, "espn": espn_id},
                event_time=games_snapshot.source.retrieved_at,
                retrieved_at=games_snapshot.source.retrieved_at,
                source_snapshots=(games_snapshot.source,),
                game_id=game_id,
                season_year=season_year,
                home_spread=home_spread,
                total_points=total_points,
                home_moneyline=home_moneyline,
                away_moneyline=away_moneyline,
            )
        )

    report = ClosingLineCoverageReport(
        season_years=tuple(requested_years),
        games_written=len(records),
        matched_to_local_game=matched_to_local_game,
        unmatched_espn_ids=tuple(unmatched_espn_ids),
        by_season={
            year: SeasonCoverage(
                season_year=year,
                games_in_source=counts["games"],
                spread_coverage=counts["spread"],
                total_coverage=counts["total"],
                moneyline_coverage=counts["moneyline"],
            )
            for year, counts in sorted(coverage_counts.items())
        },
    )
    return tuple(records), report


async def ingest_closing_lines(
    connector: NflverseConnector,
    store: ResearchStore,
    season_years: Iterable[int],
    *,
    refresh: bool = False,
) -> ClosingLineCoverageReport:
    """Fetch, normalize, and persist closing lines for the requested seasons."""

    games_snapshot = await connector.games(refresh=refresh)
    existing_game_ids = frozenset(
        game.id for game in store.load_all("game") if isinstance(game, Game)
    )
    records, report = build_closing_lines(
        games_snapshot, season_years, existing_game_ids=existing_game_ids
    )
    if records:
        store.write(records)
    return report


CoverOutcome = Literal["home_covered", "away_covered", "push", "unavailable"]


def home_cover_outcome(closing_line: ClosingLine, actual_home_margin: int) -> CoverOutcome:
    """Grade the closing spread against the realized margin.

    A push (the actual margin exactly equals the spread) is its own
    outcome, never coerced into a win or a loss for either side -- this is
    the fixture-tested behavior the AC requires for pushes and ties.
    """

    if closing_line.home_spread is None:
        return "unavailable"
    if Decimal(actual_home_margin) == closing_line.home_spread:
        return "push"
    if Decimal(actual_home_margin) > closing_line.home_spread:
        return "home_covered"
    return "away_covered"
