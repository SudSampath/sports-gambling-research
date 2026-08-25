from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime

from sgr.models import NFLSeasonType
from sgr.research.nfl_divisions import (
    CONFERENCES,
    DIVISION_WINNERS_PER_CONFERENCE,
    DIVISIONS_PER_CONFERENCE,
    WILDCARDS_PER_CONFERENCE,
    UnknownTeamAbbreviationError,
    division_of,
)
from sgr.research.pythagorean import DEFAULT_EXPONENT, InsufficientHistoryError, generate_forecast
from sgr.research.schemas import Game, Team
from sgr.research.storage import ResearchStore

DEFAULT_N_SIMULATIONS = 2000
DEFAULT_SIMULATION_SEED = 20260824

TIEBREAK_SIMPLIFICATION_NOTE = (
    "Simplified tiebreaker: ties are broken by head-to-head record when exactly two teams are "
    "tied and have played each other, otherwise by a seeded random draw. This is NOT the official "
    "multi-step NFL tiebreaker procedure (strength of victory/schedule, common games, conference "
    "record, net points, etc.) -- explicitly documented here as a simplification, per SUD-106's scope."
)


class SeasonSimulationError(RuntimeError):
    """Base error for season simulation."""


@dataclass(frozen=True)
class _TeamMeta:
    team_id: str
    abbreviation: str
    conference: str
    division: str


def _load_team_metadata(store: ResearchStore, team_ids: set[str]) -> dict[str, _TeamMeta]:
    teams = {t.id: t for t in store.load_all("team") if isinstance(t, Team)}
    meta: dict[str, _TeamMeta] = {}
    for team_id in team_ids:
        team = teams.get(team_id)
        if team is None:
            raise SeasonSimulationError(f"No team record found for {team_id}.")
        try:
            conference, division = division_of(team.abbreviation)
        except UnknownTeamAbbreviationError as error:
            raise SeasonSimulationError(str(error)) from error
        meta[team_id] = _TeamMeta(team_id, team.abbreviation, conference, division)
    return meta


@dataclass
class _Record:
    wins: int = 0
    losses: int = 0
    ties: int = 0

    @property
    def games_played(self) -> int:
        return self.wins + self.losses + self.ties

    @property
    def win_pct(self) -> float:
        played = self.games_played
        return (self.wins + 0.5 * self.ties) / played if played else 0.0


def _base_records_and_h2h(
    season_games: list[Game], as_of: datetime
) -> tuple[dict[str, _Record], dict[frozenset, str | None]]:
    """Deterministic contribution of already-completed games -- identical
    across every simulated run, computed once rather than N times."""
    records: dict[str, _Record] = {}
    head_to_head: dict[frozenset, str | None] = {}
    for game in season_games:
        if not (game.completed and game.kickoff_at < as_of):
            continue
        home, away = game.home_team_id, game.away_team_id
        records.setdefault(home, _Record())
        records.setdefault(away, _Record())
        if game.home_score == game.away_score:
            records[home].ties += 1
            records[away].ties += 1
            head_to_head[frozenset((home, away))] = None
        elif game.home_score > game.away_score:
            records[home].wins += 1
            records[away].losses += 1
            head_to_head[frozenset((home, away))] = home
        else:
            records[away].wins += 1
            records[home].losses += 1
            head_to_head[frozenset((home, away))] = away
    return records, head_to_head


def _remaining_game_probabilities(
    store: ResearchStore, season_games: list[Game], as_of: datetime, exponent: float
) -> list[tuple[str, str, str, float]]:
    """(game_id, home_team_id, away_team_id, home_win_probability) for every
    game not yet completed as of as_of. Frozen per game for the whole
    simulation -- each run draws an independent outcome from this same
    probability, matching the AC; team strength is not re-estimated
    mid-simulation from earlier simulated results in the same run."""
    remaining = []
    for game in season_games:
        if game.completed and game.kickoff_at < as_of:
            continue
        try:
            forecast = generate_forecast(store, game.id, feature_cutoff_at=as_of, exponent=exponent)
            probability = float(forecast.home_win_probability)
        except InsufficientHistoryError:
            probability = 0.5
        remaining.append((game.id, game.home_team_id, game.away_team_id, probability))
    return remaining


def _simulate_remaining_outcomes(
    remaining_games: list[tuple[str, str, str, float]], n_simulations: int, seed: int
) -> list[dict[str, str]]:
    """N independent draws over the remaining games, one shared seeded RNG
    advanced sequentially across all runs -- reproducible: the same seed and
    same remaining_games always produce the same sequence of draws."""
    rng = random.Random(seed)
    runs = []
    for _ in range(n_simulations):
        run = {}
        for game_id, home_id, away_id, probability in remaining_games:
            run[game_id] = home_id if rng.random() < probability else away_id
        runs.append(run)
    return runs


