from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

import pandas as pd
import typer
from rich.console import Console
from rich.table import Table

from sgr.algorithms import MomentumStrategy, ValueStrategy
from sgr.backtest import run_binary_backtest
from sgr.config import ConfigurationError
from sgr.connectors import KalshiConnector
from sgr.connectors.espn import EspnConnector
from sgr.connectors.nflverse import NflverseConnector
from sgr.research.candidate_comparison import run_candidate_comparison
from sgr.research.closing_lines import ingest_closing_lines
from sgr.research.play_level_features import ingest_play_level_features
from sgr.research.evaluation import run_walk_forward_evaluation
from sgr.research.historical import SeasonCoverageError, ingest_regular_season
from sgr.research.holdout_backtest import DEFAULT_HOLDOUT_FRACTION, DEFAULT_HOLDOUT_SEED, run_holdout_backtest
from sgr.research.injury_ingest import ingest_current_injuries
from sgr.research.margin import DEFAULT_HOME_FIELD_MARGIN_POINTS, calibrate_home_field_margin_points
from sgr.research.margin_evaluation import run_margin_walk_forward_evaluation
from sgr.research.player_backfill import backfill_boxscores
from sgr.research.player_impact_evaluation import evaluate_player_impact_on_missing_starters
from sgr.research.roster_continuity import (
    ingest_roster_continuity,
    project_season_win_totals_with_roster_continuity,
)
from sgr.research.roster_continuity_evaluation import run_roster_continuity_evaluation
from sgr.research.rolling_evaluation import (
    DEFAULT_ROLLING_WINDOW_SEASONS,
    PRIMARY_TEST_SEASONS,
    robustness_evaluation,
    rolling_origin_evaluation,
)
from sgr.research.schemas import Game, Team
from sgr.research.season_simulation import (
    DEFAULT_N_SIMULATIONS,
    DEFAULT_SIMULATION_SEED,
    GameOutcomeSpec,
    SeasonSimulationError,
    combined_outcome_probability,
    simulate_season,
)
from sgr.research.storage import ResearchStore
from sgr.research.win_totals import project_season_win_totals
from sgr.models import NFLSeasonType

app = typer.Typer(help="Sports gambling research CLI")
console = Console()


def _run_configured_command(operation: Callable[[], Awaitable[None]]) -> None:
    """Run a credentialed command without exposing configuration tracebacks."""
    try:
        asyncio.run(operation())
    except ConfigurationError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=2) from None


@app.command()
def demo_value() -> None:
    """Run value strategy on synthetic data."""
    from sgr.models import MarketSnapshot

    now = datetime.now(timezone.utc)
    markets = [
        MarketSnapshot(market_id="MKT1", ticker="TEAM-A", title="Team A wins", yes_price=0.45, no_price=0.55, volume=12000, ts=now),
        MarketSnapshot(market_id="MKT2", ticker="TEAM-B", title="Team B wins", yes_price=0.62, no_price=0.38, volume=9000, ts=now),
    ]
    model_probs = {"MKT1": 0.52, "MKT2": 0.58}

    strategy = ValueStrategy(min_edge=0.03)
    signals = strategy.generate(markets, model_probs)

    table = Table(title="Value Signals")
    table.add_column("Market")
    table.add_column("Side")
    table.add_column("Edge")
    table.add_column("Confidence")
    for s in signals:
        table.add_row(s.market_id, s.side, f"{s.edge:.3f}", f"{s.confidence:.2f}")
    console.print(table)


@app.command()
def demo_momentum() -> None:
    """Run momentum strategy on synthetic time series."""
    now = datetime.now(timezone.utc)
    rows = []
    for i in range(10):
        rows.append({"market_id": "MKT1", "ts": now + timedelta(minutes=i), "yes_price": 0.45 + (i * 0.01)})
    prices = pd.DataFrame(rows)

    strategy = MomentumStrategy(window=3, threshold=0.03)
    signals = strategy.generate(prices)

    table = Table(title="Momentum Signals")
    table.add_column("Market")
    table.add_column("Side")
    table.add_column("Edge")
    for s in signals:
        table.add_row(s.market_id, s.side, f"{s.edge:.3f}")
    console.print(table)


@app.command()
def demo_backtest() -> None:
    """Run sample backtest."""
    result = run_binary_backtest(
        outcomes=[1, 0, 1, 1, 0, 1],
        win_probabilities=[0.58, 0.52, 0.61, 0.57, 0.48, 0.55],
        decimal_odds=[1.95, 1.92, 2.05, 1.85, 2.1, 1.9],
        strategy_name="sample-value",
    )
    console.print(result.model_dump())


