"""
Evaluation statistics for backtest results. Deliberately reports sample
size alongside every performance number -- a Sharpe improvement computed
on 15 trades is not evidence of anything, and the runner should make that
impossible to miss.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def annualized_sharpe(returns: pd.Series, periods_per_year: int = 252, holding_days: int = 1) -> float:
    """Sharpe of a return series, annualized. `holding_days` scales the
    annualization factor for multi-day-holding-period trade returns
    (e.g. a 10-day swing return series shouldn't be annualized as if
    each observation were a single day)."""
    returns = returns.dropna()
    if len(returns) < 2 or returns.std() == 0:
        return float("nan")
    periods_per_trade_year = periods_per_year / holding_days
    return (returns.mean() / returns.std()) * np.sqrt(periods_per_trade_year)


def summarize(returns: pd.Series, holding_days: int = 1, label: str = "") -> dict:
    returns = returns.dropna()
    n = len(returns)
    return {
        "label": label,
        "n_trades": n,
        "mean_return": returns.mean() if n else float("nan"),
        "win_rate": (returns > 0).mean() if n else float("nan"),
        "sharpe": annualized_sharpe(returns, holding_days=holding_days),
        # Flag low-sample results explicitly rather than let a headline
        # number stand unqualified.
        "low_sample_warning": n < 30,
    }


def apply_transaction_costs(
    returns: pd.Series, cost_bps: float = 10.0
) -> pd.Series:
    """Subtract round-trip transaction cost (in basis points) from each
    trade return. Default 10bps is a reasonable liquid-large-cap swing
    trade estimate (commission + slippage) -- tighten or loosen based on
    your actual fill experience."""
    return returns - (cost_bps / 10_000.0)
