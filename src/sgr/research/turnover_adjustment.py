from __future__ import annotations

from datetime import datetime, timedelta

from sgr.research.pythagorean import (
    InsufficientHistoryError,
    InvalidScoringInputError,
    apply_home_field,
    blended_points_per_game,
    combine_win_probabilities_log5,
    compute_team_strength,
    pythagorean_win_pct,
)
from sgr.research.schemas import Game, PlayerGameStatline
from sgr.research.storage import ResearchStore

FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF = 24

# Calibrated by calibrate_points_per_turnover_margin(store, [2023, 2024]) --
# the closed-form OLS slope of (actual margin - blended pre-turnover margin)
# against (turnover-margin-per-game differential), fit on the 2023-2024
# training fold only. See the SUD-110 PR for the out-of-sample 2025
# comparison against the unadjusted baseline. Not copied unchecked from the
# externally cited ~2.4-points figure -- this project's own data gets its
# own fit, same discipline calibrate_home_field_margin_points (SUD-105)
# already established.
DEFAULT_POINTS_PER_TURNOVER_MARGIN = 1.281207368865855


def build_turnovers_committed_index(statlines: list[PlayerGameStatline]) -> dict[tuple[str, str], int]:
    """{(game_id, team_id): turnovers that team gave away in that game} --
    interceptions its passers threw plus fumbles it lost, the two
    categories the ESPN boxscore data actually tracks (SUD-91/93). Fumbles
    recovered by the team's own offense are correctly excluded (only the
    "LOST" label counts). Built once per caller and reused across every
    game/team lookup -- a per-lookup scan over the full statline table
    (tens of thousands of rows) does not scale to a walk-forward evaluation
    or a training-fold calibration over hundreds of games.
    """
    index: dict[tuple[str, str], int] = {}
    for statline in statlines:
        key = (statline.game_id, statline.team_id)
        if statline.stat_category == "passing" and "INT" in statline.stat_labels:
            value = statline.stat_values[statline.stat_labels.index("INT")]
        elif statline.stat_category == "fumbles" and "LOST" in statline.stat_labels:
            value = statline.stat_values[statline.stat_labels.index("LOST")]
        else:
            continue
        try:
            index[key] = index.get(key, 0) + int(value)
        except ValueError:
            continue
    return index


def turnover_margin_for_game(index: dict[tuple[str, str], int], game: Game) -> tuple[int, int]:
    """(home_margin, away_margin) for one game -- each team's takeaways
    (the opponent's committed turnovers) minus its own giveaways. Always
    sums to zero by construction, since one team's giveaway is the other's
    takeaway."""
    home_committed = index.get((game.id, game.home_team_id), 0)
    away_committed = index.get((game.id, game.away_team_id), 0)
    return away_committed - home_committed, home_committed - away_committed


def turnover_margin_per_game(
    index: dict[tuple[str, str], int], team_games: list[Game], team_id: str,
) -> float | None:
    """A team's average turnover margin per game across the given
    (already-filtered-to-completed-and-eligible) games. Current season
    only, by construction of the caller's team_games -- unlike scoring,
    a fresh season's turnover luck has no reason to carry over from the
    prior one, so there is nothing to shrink toward."""
    if not team_games:
        return None
    total = 0
    for game in team_games:
        home_margin, away_margin = turnover_margin_for_game(index, game)
        total += home_margin if game.home_team_id == team_id else away_margin
    return total / len(team_games)


def _team_games_before(all_games: list[Game], team_id: str, season_year: int, feature_cutoff_at: datetime) -> list[Game]:
    return [
        g
        for g in all_games
        if g.completed
        and g.season_year == season_year
        and g.kickoff_at < feature_cutoff_at
        and (g.home_team_id == team_id or g.away_team_id == team_id)
    ]


def _blended_pf_pa(strength) -> tuple[float, float]:
    blended_for = blended_points_per_game(
        strength.current_points_for, strength.current_games_played,
        strength.prior_points_for, strength.prior_games_played,
    )
    blended_against = blended_points_per_game(
        strength.current_points_against, strength.current_games_played,
        strength.prior_points_against, strength.prior_games_played,
    )
    return (blended_for or 0.0), (blended_against or 0.0)


