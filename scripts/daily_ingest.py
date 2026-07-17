"""
Pulls the most recent trading day's OHLCV for every ticker across every
registered index (union, deduped -- a ticker in multiple indexes is only
fetched once) and upserts into the prices table.

Requires index_constituents to already be populated -- run
scripts/refresh_universe.py at least once first.

Run via .github/workflows/daily_ingest.yml.
"""
from __future__ import annotations

import datetime as dt
import logging

from src.db.models import get_all_universe_tickers, init_db, upsert_prices
from src.ingestion.yfinance_client import fetch_bulk_ohlcv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 10  # small trailing window; prices table upserts, so overlap
                     # is cheap insurance against a missed run or long weekend


def main():
    init_db()
    tickers = get_all_universe_tickers()
    if not tickers:
        logger.error("index_constituents is empty -- run scripts/refresh_universe.py first.")
        return

    start = (dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()

    logger.info("Fetching %d tickers (union across all indexes) from %s", len(tickers), start)
    df = fetch_bulk_ohlcv(tickers, start=start, batch_size=50)
    logger.info("Fetched %d rows", len(df))

    upsert_prices(df)
    logger.info("Upsert complete")


if __name__ == "__main__":
    main()
