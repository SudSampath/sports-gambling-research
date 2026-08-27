from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from sgr.connectors.nflverse import NflverseConnector, NflverseCsvSnapshot
from sgr.research.roster_continuity import normalize_team_abbreviation
from sgr.research.schemas import Team, TeamGameEfficiency, stable_record_id
from sgr.research.storage import ResearchStore

REGULAR_SEASON_TYPE = "REG"

# A play is treated as garbage time if, in the 4th quarter or overtime, the
# score differential already exceeds three scores (21 points) -- a simple,
# documented heuristic (not a claim of precision) matching the AC's
# "garbage-time or low-leverage filtering is configurable and reported."
GARBAGE_TIME_QUARTERS = frozenset({4, 5})
GARBAGE_TIME_SCORE_DIFFERENTIAL_THRESHOLD = 21

EXPLOSIVE_PASS_YARDS = 20
EXPLOSIVE_RUSH_YARDS = 10
REDZONE_YARDLINE_100 = 20
EARLY_DOWNS = frozenset({1, 2})


class PlayLevelFeatureError(RuntimeError):
    """Base error for play-by-play ingestion and aggregation."""


@dataclass(frozen=True)
class SeasonPlayCoverage:
    season_year: int
    plays_in_source: int
    plays_used: int  # REG season_type, resolved teams, resolved canonical game
    unmatched_game_plays: int  # game_id not found in games.csv's espn mapping
    unresolved_team_plays: int  # posteam/defteam abbreviation not a known Team
    cpoe_coverage: int  # plays with a non-empty cpoe among pass plays


@dataclass(frozen=True)
class PlayLevelCoverageReport:
    season_years: tuple[int, ...]
    team_games_written: int
    by_season: dict[int, SeasonPlayCoverage] = field(default_factory=dict)


def _float_or_none(value: str | None) -> float | None:
    text = (value or "").strip()
    if not text or text.upper() == "NA":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _flag(value: str | None) -> bool:
    return (value or "").strip() == "1"


def _mean(values: list[float]) -> Decimal | None:
    if not values:
        return None
    return Decimal(str(round(sum(values) / len(values), 6)))


@dataclass
class _PlayGroup:
    plays: list[dict[str, str]] = field(default_factory=list)