def turnover_normalized_probability(
    all_games: list[Game],
    turnovers_index: dict[tuple[str, str], int],
    home_team_id: str,
    away_team_id: str,
    season_year: int,
    feature_cutoff_at: datetime,
    *,
    neutral_site: bool | None,
    exponent: float,
    points_per_turnover_margin: float = DEFAULT_POINTS_PER_TURNOVER_MARGIN,
) -> float:
    """Baseline candidate (SUD-110): the win probability the existing
    Pythagorean/log5/home-field pipeline would produce if each team's
    blended points-for/against were first discounted by the portion
    attributable to turnover margin -- turnovers regress hard to the mean
    (see the SUD-110 PR for the real-data research this discount is based
    on), so a team riding turnover luck should not be rated as though that
    luck were durable offensive/defensive quality.

    Reuses compute_team_strength for the underlying PF/PA (no new team-
    strength model) and only adjusts the blended inputs before the same
    pythagorean_win_pct/log5/home-field steps generate_forecast already
    uses -- isolates whether the turnover discount itself helps, holding
    everything else constant, the same isolation discipline evaluation.py's
    other baselines (raw_pythagorean, prior_win_pct) already use.

    turnovers_index: build once via build_turnovers_committed_index and
    reuse across every call in a walk-forward run -- see that function's
    own docstring for why per-call statline scans do not scale.
    """
    home_strength = compute_team_strength(all_games, home_team_id, season_year, feature_cutoff_at, exponent=exponent)
    away_strength = compute_team_strength(all_games, away_team_id, season_year, feature_cutoff_at, exponent=exponent)

    home_for, home_against = _blended_pf_pa(home_strength)
    away_for, away_against = _blended_pf_pa(away_strength)

    home_games = _team_games_before(all_games, home_team_id, season_year, feature_cutoff_at)
    away_games = _team_games_before(all_games, away_team_id, season_year, feature_cutoff_at)
    home_turnover_margin = turnover_margin_per_game(turnovers_index, home_games, home_team_id) or 0.0
    away_turnover_margin = turnover_margin_per_game(turnovers_index, away_games, away_team_id) or 0.0

    # Split the discount evenly across points-for and points-against: a
    # team living off turnover luck both scored more (short fields) and
    # allowed less (fewer opponent possessions) than its underlying
    # quality would produce on its own, so both sides move.
    home_for -= points_per_turnover_margin * home_turnover_margin / 2
    home_against += points_per_turnover_margin * home_turnover_margin / 2
    away_for -= points_per_turnover_margin * away_turnover_margin / 2
    away_against += points_per_turnover_margin * away_turnover_margin / 2

    home_pyth = pythagorean_win_pct(max(home_for, 1e-3), max(home_against, 1e-3), exponent)
    away_pyth = pythagorean_win_pct(max(away_for, 1e-3), max(away_against, 1e-3), exponent)
    raw = combine_win_probabilities_log5(home_pyth, away_pyth)
    return apply_home_field(raw, neutral_site=bool(neutral_site))


def calibrate_points_per_turnover_margin(store: ResearchStore, training_season_years: list[int]) -> float:
    """Fit the points-per-turnover-margin discount from real training data:
    the closed-form OLS slope of (actual margin - blended pre-turnover
    scoring margin) against (home turnover-margin-per-game minus away
    turnover-margin-per-game), both measured as of each game's own
    point-in-time cutoff.

    A single-variable OLS slope has a closed form (Cov(x, y) / Var(x)), so
    this needs no optimization dependency -- same closed-form-over-library
    preference fit_exponent's golden-section search is the one deliberate
    exception to (needing an actual multi-candidate optimum, not a single
    linear fit). Callers are responsible for keeping training_season_years
    disjoint from whatever season the resulting constant is later evaluated
    against, same contract as fit_exponent/calibrate_home_field_margin_points.
    """
    all_games = [g for g in store.load_all("game") if isinstance(g, Game)]
    all_statlines = [s for s in store.load_all("player_game_statline") if isinstance(s, PlayerGameStatline)]
    turnovers_index = build_turnovers_committed_index(all_statlines)
    training_games = sorted(
        (g for g in all_games if g.completed and g.season_year in training_season_years),
        key=lambda g: g.kickoff_at,
    )

    xs: list[float] = []
    ys: list[float] = []
    for game in training_games:
        cutoff = game.kickoff_at - timedelta(hours=FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF)
        try:
            home_strength = compute_team_strength(all_games, game.home_team_id, game.season_year, cutoff)
            away_strength = compute_team_strength(all_games, game.away_team_id, game.season_year, cutoff)
        except (InsufficientHistoryError, InvalidScoringInputError):
            continue
        home_for, home_against = _blended_pf_pa(home_strength)
        away_for, away_against = _blended_pf_pa(away_strength)
        blended_margin_diff = (home_for - home_against) - (away_for - away_against)

        home_games = _team_games_before(all_games, game.home_team_id, game.season_year, cutoff)
        away_games = _team_games_before(all_games, game.away_team_id, game.season_year, cutoff)
        home_turnover_margin = turnover_margin_per_game(turnovers_index, home_games, game.home_team_id)
        away_turnover_margin = turnover_margin_per_game(turnovers_index, away_games, game.away_team_id)
        if home_turnover_margin is None or away_turnover_margin is None:
            continue

        actual_margin = (game.home_score or 0) - (game.away_score or 0)
        xs.append(home_turnover_margin - away_turnover_margin)
        ys.append(actual_margin - blended_margin_diff)

    if len(xs) < 2:
        raise InsufficientHistoryError("Not enough scoreable training games with turnover history to calibrate.")

    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    variance = sum((x - mean_x) ** 2 for x in xs)
    if variance == 0:
        raise InsufficientHistoryError("No variance in training turnover-margin differential; cannot fit a slope.")
    raw_slope = covariance / variance

    # The regression is fit as y = (actual - blended) vs. x = (turnover-
    # margin-per-game diff), i.e. this project's modeling hypothesis is
    # actual = blended - k*x for a positive discount k (blended already
    # embeds whatever turnover luck happened in that team's own games-to-
    # date, so a team with a *good* past turnover margin has an
    # *inflated* blended margin the model should discount, not add to).
    # Rearranged: y = -k*x, so the fitted OLS slope equals -k, not k --
    # negate it here so the return value is the positive discount rate
    # turnover_normalized_probability's subtraction formula expects, and
    # so it is directly comparable in sign and magnitude to the externally
    # cited ~+2.4-points figure this project's own fit is checked against.
    return -raw_slope
