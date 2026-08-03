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


if __name__ == "__main__":
    app()
