"""
Pulls the most recent trading day's OHLCV for the universe and upserts into
the prices table. Run via .github/workflows/daily_ingest.yml.

NOTE: swap `fetch_bulk_ohlcv` for a Polygon/Tiingo bulk-endpoint client
once you outgrow yfinance's reliability at full S&P 500 scale -- the
downstream schema/engine code doesn't need to change.
"""
from __future__ import annotations

import datetime as dt
import logging

from src.db.models import init_db, upsert_prices
from src.ingestion.universe import load_universe_from_csv
from src.ingestion.yfinance_client import fetch_bulk_ohlcv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

UNIVERSE_CSV = "data/SP500.csv"
LOOKBACK_DAYS = 10  # small trailing window; prices table upserts, so overlap
                     # is cheap insurance against a missed run or long weekend


def main():
    init_db()
    tickers = load_universe_from_csv(UNIVERSE_CSV)
    start = (dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()

    logger.info("Fetching %d tickers from %s", len(tickers), start)
    df = fetch_bulk_ohlcv(tickers, start=start, batch_size=50)
    logger.info("Fetched %d rows", len(df))

    upsert_prices(df)
    logger.info("Upsert complete")


if __name__ == "__main__":
    main()