@app.command()
def kalshi_markets(limit: int = 10) -> None:
    """Fetch live Kalshi markets."""

    async def _run() -> None:
        connector = KalshiConnector()
        markets = await connector.list_markets(limit=limit)
        table = Table(title=f"Kalshi Markets (top {limit})")
        table.add_column("Ticker")
        table.add_column("YES")
        table.add_column("NO")
        table.add_column("Volume")
        for m in markets:
            table.add_row(m.ticker, f"{m.yes_price:.2f}", f"{m.no_price:.2f}", f"{m.volume:,.0f}")
        console.print(table)

    _run_configured_command(_run)


@app.command()
def ingest_historical_seasons(
    seasons: list[int] = typer.Option(..., "--season", help="Season year; repeat for multiple, e.g. --season 2023 --season 2024"),
    current_season: int | None = typer.Option(None, help="Season year to ingest as a schedule-only pass (completion not required)"),
    refresh: bool = typer.Option(False, help="Bypass the cache and refetch every week from ESPN"),
) -> None:
    """Ingest regular-season NFL games into local canonical storage (SUD-35)."""

    async def _run() -> None:
        connector = EspnConnector()
        store = ResearchStore()
        table = Table(title="Historical season ingest")
        table.add_column("Season")
        table.add_column("Games")
        table.add_column("Teams")
        table.add_column("Result")

        plan = [(year, True) for year in seasons]
        if current_season is not None:
            plan.append((current_season, False))

        for year, require_completed in plan:
            try:
                report = await ingest_regular_season(
                    connector, store, year, require_completed=require_completed, refresh=refresh
                )
                table.add_row(str(year), f"{report.games_captured}/{report.games_expected}", f"{report.teams_captured}/{report.teams_expected}", "[green]complete[/green]")
            except SeasonCoverageError as error:
                report = error.report
                table.add_row(
                    str(year),
                    f"{report.games_captured}/{report.games_expected}",
                    f"{report.teams_captured}/{report.teams_expected}",
                    f"[red]failed: {error}[/red]",
                )
        console.print(table)

    asyncio.run(_run())


@app.command()
def evaluate_pythagorean(
    seasons: list[int] = typer.Option(..., "--season", help="Completed season year to evaluate; repeat for multiple"),
    exponent: float = typer.Option(2.37, help="Pythagorean exponent to evaluate"),
) -> None:
    """Chronologically walk-forward evaluate the Pythagorean baseline (SUD-38)."""
    store = ResearchStore()
    report = run_walk_forward_evaluation(store, seasons, exponent=exponent)

    summary = Table(title=f"Walk-forward evaluation ({report.model_version}, x={report.exponent})")
    summary.add_column("Model")
    summary.add_column("N")
    summary.add_column("Excluded")
    summary.add_column("Brier")
    summary.add_column("Log loss")
    summary.add_row(
        "pythagorean (shrunk)",
        str(report.overall.sample_count),
        str(report.overall.excluded_count),
        f"{report.overall.brier_score:.4f}" if report.overall.brier_score is not None else "-",
        f"{report.overall.log_loss:.4f}" if report.overall.log_loss is not None else "-",
    )
    for name, metrics in report.baseline_overall.items():
        summary.add_row(
            name,
            str(metrics.sample_count),
            str(metrics.excluded_count),
            f"{metrics.brier_score:.4f}" if metrics.brier_score is not None else "-",
            f"{metrics.log_loss:.4f}" if metrics.log_loss is not None else "-",
        )
    console.print(summary)

    by_season = Table(title="By season")
    by_season.add_column("Season")
    by_season.add_column("N")
    by_season.add_column("Excluded")
    by_season.add_column("Brier")
    for year, metrics in sorted(report.by_season.items()):
        by_season.add_row(
            str(year),
            str(metrics.sample_count),
            str(metrics.excluded_count),
            f"{metrics.brier_score:.4f}" if metrics.brier_score is not None else "-",
        )
    console.print(by_season)
    console.print(f"dataset checksum: {report.dataset_checksum[:16]}  seed: {report.random_seed}")


