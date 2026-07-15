"""
Sanity tests focused on the things most likely to silently break a breadth
pipeline: lookahead leakage and basic metric correctness. Not exhaustive --
extend as you add metrics.
"""
import pandas as pd

from src.backtest.optimize import evaluate_on_holdout, optimize_weights
from src.backtest.runner import forward_returns
from src.backtest.signals import breakout_signal
from src.engine.composite import classify_regime, composite_score
from src.engine.metrics import advance_decline_line, pct_above_ma
from src.engine.sector_breadth import compute_sector_breadth, sector_relative_strength


def _toy_universe():
    dates = pd.date_range("2024-01-01", periods=10, freq="B")
    rows = []
    # Ticker A: steadily rising, stays above its MA
    for i, d in enumerate(dates):
        rows.append({"date": d, "ticker": "A", "close": 100 + i, "volume": 1000})
    # Ticker B: steadily falling, stays below its MA
    for i, d in enumerate(dates):
        rows.append({"date": d, "ticker": "B", "close": 100 - i, "volume": 1000})
    return pd.DataFrame(rows)


def test_pct_above_ma_bounds():
    df = _toy_universe()
    result = pct_above_ma(df, window=3)
    assert (result.dropna() >= 0).all()
    assert (result.dropna() <= 100).all()


def test_advance_decline_line_no_lookahead():
    df = _toy_universe()
    ad_line = advance_decline_line(df)
    # Truncate the input and recompute -- earlier values must be identical,
    # since they shouldn't depend on data that comes after them.
    cutoff = df["date"].unique()[5]
    truncated = df[df["date"] <= cutoff]
    ad_line_truncated = advance_decline_line(truncated)
    pd.testing.assert_series_equal(
        ad_line.loc[:cutoff], ad_line_truncated, check_names=False
    )


def test_forward_returns_shifts_correctly():
    close = pd.Series([100, 110, 121], index=pd.date_range("2024-01-01", periods=3, freq="B"))
    fwd = forward_returns(close, holding_days=1)
    assert abs(fwd.iloc[0] - 0.10) < 1e-9
    assert pd.isna(fwd.iloc[-1])  # no future data for the last observation


def test_composite_score_and_regime_run():
    df = _toy_universe()
    metrics = {"pct_above_50ma": pct_above_ma(df, window=3)}
    score = composite_score(metrics, weights={"pct_above_50ma": 1.0}, window=3)
    regime = classify_regime(score)
    assert set(regime.dropna().unique()) <= {"risk_on", "neutral", "weak", "risk_off", "unknown"}


def _toy_sector_map():
    return pd.DataFrame({"ticker": ["A", "B"], "sector": ["Tech", "Tech"]})


def test_sector_breadth_runs_and_ranks():
    df = _toy_universe()
    sector_map = _toy_sector_map()
    sb = compute_sector_breadth(df, sector_map, ma_window=3)
    assert not sb.empty
    assert set(sb["sector"].unique()) == {"Tech"}
    ranked = sector_relative_strength(sb)
    assert "rank" in ranked.columns


def test_optimize_weights_never_sees_holdout():
    """Structural check: optimize_weights only receives train-window
    series, so it is mechanically incapable of fitting to holdout data."""
    df = _toy_universe()
    dates = sorted(df["date"].unique())
    split = dates[6]
    train_dates = [d for d in dates if d < split]
    holdout_dates = [d for d in dates if d >= split]

    metrics_train = {
        "pct_above_50ma": pct_above_ma(df, 3).reindex(train_dates),
        "ad_line": advance_decline_line(df).reindex(train_dates),
    }
    metrics_holdout = {
        "pct_above_50ma": pct_above_ma(df, 3).reindex(holdout_dates),
        "ad_line": advance_decline_line(df).reindex(holdout_dates),
    }

    bench = df[df["ticker"] == "A"].sort_values("date").set_index("date")
    signal = breakout_signal(bench, lookback=2)

    best_weights, results_table = optimize_weights(
        metrics_train,
        signal.reindex(train_dates).fillna(False),
        bench["close"].reindex(train_dates),
        holding_days=2,
        weight_grid={"pct_above_50ma": [0.5, 1.0], "ad_line": [1.0]},
    )
    assert isinstance(best_weights, dict)
    assert "n_trades" in results_table.columns

    holdout_result = evaluate_on_holdout(
        best_weights,
        metrics_holdout,
        signal.reindex(holdout_dates).fillna(False),
        bench["close"].reindex(holdout_dates),
        holding_days=2,
    )
    assert "sharpe" in holdout_result
