"""
Core breadth metrics. All functions take a long-format DataFrame with
columns [date, ticker, open, high, low, close, volume] indexed/sorted by
(ticker, date), and return a daily (date-indexed) Series or tuple of Series.

Lookahead-bias discipline: every rolling calculation here only uses data
through the current row (pandas .rolling() default behavior). Do not
reindex/pivot in a way that could let a later date's value leak backward --
if you modify these, re-check that a value on date T never depends on data
after T.
"""
from __future__ import annotations

import pandas as pd


def pct_above_ma(df: pd.DataFrame, window: int) -> pd.Series:
    """% of universe closing above its own N-day moving average, per date."""
    df = df.sort_values(["ticker", "date"])
    ma = df.groupby("ticker")["close"].transform(lambda s: s.rolling(window).mean())
    above = df["close"] > ma
    out = above.groupby(df["date"]).mean() * 100
    out.name = f"pct_above_{window}ma"
    return out


def advance_decline_line(df: pd.DataFrame) -> pd.Series:
    """Cumulative (advancers - decliners) across the universe."""
    df = df.sort_values(["ticker", "date"])
    chg = df.groupby("ticker")["close"].diff()
    adv = (chg > 0).groupby(df["date"]).sum()
    dec = (chg < 0).groupby(df["date"]).sum()
    ad_line = (adv - dec).cumsum()
    ad_line.name = "ad_line"
    return ad_line


def new_highs_lows(df: pd.DataFrame, window: int = 252) -> tuple[pd.Series, pd.Series]:
    """Count of tickers making a new N-day high / low, per date."""
    df = df.sort_values(["ticker", "date"])
    roll_high = df.groupby("ticker")["close"].transform(lambda s: s.rolling(window).max())
    roll_low = df.groupby("ticker")["close"].transform(lambda s: s.rolling(window).min())
    nh = (df["close"] >= roll_high).groupby(df["date"]).sum()
    nl = (df["close"] <= roll_low).groupby(df["date"]).sum()
    nh.name, nl.name = "new_highs", "new_lows"
    return nh, nl


def up_down_volume_ratio(df: pd.DataFrame) -> pd.Series:
    """Ratio of volume on up days to volume on down days, per date."""
    df = df.sort_values(["ticker", "date"])
    chg = df.groupby("ticker")["close"].diff()
    up_vol = df.loc[chg > 0].groupby("date")["volume"].sum()
    down_vol = df.loc[chg < 0].groupby("date")["volume"].sum()
    ratio = (up_vol / down_vol.replace(0, pd.NA)).rename("up_down_vol_ratio")
    return ratio


def index_close(df: pd.DataFrame, index_ticker: str = "SPY") -> pd.Series:
    """Convenience accessor for a benchmark's close series, e.g. for
    divergence comparisons. Assumes index_ticker is present in df."""
    sub = df[df["ticker"] == index_ticker].sort_values("date")
    return sub.set_index("date")["close"].rename("index_close")