@app.command()
def evaluate_margin(
    seasons: list[int] = typer.Option(..., "--season", help="Completed season year to evaluate; repeat for multiple"),
    home_field_margin_points: float = typer.Option(
        DEFAULT_HOME_FIELD_MARGIN_POINTS, help="Home-field margin term (points); see calibrate-home-field-margin"
    ),
) -> None:
    """Walk-forward evaluate expected-margin predictions against actual final margins (SUD-105)."""
    store = ResearchStore()
    report = run_margin_walk_forward_evaluation(store, seasons, home_field_margin_points=home_field_margin_points)

    summary = Table(title=f"Margin walk-forward evaluation ({report.model_version}, home-field={report.home_field_margin_points:.2f}pt)")
    summary.add_column("Model")
    summary.add_column("N")
    summary.add_column("MAE")
    summary.add_column("RMSE")
    summary.add_row(
        "expected margin",
        str(report.overall.sample_count),
        f"{report.overall.mean_absolute_error:.2f}" if report.overall.mean_absolute_error is not None else "-",
        f"{report.overall.root_mean_squared_error:.2f}" if report.overall.root_mean_squared_error is not None else "-",
    )
    for name, metrics in report.baseline_overall.items():
        summary.add_row(
            name,
            str(metrics.sample_count),
            f"{metrics.mean_absolute_error:.2f}" if metrics.mean_absolute_error is not None else "-",
            f"{metrics.root_mean_squared_error:.2f}" if metrics.root_mean_squared_error is not None else "-",
        )
    console.print(summary)
    console.print(
        f"residual stdev (for a margin confidence interval): "
        f"{report.overall.residual_stdev:.2f}pt"
        if report.overall.residual_stdev is not None else "residual stdev: -"
    )
    console.print(f"dataset checksum: {report.dataset_checksum[:16]}")


@app.command()
def calibrate_home_field_margin(
    seasons: list[int] = typer.Option(..., "--season", help="Completed training season year; repeat for multiple"),
) -> None:
    """Fit the home-field margin term from real completed games (SUD-105)."""
    store = ResearchStore()
    calibrated = calibrate_home_field_margin_points(store, seasons)
    console.print(f"Calibrated home-field margin term: {calibrated:.4f} points (training seasons: {seasons})")


@app.command()
def backfill_player_boxscores(
    seasons: list[int] = typer.Option(..., "--season", help="Season year to backfill; repeat for multiple"),
    refresh: bool = typer.Option(False, help="Bypass the cache and refetch every game from ESPN"),
) -> None:
    """Backfill player box scores for completed regular-season games (SUD-93)."""

    async def _run() -> None:
        connector = EspnConnector()
        store = ResearchStore()
        report = await backfill_boxscores(connector, store, seasons, refresh=refresh)

        table = Table(title="Box score backfill")
        table.add_column("Field")
        table.add_column("Value")
        table.add_row("Seasons", ", ".join(str(y) for y in report.season_years))
        table.add_row("Games considered", str(report.games_considered))
        table.add_row("Games with statlines", str(report.games_with_statlines))
        table.add_row("Statlines written", str(report.statlines_written))
        table.add_row("Games with zero statlines", str(len(report.games_with_zero_statlines)))
        console.print(table)
        if report.games_with_zero_statlines:
            console.print(f"[yellow]Zero-statline event IDs: {list(report.games_with_zero_statlines)}[/yellow]")

    asyncio.run(_run())


@app.command(name="ingest-current-injuries")
def ingest_current_injuries_cmd(
    season: int = typer.Option(..., "--season", help="Season year"),
    refresh: bool = typer.Option(False, help="Bypass the cache and refetch from ESPN"),
) -> None:
    """Ingest ESPN's current injury report for every not-yet-played game this
    season (SUD-109). Never touches completed games -- see injury_ingest.py."""

    async def _run() -> None:
        connector = EspnConnector()
        store = ResearchStore()
        report = await ingest_current_injuries(connector, store, season, refresh=refresh)

        table = Table(title=f"Injury report ingest ({report.season_year})")
        table.add_column("Field")
        table.add_column("Value")
        table.add_row("Not-yet-played games considered", str(report.games_considered))
        table.add_row("Games with injury entries", str(report.games_with_injury_entries))
        table.add_row("Availability reports written", str(report.reports_written))
        console.print(table)

    asyncio.run(_run())


