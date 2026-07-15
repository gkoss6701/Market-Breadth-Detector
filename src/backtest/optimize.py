"""
Weight/threshold validation harness. Structural guard against the most
common way this kind of tuning goes wrong: `optimize_weights` physically
cannot see holdout data -- it only accepts a pre-sliced train frame. You
score the winning config against holdout as a separate, explicit call
(`evaluate_on_holdout`), once, and that's the number you report.

If you're tempted to loop `evaluate_on_holdout` and pick whichever config
scores best there too -- don't. That silently converts holdout into a
second training set and the resulting "validated" performance is fiction.
"""
from __future__ import annotations

import itertools

import pandas as pd

from src.backtest.metrics import summarize
from src.backtest.runner import forward_returns
from src.engine.composite import classify_regime, composite_score

# Coarse grid -- expand once you see which dimension the score is
# sensitive to. Keep it coarse initially; a fine grid over few components
# and a modest sample size is exactly how you overfit.
DEFAULT_WEIGHT_GRID = {
    "pct_above_50ma": [0.5, 1.0, 1.5],
    "ad_line": [0.5, 1.0, 1.5],
    "new_highs": [0.5, 1.0],
    "up_down_vol_ratio": [0.5, 1.0],
}
# new_lows weight is held fixed at -1.0 (structural: rising new lows
# should always pull the score down) rather than grid-searched.


def _weight_combinations(grid: dict[str, list[float]]) -> list[dict[str, float]]:
    keys = list(grid.keys())
    combos = []
    for values in itertools.product(*[grid[k] for k in keys]):
        combo = dict(zip(keys, values))
        combo["new_lows"] = -1.0
        combos.append(combo)
    return combos


def optimize_weights(
    metrics_train: dict[str, pd.Series],
    price_signal_train: pd.Series,
    close_train: pd.Series,
    holding_days: int = 10,
    weight_grid: dict[str, list[float]] | None = None,
    zscore_window: int = 60,
) -> tuple[dict[str, float], pd.DataFrame]:
    """Grid search over weights, scored by Sharpe of forward returns on
    signal days filtered to risk_on regime, TRAIN DATA ONLY.

    Returns (best_weights, full_results_table) -- inspect the full table,
    not just the argmax; a narrow winner surrounded by much worse
    neighbors is a red flag for overfitting even within the train set.
    """
    weight_grid = weight_grid or DEFAULT_WEIGHT_GRID
    fwd_ret = forward_returns(close_train, holding_days)
    rows = []

    for weights in _weight_combinations(weight_grid):
        score = composite_score(metrics_train, weights=weights, window=zscore_window)
        regime = classify_regime(score).reindex(price_signal_train.index).ffill()

        mask = price_signal_train & (regime == "risk_on")
        trade_ret = fwd_ret.reindex(price_signal_train.index)[mask]

        result = summarize(trade_ret, holding_days, label=str(weights))
        result["weights"] = weights
        rows.append(result)

    results_table = pd.DataFrame(rows).sort_values("sharpe", ascending=False)
    # Guard: don't hand back a "winner" chosen on a handful of trades.
    viable = results_table[~results_table["low_sample_warning"]]
    best = (viable.iloc[0] if not viable.empty else results_table.iloc[0])["weights"]
    return best, results_table


def evaluate_on_holdout(
    weights: dict[str, float],
    metrics_holdout: dict[str, pd.Series],
    price_signal_holdout: pd.Series,
    close_holdout: pd.Series,
    holding_days: int = 10,
    zscore_window: int = 60,
) -> dict:
    """Score ONE chosen weight config against holdout data. Call this once,
    after `optimize_weights` has already picked a winner from train.
    """
    score = composite_score(metrics_holdout, weights=weights, window=zscore_window)
    regime = classify_regime(score).reindex(price_signal_holdout.index).ffill()
    fwd_ret = forward_returns(close_holdout, holding_days)

    mask = price_signal_holdout & (regime == "risk_on")
    trade_ret = fwd_ret.reindex(price_signal_holdout.index)[mask]

    return summarize(trade_ret, holding_days, label="holdout_result")
