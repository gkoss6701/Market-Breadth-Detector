"""
Backtest harness. Two distinct questions, evaluated separately -- do not
conflate them:

1. FILTER EFFECTIVENESS: does gating a price signal by breadth regime
   improve outcomes vs. trading the signal unconditionally?
2. SIGNAL EFFECTIVENESS: does a divergence flag, on its own, precede a
   drawdown/rally within N days more often than a random baseline?

Both use a train/holdout split -- any threshold or weight tuning happens
on `train` only; `holdout` is reported once, not iterated on. If you find
yourself re-running holdout after adjusting weights, you've silently
turned it into a second training set.
"""
from __future__ import annotations

import pandas as pd

from src.backtest.metrics import apply_transaction_costs, summarize


def forward_returns(close: pd.Series, holding_days: int) -> pd.Series:
    """Return over the next `holding_days` bars, indexed to the signal date.
    Uses .shift(-holding_days), i.e. genuinely forward-looking on purpose
    (this is the outcome we're measuring, not a feature) -- keep this
    function's output out of anything used as a model INPUT."""
    return close.shift(-holding_days) / close - 1.0


def train_holdout_split(dates: pd.DatetimeIndex, split_date: str) -> tuple[pd.DatetimeIndex, pd.DatetimeIndex]:
    split = pd.Timestamp(split_date)
    train = dates[dates < split]
    holdout = dates[dates >= split]
    return train, holdout


def backtest_filter(
    price_signals: pd.Series,
    regime_series: pd.Series,
    close: pd.Series,
    holding_days: int = 10,
    allowed_regimes: tuple[str, ...] = ("risk_on",),
    cost_bps: float = 10.0,
) -> pd.DataFrame:
    """Compare forward returns for signal days overall vs. signal days that
    also fall within `allowed_regimes`. Returns a two-row summary frame
    (baseline vs filtered) with sample sizes -- always inspect n_trades
    before trusting a Sharpe/win-rate delta.
    """
    fwd_ret = forward_returns(close, holding_days)

    aligned = pd.DataFrame(
        {"signal": price_signals, "regime": regime_series, "fwd_ret": fwd_ret}
    ).dropna(subset=["signal"])

    baseline_ret = aligned.loc[aligned["signal"], "fwd_ret"]
    filtered_ret = aligned.loc[
        aligned["signal"] & aligned["regime"].isin(allowed_regimes), "fwd_ret"
    ]

    baseline_ret = apply_transaction_costs(baseline_ret, cost_bps)
    filtered_ret = apply_transaction_costs(filtered_ret, cost_bps)

    rows = [
        summarize(baseline_ret, holding_days, label="baseline_unfiltered"),
        summarize(filtered_ret, holding_days, label=f"filtered_{'_'.join(allowed_regimes)}"),
    ]
    return pd.DataFrame(rows)


def backtest_divergence_signal(
    divergence_flags: pd.Series,
    close: pd.Series,
    holding_days: int = 10,
    cost_bps: float = 10.0,
) -> pd.DataFrame:
    """Test a divergence flag as a standalone timing signal: compare forward
    returns following a divergence day vs. a random-day baseline of the
    same sample size (sampled without replacement, fixed seed for
    reproducibility) so the comparison isn't just 'signal days vs all days'
    which biases toward whatever the average market drift is."""
    fwd_ret = forward_returns(close, holding_days)

    flagged_dates = divergence_flags[divergence_flags].index
    signal_ret = fwd_ret.reindex(flagged_dates).dropna()

    rng = pd.Series(fwd_ret.dropna().index)
    n = min(len(signal_ret), len(rng))
    random_dates = rng.sample(n=n, random_state=42) if n else rng
    random_ret = fwd_ret.reindex(random_dates).dropna()

    signal_ret = apply_transaction_costs(signal_ret, cost_bps)
    random_ret = apply_transaction_costs(random_ret, cost_bps)

    rows = [
        summarize(signal_ret, holding_days, label="divergence_signal"),
        summarize(random_ret, holding_days, label="random_baseline"),
    ]
    return pd.DataFrame(rows)


def run_walk_forward(
    price_signals: pd.Series,
    regime_series: pd.Series,
    close: pd.Series,
    split_date: str,
    holding_days: int = 10,
    allowed_regimes: tuple[str, ...] = ("risk_on",),
    cost_bps: float = 10.0,
) -> dict[str, pd.DataFrame]:
    """Run backtest_filter separately on train and holdout windows.
    Any threshold/weight decisions should be made looking ONLY at
    results['train'] -- results['holdout'] is the honest check, run once.
    """
    train_dates, holdout_dates = train_holdout_split(close.index, split_date)

    results = {}
    for label, dates in (("train", train_dates), ("holdout", holdout_dates)):
        if len(dates) == 0:
            continue
        results[label] = backtest_filter(
            price_signals.reindex(dates),
            regime_series.reindex(dates),
            close.reindex(dates),
            holding_days=holding_days,
            allowed_regimes=allowed_regimes,
            cost_bps=cost_bps,
        )
    return results
