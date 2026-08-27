from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from sgr.connectors.nflverse import NflverseConnector, NflverseCsvSnapshot
from sgr.research.schemas import GameContext, stable_record_id
from sgr.research.storage import ResearchStore

REGULAR_SEASON_GAME_TYPE = "REG"
MIN_SEASON_YEAR = 1999

VALID_ROOF_VALUES = frozenset({"outdoors", "dome", "closed", "open"})


class GameContextError(RuntimeError):
    """Base error for game-context ingestion."""


@dataclass(frozen=True)
class SeasonContextCoverage:
    season_year: int
    games_in_source: int
    rest_coverage: int
    roof_coverage: int
    surface_coverage: int
    observed_weather_coverage: int


@dataclass(frozen=True)
class GameContextCoverageReport:
    season_years: tuple[int, ...]
    games_written: int
    unmatched_espn_ids: tuple[str, ...]
    by_season: dict[int, SeasonContextCoverage] = field(default_factory=dict)


def _int_or_none(value: str | None) -> int | None:
    text = (value or "").strip()
    if not text or text.upper() == "NA":
        return None
    try:
        return int(float(text))
    except ValueError as error:
        raise GameContextError(f"Non-numeric game-context value: {value!r}.") from error


def build_game_contexts(
    games_snapshot: NflverseCsvSnapshot,
    season_years: Iterable[int],
) -> tuple[tuple[GameContext, ...], GameContextCoverageReport]:
    """Normalize nflverse's whole-history games.csv into GameContext records.

    Joined to the canonical Game the same exact-identity way ClosingLine
    (SUD-119) is: recomputing stable_record_id("game","espn",<espn_id>)
    from nflverse's own espn column, not a fuzzy team/week match. Rows
    with no espn ID, or with a rest value nflverse itself did not supply
    (both are effectively unseen in practice; nflverse computes rest for
    every scheduled game), are excluded and counted rather than guessed.
    """
    requested_years = sorted(set(season_years))
    if any(year < MIN_SEASON_YEAR for year in requested_years):
        raise GameContextError(f"Season years must be >= {MIN_SEASON_YEAR}.")

    records: list[GameContext] = []
    unmatched_espn_ids: list[str] = []
    coverage_counts: dict[int, dict[str, int]] = {
        year: {"games": 0, "rest": 0, "roof": 0, "surface": 0, "weather": 0} for year in requested_years
    }

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

        home_rest = _int_or_none(row.get("home_rest"))
        away_rest = _int_or_none(row.get("away_rest"))
        if home_rest is None or away_rest is None:
            continue  # cannot build a record without both rest values

        roof_raw = (row.get("roof") or "").strip().lower()
        roof = roof_raw if roof_raw in VALID_ROOF_VALUES else None
        surface_raw = (row.get("surface") or "").strip().lower()
        surface = surface_raw or None
        observed_temp = _int_or_none(row.get("temp"))
        observed_wind = _int_or_none(row.get("wind"))

        counts["rest"] += 1
        if roof is not None:
            counts["roof"] += 1
        if surface is not None:
            counts["surface"] += 1
        if observed_temp is not None and observed_wind is not None:
            counts["weather"] += 1

        game_id = stable_record_id("game", "espn", espn_id)
        records.append(
            GameContext(
                id=stable_record_id("game_context", "nflverse", espn_id),
                provider_ids={"nflverse": row.get("game_id") or espn_id, "espn": espn_id},
                event_time=games_snapshot.source.retrieved_at,
                retrieved_at=games_snapshot.source.retrieved_at,
                source_snapshots=(games_snapshot.source,),
                game_id=game_id,
                season_year=season_year,
                home_rest_days=home_rest,
                away_rest_days=away_rest,
                divisional_game=(row.get("div_game") or "").strip() == "1",
                roof=roof,
                surface=surface,
                observed_temp_fahrenheit=observed_temp,
                observed_wind_mph=observed_wind,
            )
        )

    report = GameContextCoverageReport(
        season_years=tuple(requested_years),
        games_written=len(records),
        unmatched_espn_ids=tuple(unmatched_espn_ids),
        by_season={
            year: SeasonContextCoverage(
                season_year=year,
                games_in_source=counts["games"],
                rest_coverage=counts["rest"],
                roof_coverage=counts["roof"],
                surface_coverage=counts["surface"],
                observed_weather_coverage=counts["weather"],
            )
            for year, counts in sorted(coverage_counts.items())
        },
    )
    return tuple(records), report


async def ingest_game_contexts(
    connector: NflverseConnector,
    store: ResearchStore,
    season_years: Iterable[int],
    *,
    refresh: bool = False,
) -> GameContextCoverageReport:
    games_snapshot = await connector.games(refresh=refresh)
    records, report = build_game_contexts(games_snapshot, season_years)
    if records:
        store.write(records)
    return report