def build_team_game_efficiencies(
    pbp_snapshot: NflverseCsvSnapshot,
    games_snapshot: NflverseCsvSnapshot,
    teams: Iterable[Team],
    season_year: int,
    *,
    garbage_time_excluded: bool,
) -> tuple[tuple[TeamGameEfficiency, ...], SeasonPlayCoverage]:
    """Aggregate one season's play-by-play into team-game efficiency records.

    Each canonical game_id is resolved via nflverse's own games.csv espn
    column (the same identity join ClosingLine uses), not by re-deriving it
    from pbp's own game_id/old_game_id fields, which are not ESPN event
    IDs. Rows whose game_id has no espn mapping, or whose posteam/defteam
    does not resolve to a known Team, are excluded and counted rather than
    silently dropped or guessed at.
    """
    game_id_to_espn = {
        row.get("game_id"): row.get("espn")
        for row in games_snapshot.rows
        if row.get("game_id") and row.get("espn")
    }
    teams_by_abbreviation = {normalize_team_abbreviation(team.abbreviation): team for team in teams}

    plays_in_source = 0
    unmatched_game_plays = 0
    unresolved_team_plays = 0
    cpoe_eligible = 0
    cpoe_present = 0
    groups: dict[tuple[str, str], _PlayGroup] = {}
    game_weeks: dict[str, int] = {}
    opponents: dict[tuple[str, str], str] = {}

    for row in pbp_snapshot.rows:
        try:
            row_season = int((row.get("season") or "").strip())
        except ValueError:
            continue
        if row_season != season_year or (row.get("season_type") or "").strip().upper() != REGULAR_SEASON_TYPE:
            continue
        plays_in_source += 1

        nflverse_game_id = row.get("game_id")
        espn_id = game_id_to_espn.get(nflverse_game_id or "")
        if not espn_id:
            unmatched_game_plays += 1
            continue

        posteam = normalize_team_abbreviation(row.get("posteam") or "")
        defteam = normalize_team_abbreviation(row.get("defteam") or "")
        if posteam not in teams_by_abbreviation or defteam not in teams_by_abbreviation:
            unresolved_team_plays += 1
            continue

        # Coverage counters below reflect every resolvable play regardless
        # of the garbage-time filter, so the season's field-coverage report
        # doesn't change depending on which variant happens to run last.
        if _flag(row.get("pass")):
            cpoe_eligible += 1
            if _float_or_none(row.get("cpoe")) is not None:
                cpoe_present += 1

        if garbage_time_excluded:
            qtr = _float_or_none(row.get("qtr"))
            score_differential = _float_or_none(row.get("score_differential"))
            if (
                qtr is not None
                and score_differential is not None
                and int(qtr) in GARBAGE_TIME_QUARTERS
                and abs(score_differential) >= GARBAGE_TIME_SCORE_DIFFERENTIAL_THRESHOLD
            ):
                continue

        game_id = stable_record_id("game", "espn", espn_id)
        key = (game_id, posteam)
        groups.setdefault(key, _PlayGroup()).plays.append(row)
        opponents[key] = defteam
        try:
            game_weeks[game_id] = int((row.get("week") or "").strip())
        except ValueError:
            pass

    retrieved_at = max(pbp_snapshot.source.retrieved_at, games_snapshot.source.retrieved_at)
    sources = (pbp_snapshot.source, games_snapshot.source)

    records: list[TeamGameEfficiency] = []
    for (game_id, team_abbr), group in sorted(groups.items()):
        plays = group.plays
        offense_plays = [p for p in plays if _flag(p.get("pass")) or _flag(p.get("rush"))]
        pass_plays = [p for p in plays if _flag(p.get("pass"))]
        rush_plays = [p for p in plays if _flag(p.get("rush"))]
        early_down_plays = [p for p in offense_plays if (_float_or_none(p.get("down")) or 0) in EARLY_DOWNS]
        redzone_plays = [
            p
            for p in offense_plays
            if (yardline := _float_or_none(p.get("yardline_100"))) is not None and yardline <= REDZONE_YARDLINE_100
        ]
        special_teams_plays = [p for p in plays if _flag(p.get("special_teams_play"))]

        def _epa(rows: list[dict[str, str]]) -> Decimal | None:
            values = [v for p in rows if (v := _float_or_none(p.get("epa"))) is not None]
            return _mean(values)

        def _success_rate(rows: list[dict[str, str]]) -> Decimal | None:
            if not rows:
                return None
            values = [1.0 if _flag(p.get("success")) else 0.0 for p in rows]
            return _mean(values)

        cpoe_values = [
            v for p in pass_plays if (v := _float_or_none(p.get("cpoe"))) is not None
        ]

        team = teams_by_abbreviation[team_abbr]
        opponent = teams_by_abbreviation[opponents[(game_id, team_abbr)]]
        records.append(
            TeamGameEfficiency(
                id=stable_record_id(
                    "team_game_efficiency", game_id, team.id, str(garbage_time_excluded)
                ),
                provider_ids={"nflverse": f"{game_id}:{team_abbr}:{garbage_time_excluded}"},
                event_time=retrieved_at,
                retrieved_at=retrieved_at,
                source_snapshots=sources,
                game_id=game_id,
                team_id=team.id,
                opponent_team_id=opponent.id,
                season_year=season_year,
                week=game_weeks.get(game_id, 1),
                garbage_time_excluded=garbage_time_excluded,
                offense_plays=len(offense_plays),
                offense_epa_per_play=_epa(offense_plays),
                offense_success_rate=_success_rate(offense_plays),
                pass_plays=len(pass_plays),
                pass_epa_per_play=_epa(pass_plays),
                pass_success_rate=_success_rate(pass_plays),
                completions=sum(1 for p in pass_plays if _flag(p.get("complete_pass"))),
                cpoe=_mean(cpoe_values),
                rush_plays=len(rush_plays),
                rush_epa_per_play=_epa(rush_plays),
                rush_success_rate=_success_rate(rush_plays),
                early_down_plays=len(early_down_plays),
                early_down_epa_per_play=_epa(early_down_plays),
                early_down_success_rate=_success_rate(early_down_plays),
                explosive_pass_plays=sum(
                    1 for p in pass_plays if (_float_or_none(p.get("yards_gained")) or 0) >= EXPLOSIVE_PASS_YARDS
                ),
                explosive_rush_plays=sum(
                    1 for p in rush_plays if (_float_or_none(p.get("yards_gained")) or 0) >= EXPLOSIVE_RUSH_YARDS
                ),
                redzone_plays=len(redzone_plays),
                redzone_touchdowns=sum(1 for p in redzone_plays if _flag(p.get("touchdown"))),
                sacks_taken=sum(1 for p in pass_plays if _flag(p.get("sack"))),
                special_teams_plays=len(special_teams_plays),
                special_teams_epa_per_play=_epa(special_teams_plays),
            )
        )

    coverage = SeasonPlayCoverage(
        season_year=season_year,
        plays_in_source=plays_in_source,
        plays_used=plays_in_source - unmatched_game_plays - unresolved_team_plays,
        unmatched_game_plays=unmatched_game_plays,
        unresolved_team_plays=unresolved_team_plays,
        cpoe_coverage=cpoe_present if cpoe_eligible else 0,
    )
    return tuple(records), coverage


async def ingest_play_level_features(
    connector: NflverseConnector,
    store: ResearchStore,
    season_years: Iterable[int],
    *,
    garbage_time_excluded_variants: tuple[bool, ...] = (False, True),
    refresh: bool = False,
) -> PlayLevelCoverageReport:
    """Fetch, aggregate, and persist team-game efficiency for the requested
    seasons, one unfiltered pass and one garbage-time-excluded pass by
    default (both kept, per the AC's "unfiltered aggregates remain
    available for ablation")."""
    games_snapshot = await connector.games(refresh=refresh)
    teams = [team for team in store.load_all("team") if isinstance(team, Team)]

    by_season: dict[int, SeasonPlayCoverage] = {}
    total_written = 0
    for season_year in season_years:
        pbp_snapshot = await connector.play_by_play(season_year, refresh=refresh)
        for variant in garbage_time_excluded_variants:
            records, coverage = build_team_game_efficiencies(
                pbp_snapshot, games_snapshot, teams, season_year, garbage_time_excluded=variant
            )
            if records:
                store.write(records)
                total_written += len(records)
            by_season[season_year] = coverage

    return PlayLevelCoverageReport(
        season_years=tuple(sorted(by_season)), team_games_written=total_written, by_season=by_season
    )