@app.command()
def evaluate_player_impact(
    seasons: list[int] = typer.Option(..., "--season", help="Completed season year to evaluate; repeat for multiple"),
) -> None:
    """Walk-forward evaluate the player-impact adjustment on games with a missing usual starter (SUD-62)."""

    async def _run() -> None:
        store = ResearchStore()
        report = await evaluate_player_impact_on_missing_starters(store, seasons)

        summary = Table(title="Player impact evaluation (missing-starter games)")
        summary.add_column("Field")
        summary.add_column("Value")
        summary.add_row("Seasons", ", ".join(str(y) for y in report.season_years))
        summary.add_row("Games considered", str(report.games_considered))
        summary.add_row("Games with missing starters", str(report.games_with_missing_starters))
        summary.add_row("Missing-starter samples", str(len(report.samples)))
        summary.add_row(
            "Baseline Brier / Log loss",
            f"{report.baseline_brier:.4f} / {report.baseline_log_loss:.4f}"
            if report.baseline_brier is not None else "-",
        )
        summary.add_row(
            "Adjusted Brier / Log loss",
            f"{report.adjusted_brier:.4f} / {report.adjusted_log_loss:.4f}"
            if report.adjusted_brier is not None else "-",
        )
        console.print(summary)
        if report.position_sample_counts:
            console.print(f"Samples by category: {report.position_sample_counts}")

    asyncio.run(_run())


@app.command()
def holdout_backtest(
    seasons: list[int] = typer.Option(..., "--season", help="Completed season year to evaluate; repeat for multiple"),
    holdout_fraction: float = typer.Option(DEFAULT_HOLDOUT_FRACTION, help="Fraction of games to show a scorecard row for"),
    seed: int = typer.Option(DEFAULT_HOLDOUT_SEED, help="Random seed for holdout selection (reproducible reruns)"),
    limit: int = typer.Option(0, help="Print at most this many scorecard rows (0 = all)"),
) -> None:
    """Predicted-vs-actual scorecard for a reproducible holdout sample of real games (SUD-103)."""
    store = ResearchStore()
    report = run_holdout_backtest(store, seasons, holdout_fraction=holdout_fraction, seed=seed)

    console.print(
        "[dim]Note: the model has no free parameters fit per run, so 'holdout' here controls which "
        "games get a scorecard row -- every game's forecast already only uses strictly-prior data, "
        "on either side of the split.[/dim]"
    )

    rows_to_show = report.rows if limit <= 0 else report.rows[:limit]
    scorecard = Table(title=f"Holdout scorecard ({report.holdout_game_count}/{report.full_game_count} games, seed={report.seed})")
    scorecard.add_column("Wk")
    scorecard.add_column("Matchup")
    scorecard.add_column("Predicted (home win)")
    scorecard.add_column("Actual")
    scorecard.add_column("Correct")
    for row in rows_to_show:
        scorecard.add_row(
            str(row.week),
            f"{row.away_team} @ {row.home_team}",
            f"{row.predicted_home_win_probability:.1%}",
            "home" if row.actual_home_win else "away",
            "[green]yes[/green]" if row.correct else "[red]no[/red]",
        )
    console.print(scorecard)

    summary = Table(title="Holdout vs. full-set metrics")
    summary.add_column("Metric")
    summary.add_column(f"Holdout (n={report.holdout_game_count})")
    summary.add_column(f"Full set (n={report.full_game_count})")
    summary.add_row("Brier score", f"{report.holdout_brier:.4f}" if report.holdout_brier is not None else "-", f"{report.full_brier:.4f}" if report.full_brier is not None else "-")
    summary.add_row("Log loss", f"{report.holdout_log_loss:.4f}" if report.holdout_log_loss is not None else "-", f"{report.full_log_loss:.4f}" if report.full_log_loss is not None else "-")
    summary.add_row("Accuracy", f"{report.holdout_accuracy:.1%}" if report.holdout_accuracy is not None else "-", f"{report.full_accuracy:.1%}" if report.full_accuracy is not None else "-")
    console.print(summary)


@app.command()
def project_win_totals(
    season: int = typer.Option(..., help="Season year to project"),
    as_of: datetime = typer.Option(
        None, help="Point-in-time cutoff (ISO 8601, UTC); defaults to now"
    ),
) -> None:
    """Each team's projected win total: exact expectation plus a variance-based
    confidence band, no simulation (SUD-104)."""
    store = ResearchStore()
    cutoff = as_of.replace(tzinfo=timezone.utc) if as_of and as_of.tzinfo is None else (as_of or datetime.now(timezone.utc))
    report = project_season_win_totals(store, season, as_of=cutoff)

    table = Table(title=f"Season {report.season_year} win-total projections (as of {report.as_of.isoformat()})")
    table.add_column("Team")
    table.add_column("Record so far")
    table.add_column("Games left")
    table.add_column("Exp. additional wins")
    table.add_column("Exp. total wins")
    table.add_column("~68% range")
    for p in report.projections:
        table.add_row(
            p.abbreviation,
            f"{p.wins_so_far:g} / {p.games_played}",
            str(p.games_remaining),
            f"{p.expected_additional_wins:.2f}",
            f"{p.expected_total_wins:.2f}",
            f"{p.confidence_low:.1f}-{p.confidence_high:.1f}",
        )
    console.print(table)


