"""
One-time historical backfill across the FULL multi-index universe (union
of every major + sector index's tickers). Run this once before relying on
daily_ingest.py's incremental pulls -- the breadth engine's rolling
windows (50/200-day MAs, 252-day new-high/low lookback, 60-day composite
z-score) need real history to produce non-NaN values.

Requires index_constituents to already be populated -- run
scripts/refresh_universe.py FIRST.

Run: python -m scripts.backfill_history --years 2

Note on scale: the full multi-index universe (8 major indexes -- S&P
500/400/600, Nasdaq-100, Dow 30, Russell 1000/2000/3000 -- + 11 sectors)
is roughly 3,000 unique tickers, dominated by Russell 3000's own ~2,961
(sectors are subsets of the S&P 500 and add nothing beyond it; S&P
400/600 add only a couple dozen names not already in Russell 3000). Each
ticker is one EODHD API call (see src/ingestion/eodhd_client.py) -- at
this scale, though, that's enough sustained request volume that EODHD
will 429 ("Too Many Requests") at the old max_workers=10 concurrency;
eodhd_client.py now retries a 429 with backoff instead of giving up on
that ticker immediately, and defaults to max_workers=5 to make hitting
the limit in the first place less likely. Concurrent fetching still
keeps wall-clock time reasonable even though there's no bulk
multi-ticker historical-price endpoint on this plan -- just budget more
time than a single-threaded pull would suggest, and don't be alarmed by
occasional "rate-limited ... retrying" warnings in the log; that's the
retry logic working as intended, not a failure.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging

from src.db.models import get_all_universe_tickers, init_db, upsert_prices
from src.ingestion.eodhd_client import fetch_bulk_ohlcv

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

    logger.info("Backfilling %d tickers from %s (this can take a few minutes at this scale)",
                len(tickers), start)
    df = fetch_bulk_ohlcv(tickers, start=start)
    logger.info("Fetched %d rows across %d tickers", len(df), df["ticker"].nunique() if not df.empty else 0)

    if df.empty:
        logger.error("Backfill returned no data -- check network access / ticker list before proceeding.")
        return

    upsert_prices(df)
    logger.info("Backfill complete. Run scripts/breadth_compute.py next (or the "
                "Breadth Compute workflow) to populate breadth_daily for all indexes.")


if __name__ == "__main__":
    main()
