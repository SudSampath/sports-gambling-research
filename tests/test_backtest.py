from sgr.backtest.engine import run_binary_backtest


def test_backtest_runs():
    result = run_binary_backtest(
        outcomes=[1, 0, 1],
        win_probabilities=[0.6, 0.55, 0.58],
        decimal_odds=[2.0, 1.9, 2.1],
        strategy_name="t1",
    )
    assert result.trades > 0
    assert result.strategy == "t1"