@app.command(name="ingest-roster-continuity")
def ingest_roster_continuity_cmd(
    season: int = typer.Option(..., "--season", help="Target NFL season year"),
    as_of: datetime = typer.Option(
        None,
        help="Historical feature cutoff (ISO 8601); required with --historical-week1",
    ),
    historical_week1: bool = typer.Option(
        False,
        "--historical-week1",
        help="Use the target season's Week 1 roster instead of the current roster",
    ),
    refresh: bool = typer.Option(False, help="Bypass the local nflverse CSV cache"),
) -> None:
    """Ingest prior-snap-weighted roster retention from nflverse (SUD-118)."""
    if historical_week1 and as_of is None:
        console.print("[red]--as-of is required with --historical-week1.[/red]")
        raise typer.Exit(code=2)

    async def _run() -> None:
        requested_cutoff = _resolve_cutoff(as_of)
        signals = await ingest_roster_continuity(
            NflverseConnector(),
            ResearchStore(),
            season,
            feature_cutoff_at=requested_cutoff,
            historical_week1=historical_week1,
            refresh=refresh,
        )
        table = Table(title=f"Season {season} roster continuity")
        table.add_column("Teams")
        table.add_column("Source")
        table.add_column("Feature cutoff")
        table.add_row(
            str(len(signals)),
            signals[0].roster_source_kind,
            signals[0].feature_cutoff_at.isoformat(),
        )
        console.print(table)

    asyncio.run(_run())


@app.command(name="compare-roster-continuity")
def compare_roster_continuity_cmd(
    seasons: list[int] = typer.Option(
        ..., "--season", help="Completed held-out season; repeat for multiple"
    ),
) -> None:
    """Compare baseline and roster continuity on game and win-total errors (SUD-118)."""
    report = run_roster_continuity_evaluation(ResearchStore(), seasons)
    table = Table(title=f"Roster-continuity holdout ({', '.join(str(y) for y in report.season_years)})")
    table.add_column("Configuration")
    table.add_column("Game Brier")
    table.add_column("Game log loss")
    table.add_column("Accuracy")
    table.add_column("Win-total MAE")
    table.add_column("Win-total RMSE")
    for name, game, wins in (
        ("baseline", report.baseline_game_metrics, report.baseline_win_total_metrics),
        (report.model_version, report.candidate_game_metrics, report.candidate_win_total_metrics),
    ):
        table.add_row(
            name,
            f"{game.brier_score:.6f}" if game.brier_score is not None else "-",
            f"{game.log_loss:.6f}" if game.log_loss is not None else "-",
            f"{game.accuracy:.2%}" if game.accuracy is not None else "-",
            f"{wins.mean_absolute_error:.3f}",
            f"{wins.root_mean_squared_error:.3f}",
        )
    console.print(table)
    console.print(
        f"Paired Brier z={report.paired_brier_z:.3f}, p={report.paired_brier_p_value:.4f}"
        if report.paired_brier_z is not None else "Paired Brier test: unavailable (zero variance/too few samples)"
    )
    console.print(
        f"Paired win-total squared-error z={report.paired_win_total_z:.3f}, "
        f"p={report.paired_win_total_p_value:.4f}"
        if report.paired_win_total_z is not None else "Paired win-total test: unavailable (zero variance/too few samples)"
    )


