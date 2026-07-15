"""
Price-vs-breadth divergence detection.

A bearish divergence: price makes a new N-day high while the breadth series
fails to confirm (does not also make a new high).
A bullish divergence: price makes a new N-day low while breadth fails to
confirm (does not also make a new low) -- often read as downside momentum
fading even as price ticks lower.
"""
from __future__ import annotations

import pandas as pd


def bearish_divergence(price: pd.Series, breadth: pd.Series, lookback: int = 20) -> pd.Series:
    price, breadth = price.align(breadth, join="inner")
    price_new_high = price >= price.rolling(lookback).max()
    breadth_confirmed = breadth >= breadth.rolling(lookback).max()
    return (price_new_high & ~breadth_confirmed).rename("bearish_divergence")


def bullish_divergence(price: pd.Series, breadth: pd.Series, lookback: int = 20) -> pd.Series:
    price, breadth = price.align(breadth, join="inner")
    price_new_low = price <= price.rolling(lookback).min()
    breadth_confirmed = breadth <= breadth.rolling(lookback).min()
    return (price_new_low & ~breadth_confirmed).rename("bullish_divergence")
