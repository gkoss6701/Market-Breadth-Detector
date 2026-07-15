"""
Sector-level breadth: reuses the same metric functions from engine/metrics.py,
scoped to a sector's ticker subset instead of the full universe. This is
what lets you answer "is the rally broad-based or is it just tech" rather
than only tracking a single market-wide composite.

Requires a sector mapping (data/sector_map.csv: ticker,sector) since GICS
sector isn't derivable from OHLCV alone.
"""
from __future__ import annotations

import pandas as pd

from src.engine.composite import classify_regime, composite_score
from src.engine.metrics import (
    advance_decline_line,
    new_highs_lows,
    pct_above_ma,
    up_down_volume_ratio,
)


def load_sector_map(path: str = "data/sector_map.csv") -> pd.DataFrame:
    return pd.read_csv(path)


def compute_sector_breadth(
    df: pd.DataFrame,
    sector_map: pd.DataFrame,
    ma_window: int = 50,
) -> pd.DataFrame:
    """Compute a composite breadth score per sector, per date.

    df: long-format OHLCV [date, ticker, close, volume, ...]
    sector_map: [ticker, sector]

    Returns a long-format frame: [date, sector, composite_score, regime,
    pct_above_ma, ad_line, new_highs, new_lows].

    Note: sector universes are much smaller than the full index (often
    20-70 tickers), so metrics like new-highs/lows and the composite
    z-score are noisier here -- treat single-day sector regime flips with
    more skepticism than the market-wide composite, and consider a wider
    zscore rolling window (e.g. 90-120d instead of 60d) to smooth it.
    """
    merged = df.merge(sector_map, on="ticker", how="inner")
    results = []

    for sector, sub in merged.groupby("sector"):
        metrics = {
            f"pct_above_{ma_window}ma": pct_above_ma(sub, window=ma_window),
            "ad_line": advance_decline_line(sub),
            "up_down_vol_ratio": up_down_volume_ratio(sub),
        }
        nh, nl = new_highs_lows(sub, window=252)
        metrics["new_highs"], metrics["new_lows"] = nh, nl

        score = composite_score(
            {
                f"pct_above_{ma_window}ma": metrics[f"pct_above_{ma_window}ma"],
                "ad_line": metrics["ad_line"],
                "new_highs": metrics["new_highs"],
                "new_lows": metrics["new_lows"],
                "up_down_vol_ratio": metrics["up_down_vol_ratio"],
            },
            weights={
                f"pct_above_{ma_window}ma": 1.0,
                "ad_line": 1.0,
                "new_highs": 1.0,
                "new_lows": -1.0,
                "up_down_vol_ratio": 1.0,
            },
            window=90,  # wider than the market-wide default; smaller N needs it
        )
        regime = classify_regime(score)

        out = pd.DataFrame(
            {
                "date": score.index,
                "sector": sector,
                "composite_score": score.values,
                "regime": regime.values,
                "pct_above_ma": metrics[f"pct_above_{ma_window}ma"].reindex(score.index).values,
                "ad_line": metrics["ad_line"].reindex(score.index).values,
                "new_highs": metrics["new_highs"].reindex(score.index).values,
                "new_lows": metrics["new_lows"].reindex(score.index).values,
            }
        )
        results.append(out)

    return pd.concat(results, ignore_index=True) if results else pd.DataFrame()


def sector_relative_strength(sector_breadth: pd.DataFrame) -> pd.DataFrame:
    """Rank sectors by composite_score per date -- surfaces rotation
    (which sectors are leading/lagging breadth-wise on a given day),
    independent of the absolute regime level."""
    ranked = sector_breadth.copy()
    ranked["rank"] = ranked.groupby("date")["composite_score"].rank(
        ascending=False, method="min"
    )
    return ranked.sort_values(["date", "rank"])