@app.command(name="project-roster-continuity")
def project_roster_continuity_cmd(
    season: int = typer.Option(..., "--season", help="Season year to project"),
    as_of: datetime = typer.Option(None, help="Point-in-time cutoff (ISO 8601); defaults to now"),
) -> None:
    """Show current baseline and roster-continuity win estimates side by side."""
    store = ResearchStore()
    cutoff = _resolve_cutoff(as_of)
    baseline = project_season_win_totals(
        store, season, as_of=cutoff, apply_injury_adjustment=False
    )
    candidate = project_season_win_totals_with_roster_continuity(store, season, as_of=cutoff)
    baseline_by_team = {projection.team_id: projection for projection in baseline.projections}
    table = Table(title=f"Season {season} win totals as of {cutoff.isoformat()}")
    table.add_column("Team")
    table.add_column("Baseline")
    table.add_column("Roster continuity")
    table.add_column("Delta")
    table.add_column("Off retained")
    table.add_column("Def retained")
    for projection in candidate.projections:
        baseline_projection = baseline_by_team[projection.team_id]
        delta = projection.expected_total_wins - baseline_projection.expected_total_wins
        table.add_row(
            projection.abbreviation,
            f"{baseline_projection.expected_total_wins:.2f}",
            f"{projection.expected_total_wins:.2f}",
            f"{delta:+.2f}",
            f"{projection.offense_retention:.1%}",
            f"{projection.defense_retention:.1%}",
        )
    console.print(table)


def _resolve_cutoff(as_of: datetime | None) -> datetime:
    return as_of.replace(tzinfo=timezone.utc) if as_of and as_of.tzinfo is None else (as_of or datetime.now(timezone.utc))


@app.command(name="simulate-season")
def simulate_season_cmd(
    season: int = typer.Option(..., "--season", help="Season year to simulate"),
    as_of: datetime = typer.Option(None, help="Point-in-time cutoff (ISO 8601, UTC); defaults to now"),
    n_simulations: int = typer.Option(DEFAULT_N_SIMULATIONS, help="Number of Monte Carlo runs"),
    seed: int = typer.Option(DEFAULT_SIMULATION_SEED, help="Random seed (reproducible reruns)"),
) -> None:
    """Full-league Monte Carlo simulation: win-total distribution, division-win
    odds, and playoff odds per team (SUD-106)."""
    store = ResearchStore()
    cutoff = _resolve_cutoff(as_of)
    try:
        report = simulate_season(store, season, as_of=cutoff, n_simulations=n_simulations, seed=seed)
    except SeasonSimulationError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=2) from None

    console.print(f"[dim]{report.tiebreaker_note}[/dim]")
    table = Table(title=f"Season {report.season_year} simulation ({report.n_simulations} runs, seed={report.seed}, as of {report.as_of.isoformat()})")
    table.add_column("Team")
    table.add_column("Conf/Div")
    table.add_column("Win total (p10-p50-p90)")
    table.add_column("Mean wins")
    table.add_column("Div. win %")
    table.add_column("Playoff %")
    for r in report.team_results:
        table.add_row(
            r.abbreviation,
            f"{r.conference} {r.division}",
            f"{r.win_total_p10:g}-{r.win_total_p50:g}-{r.win_total_p90:g}",
            f"{r.mean_win_total:.1f}",
            f"{r.division_win_probability:.1%}",
            f"{r.playoff_probability:.1%}",
        )
    console.print(table)


@app.command()
def combined_outcome(
    season: int = typer.Option(..., "--season", help="Season year"),
    pick: list[str] = typer.Option(
        ..., "--pick", help="WEEK:WINNER_ABBR:OPPONENT_ABBR, e.g. 2:KC:LV; repeat for multiple games"
    ),
    as_of: datetime = typer.Option(None, help="Point-in-time cutoff (ISO 8601, UTC); defaults to now"),
    n_simulations: int = typer.Option(DEFAULT_N_SIMULATIONS, help="Number of Monte Carlo runs"),
    seed: int = typer.Option(DEFAULT_SIMULATION_SEED, help="Random seed (reproducible reruns)"),
) -> None:
    """The model's estimated joint probability and fair/breakeven odds for a
    user-specified set of game outcomes -- a research/calibration figure,
    never a recommendation, pick, or ranked combination (SUD-106)."""
    store = ResearchStore()
    cutoff = _resolve_cutoff(as_of)

    teams_by_abbr = {t.abbreviation: t for t in store.load_all("team") if isinstance(t, Team)}
    season_games = [
        g for g in store.load_all("game")
        if isinstance(g, Game) and g.season_type == NFLSeasonType.REGULAR and g.season_year == season
    ]

    outcomes: list[GameOutcomeSpec] = []
    for entry in pick:
        try:
            week_str, winner_abbr, opponent_abbr = entry.split(":")
            week = int(week_str)
        except ValueError:
            console.print(f"[red]--pick must be WEEK:WINNER_ABBR:OPPONENT_ABBR, got {entry!r}[/red]")
            raise typer.Exit(code=2) from None
        winner_team = teams_by_abbr.get(winner_abbr)
        opponent_team = teams_by_abbr.get(opponent_abbr)
        if winner_team is None or opponent_team is None:
            console.print(f"[red]Unknown team abbreviation in --pick {entry!r}[/red]")
            raise typer.Exit(code=2) from None
        game = next(
            (
                g for g in season_games
                if g.week == week and {g.home_team_id, g.away_team_id} == {winner_team.id, opponent_team.id}
            ),
            None,
        )
        if game is None:
            console.print(f"[red]No week {week} game found between {winner_abbr} and {opponent_abbr} in {season}[/red]")
            raise typer.Exit(code=2) from None
        outcomes.append(GameOutcomeSpec(game.id, winner_team.id, f"{winner_abbr} over {opponent_abbr} (wk{week})"))

    try:
        result = combined_outcome_probability(
            store, season, outcomes, as_of=cutoff, n_simulations=n_simulations, seed=seed,
        )
    except SeasonSimulationError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=2) from None

    console.print(f"[dim]{result.label}[/dim]")
    for spec in result.outcomes:
        console.print(f"  - {spec.description}")
    console.print(f"Joint probability: {result.joint_probability:.4f}")
    console.print(
        f"Fair (breakeven) decimal odds: {result.fair_decimal_odds:.2f}"
        if result.fair_decimal_odds is not None else "Fair decimal odds: n/a (probability is zero)"
    )


