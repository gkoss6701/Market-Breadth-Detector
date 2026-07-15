"""
Reads accumulated prices, computes the FULL breadth history (metrics +
composite score/regime + divergence flags for every date with enough
lookback), and bulk-upserts all of it into breadth_daily -- not just
today's row. This is what makes the dashboard's time-series charts have
data to plot; writing only the latest day per run leaves breadth_daily
with as few rows as the job has been run, which looks empty even once
metrics are non-null.

Recomputing the whole history each run is more work than strictly
necessary for a single new day, but at a few hundred tickers x a few
years of data this is a sub-second-to-low-seconds pandas operation, not
a real cost -- correctness/simplicity wins here over a more complex
incremental-append design.

Alerts (regime flip, divergence) still only fire based on the latest day,
compared against the prior day already in the table.

Run via .github/workflows/breadth_compute.yml, chained after daily_ingest.
"""
from __future__ import annotations

import logging

import pandas as pd

from src.alerts.twilio_notify import maybe_alert_divergence, maybe_alert_regime_flip
from src.db.models import get_connection, get_latest_breadth, init_db, upsert_breadth_daily_bulk
from src.engine.composite import classify_regime, composite_score
from src.engine.divergence import bearish_divergence, bullish_divergence
from src.engine.metrics import (
    advance_decline_line,
    index_close,
    new_highs_lows,
    pct_above_ma,
    up_down_volume_ratio,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BENCHMARK_TICKER = "SPY"


def main():
    init_db()
    with get_connection() as conn:
        prices = pd.read_sql("SELECT * FROM prices", conn, parse_dates=["date"])

    if prices.empty:
        logger.warning("No price data found; run daily_ingest / backfill_history first.")
        return

    universe = prices[prices["ticker"] != BENCHMARK_TICKER]

    metrics = {
        "pct_above_20ma": pct_above_ma(universe, 20),
        "pct_above_50ma": pct_above_ma(universe, 50),
        "pct_above_200ma": pct_above_ma(universe, 200),
        "ad_line": advance_decline_line(universe),
        "up_down_vol_ratio": up_down_volume_ratio(universe),
    }
    nh, nl = new_highs_lows(universe, window=252)
    metrics["new_highs"], metrics["new_lows"] = nh, nl

    score = composite_score(
        {k: v for k, v in metrics.items() if k in
         ("pct_above_50ma", "ad_line", "new_highs", "new_lows", "up_down_vol_ratio")}
    )
    regime = classify_regime(score)

    bench = index_close(prices, BENCHMARK_TICKER)
    bearish = bearish_divergence(bench, score.reindex(bench.index))
    bullish = bullish_divergence(bench, score.reindex(bench.index))

    # Build the full history frame, one row per date across all metrics.
    history = pd.DataFrame(metrics)
    history["composite_score"] = score
    history["regime"] = regime
    history["bearish_divergence"] = bearish.reindex(history.index).fillna(False)
    history["bullish_divergence"] = bullish.reindex(history.index).fillna(False)
    history = history.reset_index().rename(columns={"index": "date"})
    history["date"] = pd.to_datetime(history["date"]).dt.date.astype(str)

    # Only persist rows where we have at least SOME signal (ad_line always
    # has a value once there are 2+ days of data; earlier rows before that
    # are meaningless placeholders and just clutter the table/charts).
    history = history.dropna(subset=["ad_line"])

    if history.empty:
        logger.warning("Computed history is empty after filtering; nothing to write.")
        return

    upsert_breadth_daily_bulk(history)
    logger.info(
        "Wrote %d breadth_daily rows (%s to %s)",
        len(history), history["date"].min(), history["date"].max(),
    )

    # Alerts: compare the latest two rows now in the table. Alerting is
    # best-effort -- a missing/misconfigured Twilio setup must not crash
    # this script, because a crash here stops the workflow before the
    # "Commit updated DB" step runs, silently losing the computed history.
    try:
        latest_two = get_latest_breadth(n_days=2)
        if len(latest_two) >= 2:
            today_row, prior_row = latest_two.iloc[0], latest_two.iloc[1]
            maybe_alert_regime_flip(today_row["date"], prior_row["regime"], today_row["regime"])
            if bool(today_row["bearish_divergence"]):
                maybe_alert_divergence(today_row["date"], "bearish_divergence")
            if bool(today_row["bullish_divergence"]):
                maybe_alert_divergence(today_row["date"], "bullish_divergence")
    except Exception:
        logger.exception("Alert step failed (Twilio not configured or a send error) -- "
                          "continuing, since breadth_daily was already written successfully.")


if __name__ == "__main__":
    main()