def _rank_with_tiebreak(
    team_ids: list[str], records: dict[str, _Record], head_to_head: dict[frozenset, str | None], rng: random.Random
) -> list[str]:
    groups: dict[float, list[str]] = {}
    for team_id in team_ids:
        groups.setdefault(records[team_id].win_pct, []).append(team_id)

    ranked: list[str] = []
    for win_pct in sorted(groups, reverse=True):
        group = sorted(groups[win_pct])
        if len(group) == 1:
            ranked.extend(group)
            continue
        if len(group) == 2:
            winner = head_to_head.get(frozenset(group))
            if winner is not None:
                loser = group[0] if winner == group[1] else group[1]
                ranked.extend([winner, loser])
                continue
        shuffled = list(group)
        rng.shuffle(shuffled)
        ranked.extend(shuffled)
    return ranked


@dataclass(frozen=True)
class TeamSimulationResult:
    team_id: str
    abbreviation: str
    conference: str
    division: str
    win_total_p10: float
    win_total_p25: float
    win_total_p50: float
    win_total_p75: float
    win_total_p90: float
    mean_win_total: float
    division_win_probability: float
    playoff_probability: float


@dataclass(frozen=True)
class SeasonSimulationReport:
    season_year: int
    as_of: datetime
    n_simulations: int
    seed: int
    tiebreaker_note: str
    team_results: tuple[TeamSimulationResult, ...]


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, max(0, round((len(sorted_values) - 1) * pct / 100)))
    return sorted_values[idx]


def simulate_season(
    store: ResearchStore,
    season_year: int,
    *,
    as_of: datetime,
    n_simulations: int = DEFAULT_N_SIMULATIONS,
    seed: int = DEFAULT_SIMULATION_SEED,
    exponent: float = DEFAULT_EXPONENT,
) -> SeasonSimulationReport:
    """Seeded, reproducible full-league Monte Carlo simulation: each run
    draws an independent random outcome per remaining game from its current
    point-in-time forecast probability, and tracks each team's simulated win
    total, division-win, and playoff qualification across all runs.
    """
    all_games = [g for g in store.load_all("game") if isinstance(g, Game)]
    season_games = [g for g in all_games if g.season_type == NFLSeasonType.REGULAR and g.season_year == season_year]
    if not season_games:
        raise SeasonSimulationError(f"No regular-season games found in storage for {season_year}.")

    team_ids = sorted({g.home_team_id for g in season_games} | {g.away_team_id for g in season_games})
    meta = _load_team_metadata(store, set(team_ids))

    base_records, base_h2h = _base_records_and_h2h(season_games, as_of)
    remaining_games = _remaining_game_probabilities(store, season_games, as_of, exponent)
    runs = _simulate_remaining_outcomes(remaining_games, n_simulations, seed)

    win_totals: dict[str, list[float]] = {team_id: [] for team_id in team_ids}
    division_win_counts: dict[str, int] = {team_id: 0 for team_id in team_ids}
    playoff_counts: dict[str, int] = {team_id: 0 for team_id in team_ids}

    tiebreak_rng = random.Random(seed)
    for run in runs:
        records = {team_id: _Record(base_records[team_id].wins, base_records[team_id].losses, base_records[team_id].ties)
                   if team_id in base_records else _Record() for team_id in team_ids}
        head_to_head = dict(base_h2h)
        for game_id, home_id, away_id, _probability in remaining_games:
            winner = run[game_id]
            loser = away_id if winner == home_id else home_id
            records[winner].wins += 1
            records[loser].losses += 1
            head_to_head[frozenset((home_id, away_id))] = winner

        for team_id in team_ids:
            win_totals[team_id].append(records[team_id].wins + 0.5 * records[team_id].ties)

        for conference in CONFERENCES:
            conference_teams = [t for t in team_ids if meta[t].conference == conference]
            division_winners: list[str] = []
            for division in DIVISIONS_PER_CONFERENCE:
                division_teams = [t for t in conference_teams if meta[t].division == division]
                ranked = _rank_with_tiebreak(division_teams, records, head_to_head, tiebreak_rng)
                division_winners.append(ranked[0])
            for team_id in division_winners:
                division_win_counts[team_id] += 1

            remaining_conference_teams = [t for t in conference_teams if t not in division_winners]
            ranked_remaining = _rank_with_tiebreak(remaining_conference_teams, records, head_to_head, tiebreak_rng)
            wildcards = ranked_remaining[:WILDCARDS_PER_CONFERENCE]

            for team_id in division_winners + wildcards:
                playoff_counts[team_id] += 1

    team_results = []
    for team_id in team_ids:
        totals = sorted(win_totals[team_id])
        team_results.append(
            TeamSimulationResult(
                team_id=team_id,
                abbreviation=meta[team_id].abbreviation,
                conference=meta[team_id].conference,
                division=meta[team_id].division,
                win_total_p10=_percentile(totals, 10),
                win_total_p25=_percentile(totals, 25),
                win_total_p50=_percentile(totals, 50),
                win_total_p75=_percentile(totals, 75),
                win_total_p90=_percentile(totals, 90),
                mean_win_total=sum(totals) / len(totals) if totals else 0.0,
                division_win_probability=division_win_counts[team_id] / n_simulations,
                playoff_probability=playoff_counts[team_id] / n_simulations,
            )
        )
    team_results.sort(key=lambda r: r.playoff_probability, reverse=True)

    return SeasonSimulationReport(
        season_year=season_year,
        as_of=as_of,
        n_simulations=n_simulations,
        seed=seed,
        tiebreaker_note=TIEBREAK_SIMPLIFICATION_NOTE,
        team_results=tuple(team_results),
    )