@app.command(name="ingest-closing-lines")
def ingest_closing_lines_cmd(
    seasons: list[int] = typer.Option(
        ..., "--season", help="Regular season year to ingest closing lines for; repeat for multiple"
    ),
    refresh: bool = typer.Option(False, help="Bypass the local nflverse games.csv cache"),
) -> None:
    """Ingest closing spreads/totals/moneylines from nflverse for the closing
    market benchmark (SUD-119). Not a training feature for the independent
    fair-price model -- benchmark-only, see docs/PRD.md."""

    async def _run() -> None:
        report = await ingest_closing_lines(
            NflverseConnector(), ResearchStore(), seasons, refresh=refresh
        )

        table = Table(title="Closing-line ingest coverage")
        table.add_column("Season")
        table.add_column("Games in source")
        table.add_column("Spread")
        table.add_column("Total")
        table.add_column("Moneyline")
        for year, coverage in sorted(report.by_season.items()):
            table.add_row(
                str(year),
                str(coverage.games_in_source),
                f"{coverage.spread_coverage}/{coverage.games_in_source}",
                f"{coverage.total_coverage}/{coverage.games_in_source}",
                f"{coverage.moneyline_coverage}/{coverage.games_in_source}",
            )
        console.print(table)
        console.print(
            f"Closing lines written: {report.games_written} "
            f"(matched to a locally ingested Game: {report.matched_to_local_game})"
        )
        if report.unmatched_espn_ids:
            console.print(
                f"[yellow]{len(report.unmatched_espn_ids)} source rows had no ESPN ID and were "
                f"excluded: {list(report.unmatched_espn_ids)[:10]}[/yellow]"
            )

    asyncio.run(_run())


@app.command(name="ingest-play-level-features")
def ingest_play_level_features_cmd(
    seasons: list[int] = typer.Option(
        ..., "--season", help="Regular season year to ingest play-by-play for; repeat for multiple"
    ),
    refresh: bool = typer.Option(False, help="Bypass the local nflverse pbp/games.csv cache"),
) -> None:
    """Aggregate nflverse play-by-play into team-game efficiency records
    (SUD-123). Data layer only -- fits no coefficients, selects no model,
    and changes no forecast."""

    async def _run() -> None:
        report = await ingest_play_level_features(NflverseConnector(), ResearchStore(), seasons, refresh=refresh)

        table = Table(title="Play-level feature ingest coverage")
        table.add_column("Season")
        table.add_column("Plays in source")
        table.add_column("Used")
        table.add_column("Unmatched game")
        table.add_column("Unresolved team")
        table.add_column("CPOE coverage")
        for year, coverage in sorted(report.by_season.items()):
            table.add_row(
                str(year),
                str(coverage.plays_in_source),
                str(coverage.plays_used),
                str(coverage.unmatched_game_plays),
                str(coverage.unresolved_team_plays),
                str(coverage.cpoe_coverage),
            )
        console.print(table)
        console.print(f"Team-game efficiency records written: {report.team_games_written}")

    asyncio.run(_run())


