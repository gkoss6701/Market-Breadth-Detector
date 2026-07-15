"""
Price-pattern signal generators. Kept separate from the breadth filter and
the evaluation harness so you can test multiple signal types against the
same breadth conditions without touching the harness.

Each function takes a single ticker's OHLCV DataFrame (sorted by date) and
returns a boolean Series aligned to df.index, True on days the pattern fires.
"""
from __future__ import annotations

import pandas as pd


def breakout_signal(df: pd.DataFrame, lookback: int = 20) -> pd.Series:
    """True when today's close exceeds the prior `lookback`-day high
    (the high is computed excluding today, via shift(1), to avoid
    the trivial/circular case of today's high including today's close)."""
    prior_high = df["close"].shift(1).rolling(lookback).max()
    return (df["close"] > prior_high).rename("breakout_signal")


def pullback_signal(
    df: pd.DataFrame,
    trend_ma: int = 50,
    pullback_ma: int = 20,
    threshold: float = 0.02,
) -> pd.Series:
    """True when price is above its longer trend MA (uptrend context) but
    has pulled back to within `threshold` (fractional) of the shorter MA --
    a 'buy the dip in an established uptrend' setup."""
    trend = df["close"].rolling(trend_ma).mean()
    pb = df["close"].rolling(pullback_ma).mean()
    uptrend = df["close"] > trend
    near_pullback_ma = (df["close"] - pb).abs() / df["close"] < threshold
    return (uptrend & near_pullback_ma).rename("pullback_signal")
