"""
Computes breadth metrics for EVERY registered index (major + sector), not
just a single market-wide universe. For each index_key in index_metadata,
scopes the shared `prices` table down to that index's constituent
tickers, computes the full history of metrics + composite score/regime +
divergence flags, and bulk-upserts into breadth_daily tagged with that
index_key.

Recomputing full history per index on every run is the same tradeoff as
phase 1: more work than a pure incremental append, but simple and
correct, and still fast at this scale (a few hundred tickers x a few
years x ~14 indexes is a few seconds of pandas work, not a real cost).

Alerts are index-aware -- see src/alerts/twilio_notify.py's
ALERT_INDEX_KEYS for which indexes actually fire SMS (defaults to sp500
only, to avoid 14x alert volume from every sector flipping independently).

Run via .github/workflows/breadth_compute.yml, chained after daily_ingest.
Requires refresh_universe.py and daily_ingest.py to have already run.
"""
from __future__ import annotations

import logging

import pandas as pd

from src.alerts.twilio_notify import maybe_alert_divergence, maybe_alert_regime_flip
from src.db.models import (
    get_connection,
    get_index_registry,
    get_latest_breadth,
    init_db,
    upsert_breadth_daily_bulk,
)
from src.engine.composite import classify_regime, composite_score
from src.engine.divergence import bearish_divergence, bullish_divergence
from src.engine.metrics import (
    advance_decline_line,
    new_highs_lows,
    pct_above_ma,
    synthetic_index_price,
    up_down_volume_ratio,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def compute_history_for_index(prices: pd.DataFrame, index_key: str, tickers: set[str]) -> pd.DataFrame:
    """Returns the full computed breadth_daily history for one index, or
    an empty frame if there isn't enough data yet."""
    universe = prices[prices["ticker"].isin(tickers)]
    if universe.empty:
        logger.warning("No price data for index '%s' (0 of %d tickers matched) -- "
                        "check daily_ingest has run since this index was registered.",
                        index_key, len(tickers))
        return pd.DataFrame()

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

    proxy_price = synthetic_index_price(universe)
    bearish = bearish_divergence(proxy_price, score.reindex(proxy_price.index))
    bullish = bullish_divergence(proxy_price, score.reindex(proxy_price.index))

    history = pd.DataFrame(metrics)
    history["composite_score"] = score
    history["regime"] = regime
    history["bearish_divergence"] = bearish.reindex(history.index).fillna(False)
    history["bullish_divergence"] = bullish.reindex(history.index).fillna(False)
    history = history.reset_index().rename(columns={"index": "date"})
    history["date"] = pd.to_datetime(history["date"]).dt.date.astype(str)
    history["index_key"] = index_key

    # Only persist rows with at least some signal (ad_line always has a
    # value once there are 2+ days of data for that index).
    history = history.dropna(subset=["ad_line"])
    return history


def main():
    init_db()
    registry = get_index_registry()
    if registry.empty:
        logger.error("index_metadata is empty -- run scripts/refresh_universe.py first.")
        return

    with get_connection() as conn:
        prices = pd.read_sql("SELECT * FROM prices", conn, parse_dates=["date"])
        constituents = pd.read_sql("SELECT * FROM index_constituents", conn)

    if prices.empty:
        logger.warning("No price data found; run daily_ingest / backfill_history first.")
        return

    all_histories = []
    for _, idx_row in registry.iterrows():
        index_key, label = idx_row["index_key"], idx_row["label"]
        tickers = set(constituents.loc[constituents["index_key"] == index_key, "ticker"])
        if not tickers:
            logger.warning("Index '%s' has no constituents registered -- skipping.", index_key)
            continue

        history = compute_history_for_index(prices, index_key, tickers)
        if history.empty:
            continue

        all_histories.append(history)
        logger.info("%-32s %-6s %d rows (%s to %s)",
                    index_key, idx_row["index_type"], len(history),
                    history["date"].min(), history["date"].max())

    if not all_histories:
        logger.warning("No index produced computable history; nothing written.")
        return

    combined = pd.concat(all_histories, ignore_index=True)
    upsert_breadth_daily_bulk(combined)
    logger.info("Wrote %d total breadth_daily rows across %d indexes", len(combined), len(all_histories))

    # Alerts: compare latest two rows per index (gated by ALERT_INDEX_KEYS
    # inside the alert functions -- safe to call for every index).
    try:
        for _, idx_row in registry.iterrows():
            index_key, label = idx_row["index_key"], idx_row["label"]
            latest_two = get_latest_breadth(index_key, n_days=2)
            if len(latest_two) < 2:
                continue
            today_row, prior_row = latest_two.iloc[0], latest_two.iloc[1]
            maybe_alert_regime_flip(index_key, label, today_row["date"], prior_row["regime"], today_row["regime"])
            if bool(today_row["bearish_divergence"]):
                maybe_alert_divergence(index_key, label, today_row["date"], "bearish_divergence")
            if bool(today_row["bullish_divergence"]):
                maybe_alert_divergence(index_key, label, today_row["date"], "bullish_divergence")
    except Exception:
        # Alerting must never block the DB commit that follows this script
        # in the workflow -- see phase 1 postmortem on this exact failure mode.
        logger.exception("Alert step failed (Twilio not configured or a send error) -- "
                          "continuing, since breadth_daily was already written successfully.")


if __name__ == "__main__":
    main()
