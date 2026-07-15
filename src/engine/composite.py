"""
Composite momentum gauge: combines individual breadth metrics into a single
z-scored regime signal. Weights and thresholds are deliberately simple
defaults -- treat them as a starting point to validate via backtest, not
a finished product. See backtest/runner.py for the train/holdout discipline
used to avoid curve-fitting these.
"""
from __future__ import annotations

import pandas as pd

DEFAULT_WEIGHTS = {
    "pct_above_50ma": 1.0,
    "ad_line": 1.0,
    "new_highs": 1.0,
    "new_lows": -1.0,  # rising new lows should pull score down
    "up_down_vol_ratio": 1.0,
}

# Regime thresholds on the composite z-score. Start here; re-validate on
# holdout data before trusting them, per the backtest methodology.
REGIME_THRESHOLDS = {
    "risk_on": 1.0,
    "neutral": -0.5,
    "weak": -1.5,
    # anything below "weak" threshold => "risk_off"
}


def zscore(series: pd.Series, window: int = 60) -> pd.Series:
    """Rolling z-score. Uses only trailing data (no lookahead)."""
    mean = series.rolling(window).mean()
    std = series.rolling(window).std()
    return (series - mean) / std


def composite_score(
    metrics: dict[str, pd.Series],
    weights: dict[str, float] | None = None,
    window: int = 60,
) -> pd.Series:
    """Combine metrics into a single weighted z-score series."""
    weights = weights or DEFAULT_WEIGHTS
    aligned = pd.DataFrame(metrics)
    z = aligned.apply(lambda col: zscore(col, window))

    weighted_sum = pd.Series(0.0, index=z.index)
    weight_total = 0.0
    for name, w in weights.items():
        if name in z.columns:
            weighted_sum = weighted_sum.add(z[name] * w, fill_value=0.0)
            weight_total += abs(w)

    score = weighted_sum / weight_total if weight_total else weighted_sum
    score.name = "composite_score"
    return score


def classify_regime(score: pd.Series, thresholds: dict[str, float] | None = None) -> pd.Series:
    thresholds = thresholds or REGIME_THRESHOLDS

    def _bucket(v: float) -> str:
        if pd.isna(v):
            return "unknown"
        if v > thresholds["risk_on"]:
            return "risk_on"
        if v > thresholds["neutral"]:
            return "neutral"
        if v > thresholds["weak"]:
            return "weak"
        return "risk_off"

    return score.apply(_bucket).rename("regime")