@app.command()
def compare_candidates(
    seasons: list[int] = typer.Option(..., "--season", help="Completed season year to evaluate; repeat for multiple"),
) -> None:
    """Walk-forward compare baseline vs. injuries/turnover/SOS individually
    and blended together, on the same held-out real games (SUD-111)."""
    store = ResearchStore()
    report = run_candidate_comparison(store, seasons)

    table = Table(title=f"Candidate comparison ({', '.join(str(y) for y in report.season_years)})")
    table.add_column("Configuration")
    table.add_column("N")
    table.add_column("Brier")
    table.add_column("Log loss")
    table.add_column("Accuracy")
    for name, m in report.results.items():
        table.add_row(
            name,
            str(m.sample_count),
            f"{m.brier_score:.4f}" if m.brier_score is not None else "-",
            f"{m.log_loss:.4f}" if m.log_loss is not None else "-",
            f"{m.accuracy:.1%}" if m.accuracy is not None else "-",
        )
    console.print(table)


@app.command(name="expand-evaluation")
def expand_evaluation_cmd(
    window: str = typer.Option("expanding", help="Training window: 'expanding' or 'rolling'"),
    rolling_window_seasons: int = typer.Option(
        DEFAULT_ROLLING_WINDOW_SEASONS, help="Seasons of history used when --window=rolling"
    ),
    test_seasons: list[int] = typer.Option(
        list(PRIMARY_TEST_SEASONS),
        "--test-season",
        help="Rolling-origin test season; repeat for multiple (default: 2017-2025)",
    ),
    robustness: bool = typer.Option(
        False, "--robustness", help="Also run the 2000-2010-trained, 2011-2016-tested stable-parameter check"
    ),
) -> None:
    """Rolling-origin, season-held-out evaluation across many seasons (SUD-122).

    2025 is labeled validation, not a pristine holdout; 2026 can never enter
    a fold as a test season or training season."""
    store = ResearchStore()
    report = rolling_origin_evaluation(
        store, test_seasons=tuple(test_seasons), window=window, rolling_window_seasons=rolling_window_seasons
    )

    console.print(
        f"[dim]Validation season: {report.validation_season} (already consulted by earlier "
        f"candidate decisions). Prospective lockbox: {report.lockbox_season} (reserved, never "
        f"scored here).[/dim]"
    )
    table = Table(title=f"Rolling-origin evaluation ({report.window} window)")
    table.add_column("Test season")
    table.add_column("Train seasons")
    table.add_column("Exponent")
    table.add_column("N")
    table.add_column("Excluded")
    table.add_column("Brier")
    table.add_column("Margin MAE")
    for fold in report.folds:
        table.add_row(
            str(fold.test_season),
            f"{fold.training_seasons[0]}-{fold.training_seasons[-1]} ({len(fold.training_seasons)})",
            f"{fold.chosen_exponent:.3f}",
            str(fold.game_metrics.sample_count),
            str(fold.game_metrics.excluded_count),
            f"{fold.game_metrics.brier_score:.4f}" if fold.game_metrics.brier_score is not None else "-",
            f"{fold.margin_metrics.mean_absolute_error:.2f}"
            if fold.margin_metrics.mean_absolute_error is not None
            else "-",
        )
    console.print(table)
    console.print(
        f"Overall: N={report.overall.sample_count}, excluded={report.overall.excluded_count}, "
        f"Brier={report.overall.brier_score:.4f}" if report.overall.brier_score is not None else "Overall: no scored games"
    )
    if report.season_clustered_brier_ci is not None:
        lo, hi = report.season_clustered_brier_ci
        console.print(f"Season-clustered Brier 95% CI: [{lo:.4f}, {hi:.4f}]")
    if report.overall.exclusion_reasons:
        console.print(f"Exclusion reasons: {report.overall.exclusion_reasons}")

    if robustness:
        robustness_report = robustness_evaluation(store)
        console.print(
            f"\n[dim]Robustness check: trained once on "
            f"{robustness_report.training_seasons[0]}-{robustness_report.training_seasons[-1]}, "
            f"tested on {', '.join(str(f.test_season) for f in robustness_report.folds)} "
            f"(kept separate from the primary rolling analysis).[/dim]"
        )
        console.print(
            f"Robustness overall: N={robustness_report.overall.sample_count}, "
            f"Brier={robustness_report.overall.brier_score:.4f}"
            if robustness_report.overall.brier_score is not None
            else "Robustness overall: no scored games"
        )


if __name__ == "__main__":
    app()