@dataclass(frozen=True)
class GameOutcomeSpec:
    game_id: str
    winner_team_id: str
    description: str = ""


@dataclass(frozen=True)
class CombinedOutcomeResult:
    outcomes: tuple[GameOutcomeSpec, ...]
    n_simulations: int
    seed: int
    joint_probability: float
    fair_decimal_odds: float | None
    label: str


COMBINED_OUTCOME_LABEL = (
    "Research/calibration output: the model's estimated joint probability and implied fair "
    "(breakeven) odds for this exact scenario. Not a recommendation, tip, pick, or ranked list of "
    "favorable combinations -- see docs/PRD.md's Responsible use section."
)


def combined_outcome_probability(
    store: ResearchStore,
    season_year: int,
    outcomes: list[GameOutcomeSpec],
    *,
    as_of: datetime,
    n_simulations: int = DEFAULT_N_SIMULATIONS,
    seed: int = DEFAULT_SIMULATION_SEED,
    exponent: float = DEFAULT_EXPONENT,
) -> CombinedOutcomeResult:
    """The model's estimated joint probability of a user-specified set of
    exact game outcomes, and the implied fair/breakeven combined odds.

    Uses the same seeded simulation draws as simulate_season (same
    remaining-game probabilities, same RNG discipline) rather than a
    separate closed-form product, so this stays consistent with the rest of
    the module and with any future version that adds cross-game
    correlation; under the current model (each remaining game an
    independent Bernoulli draw from its own frozen forecast) this Monte
    Carlo estimate converges to the exact product of each specified game's
    win probability as n_simulations grows.
    """
    if not outcomes:
        raise SeasonSimulationError("At least one game outcome is required.")

    all_games = [g for g in store.load_all("game") if isinstance(g, Game)]
    games_by_id = {g.id: g for g in all_games if g.season_type == NFLSeasonType.REGULAR and g.season_year == season_year}
    for spec in outcomes:
        game = games_by_id.get(spec.game_id)
        if game is None:
            raise SeasonSimulationError(f"Game {spec.game_id} is not a {season_year} regular-season game.")
        if spec.winner_team_id not in (game.home_team_id, game.away_team_id):
            raise SeasonSimulationError(f"{spec.winner_team_id} is not a participant in game {spec.game_id}.")

    season_games = list(games_by_id.values())
    remaining_games = _remaining_game_probabilities(store, season_games, as_of, exponent)
    runs = _simulate_remaining_outcomes(remaining_games, n_simulations, seed)

    completed_winner: dict[str, str | None] = {}
    for spec in outcomes:
        game = games_by_id[spec.game_id]
        if game.completed and game.kickoff_at < as_of:
            if game.home_score == game.away_score:
                completed_winner[spec.game_id] = None
            else:
                completed_winner[spec.game_id] = game.home_team_id if game.home_score > game.away_score else game.away_team_id

    matches = 0
    for run in runs:
        all_match = True
        for spec in outcomes:
            if spec.game_id in completed_winner:
                actual = completed_winner[spec.game_id]
                if actual != spec.winner_team_id:
                    all_match = False
                    break
            else:
                if run.get(spec.game_id) != spec.winner_team_id:
                    all_match = False
                    break
        if all_match:
            matches += 1

    probability = matches / n_simulations
    fair_odds = (1.0 / probability) if probability > 0 else None

    return CombinedOutcomeResult(
        outcomes=tuple(outcomes),
        n_simulations=n_simulations,
        seed=seed,
        joint_probability=probability,
        fair_decimal_odds=fair_odds,
        label=COMBINED_OUTCOME_LABEL,
    )
