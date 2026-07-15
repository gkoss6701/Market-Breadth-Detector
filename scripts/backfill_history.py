"""
One-time historical backfill. Run this BEFORE relying on daily_ingest.py's
incremental 5-day pulls -- the breadth engine's rolling windows (50/200-day
MAs, 252-day new-high/low lookback, 60-day composite z-score) need real
history to produce non-NaN values. Without this, breadth_daily rows exist
but composite_score/regime/pct_above_200ma etc. are all None, which is why
the dashboard looks empty even after the daily workflows run successfully.

Run locally or via the one-off backfill_history.yml workflow:
    python -m scripts.backfill_history --years 2
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging

from src.db.models import init_db, upsert_prices
from src.ingestion.universe import load_universe_from_csv
from src.ingestion.yfinance_client import fetch_bulk_ohlcv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

UNIVERSE_CSV = "data/SP500.csv"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=float, default=2.0,
                         help="Years of history to backfill. 2 comfortably "
                              "covers the 252-day and 200-day windows with "
                              "room to spare.")
    args = parser.parse_args()

    init_db()
    tickers = load_universe_from_csv(UNIVERSE_CSV)
    start = (dt.date.today() - dt.timedelta(days=int(args.years * 365))).isoformat()

    logger.info("Backfilling %d tickers from %s (this can take a few minutes)", len(tickers), start)
    df = fetch_bulk_ohlcv(tickers, start=start, batch_size=15, pause_seconds=1.5)
    logger.info("Fetched %d rows across %d tickers", len(df), df["ticker"].nunique() if not df.empty else 0)

    if df.empty:
        logger.error("Backfill returned no data -- check network access / ticker list before proceeding.")
        return

    upsert_prices(df)
    logger.info("Backfill complete. Run scripts/breadth_compute.py next (or the "
                "Breadth Compute workflow) to populate breadth_daily.")


if __name__ == "__main__":
    main()
