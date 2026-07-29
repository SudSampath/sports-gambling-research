from __future__ import annotations

from sgr.models import BacktestResult


def run_binary_backtest(
    outcomes: list[int],
    win_probabilities: list[float],
    decimal_odds: list[float],
    stake_fraction: float = 0.01,
    starting_bankroll: float = 10000.0,
    strategy_name: str = "unknown",
) -> BacktestResult:
    bankroll = starting_bankroll
    trades = wins = losses = 0

    for actual, p_win, odds in zip(outcomes, win_probabilities, decimal_odds):
        if p_win <= (1.0 / odds):
            continue
        stake = bankroll * stake_fraction
        trades += 1
        if actual == 1:
            bankroll += stake * (odds - 1.0)
            wins += 1
        else:
            bankroll -= stake
            losses += 1

    pnl = bankroll - starting_bankroll
    return BacktestResult(
        strategy=strategy_name,
        trades=trades,
        wins=wins,
        losses=losses,
        pnl=round(pnl, 2),
        ending_bankroll=round(bankroll, 2),
    )
