"""
One-time historical backfill across the FULL multi-index universe (union
of every major + sector index's tickers). Run this once before relying on
daily_ingest.py's incremental pulls -- the breadth engine's rolling
windows (50/200-day MAs, 252-day new-high/low lookback, 60-day composite
z-score) need real history to produce non-NaN values.

Requires index_constituents to already be populated -- run
scripts/refresh_universe.py FIRST.

Run: python -m scripts.backfill_history --years 2

Note on scale: the full S&P 500 + Nasdaq-100 + Dow 30 + 11 sectors is
~500-600 unique tickers (sectors are subsets of the S&P 500, so they add
little beyond the base ~500 + the non-overlapping Nasdaq-100/Dow names).
At 2 years of history via yfinance with batching, expect this to take
several minutes and hit rate limits occasionally -- see
src/ingestion/yfinance_client.py's batch_size/pause_seconds if you need
to tune for reliability over speed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging

from src.db.models import get_all_universe_tickers, init_db, upsert_prices
from src.ingestion.yfinance_client import fetch_bulk_ohlcv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=float, default=2.0,
                         help="Years of history to backfill. 2 comfortably "
                              "covers the 252-day and 200-day windows with "
                              "room to spare.")
    args = parser.parse_args()

    init_db()
    tickers = get_all_universe_tickers()
    if not tickers:
        logger.error("index_constituents is empty -- run scripts/refresh_universe.py first.")
        return

    start = (dt.date.today() - dt.timedelta(days=int(args.years * 365))).isoformat()

    logger.info("Backfilling %d tickers from %s (this can take several minutes at this scale)",
                len(tickers), start)
    df = fetch_bulk_ohlcv(tickers, start=start, batch_size=15, pause_seconds=1.5)
    logger.info("Fetched %d rows across %d tickers", len(df), df["ticker"].nunique() if not df.empty else 0)

    if df.empty:
        logger.error("Backfill returned no data -- check network access / ticker list before proceeding.")
        return

    upsert_prices(df)
    logger.info("Backfill complete. Run scripts/breadth_compute.py next (or the "
                "Breadth Compute workflow) to populate breadth_daily for all indexes.")


if __name__ == "__main__":
    main()
