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
from sgr.research.evaluation import run_walk_forward_evaluation
from sgr.research.historical import SeasonCoverageError, ingest_regular_season
from sgr.research.storage import ResearchStore

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


if __name__ == "__main__":
    app()
