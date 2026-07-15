"""
End-to-end example: pull a small universe via yfinance, compute breadth
metrics, build the composite regime signal, and backtest a breakout signal
filtered by regime.

Deliberately uses a SMALL universe (~30 tickers) for a fast first run.
Swap in the full S&P 500 list (src/ingestion/universe.py) once this runs
clean end to end -- validate the pipeline on a small set before scaling up.

Run: python -m examples.run_backtest_example
"""
from __future__ import annotations

import pandas as pd

from src.backtest.runner import run_walk_forward
from src.backtest.signals import breakout_signal
from src.engine.composite import classify_regime, composite_score
from src.engine.metrics import (
    advance_decline_line,
    new_highs_lows,
    pct_above_ma,
    up_down_volume_ratio,
)
from src.ingestion.yfinance_client import fetch_bulk_ohlcv

# Small starter universe -- mega/large caps across sectors, plus SPY as the
# benchmark/index proxy for the price side of the backtest.
STARTER_UNIVERSE = [
    "SPY", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO",
    "JPM", "V", "UNH", "XOM", "JNJ", "PG", "HD", "MA", "COST", "MRK", "ABBV",
    "CVX", "KO", "PEP", "ADBE", "CRM", "AMD", "NFLX", "TMO", "LIN", "WMT",
]


def main():
    print("Fetching OHLCV via yfinance (this may take a minute)...")
    df = fetch_bulk_ohlcv(STARTER_UNIVERSE, start="2019-01-01", batch_size=15)
    df["date"] = pd.to_datetime(df["date"])

    breadth_universe = df[df["ticker"] != "SPY"]  # exclude the benchmark itself

    print("Computing breadth metrics...")
    metrics = {
        "pct_above_50ma": pct_above_ma(breadth_universe, window=50),
        "ad_line": advance_decline_line(breadth_universe),
        "up_down_vol_ratio": up_down_volume_ratio(breadth_universe),
    }
    nh, nl = new_highs_lows(breadth_universe, window=252)
    metrics["new_highs"] = nh
    metrics["new_lows"] = nl

    score = composite_score(metrics)
    regime = classify_regime(score)

    print("Regime distribution:")
    print(regime.value_counts())

    spy = df[df["ticker"] == "SPY"].sort_values("date").set_index("date")
    signal = breakout_signal(spy, lookback=20)

    # Align everything to SPY's date index (the tradeable instrument here).
    regime_aligned = regime.reindex(spy.index).ffill()

    print("\nRunning walk-forward backtest (breakout signal, risk_on filter)...")
    results = run_walk_forward(
        price_signals=signal,
        regime_series=regime_aligned,
        close=spy["close"],
        split_date="2023-06-01",
        holding_days=10,
        allowed_regimes=("risk_on",),
    )

    for label, table in results.items():
        print(f"\n--- {label} ---")
        print(table.to_string(index=False))
        if (table["n_trades"] < 30).any():
            print("  ^ NOTE: small sample size, treat results as directional only.")


if __name__ == "__main__":
    main()
