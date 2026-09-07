"""
EODHD-based OHLCV client. Replaces yfinance_client.py -- yfinance's lack
of a real bulk endpoint plus frequent rate-limit/partial-failure behavior
at ~500-600 ticker scale was the original motivation for batch_size/
pause_seconds tuning there (see the removed module's own docstring in
git history). EODHD is a paid EOD data vendor: still one HTTP call per
ticker (no bulk multi-ticker historical-price endpoint on the current
plan -- see README's Data source section), but each call is a real
api_token-authenticated REST request against consolidated-exchange data
rather than scraping/rate-limit roulette, and returns the ticker's full
requested date range in one shot.

Endpoint confirmed directly against EODHD's own docs (not assumed from
memory) and against a handful of live test calls: GET
https://eodhd.com/api/eod/{TICKER}.US?api_token=...&from=...&to=...&period=d&fmt=json
Ticker format is SYMBOL.EXCHANGE; ".US" covers the major US exchanges
for this endpoint. Share-class tickers use a dash, same as this
project's existing Wikipedia/SlickCharts convention (BRK-B, not BRK.B --
verified live; the dotted form 404s). An unknown/delisted ticker 404s
rather than returning an empty 200, so that's handled as "no data for
this ticker," not a hard failure that aborts the whole run.

Requires EODHD_API_KEY in the environment (see .env.example) -- unlike
yfinance, which needed no API key at all, this is a new required setup
step on the NAS.
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests

logger = logging.getLogger(__name__)

EODHD_API_KEY = os.environ.get("EODHD_API_KEY")
_BASE_URL = "https://eodhd.com/api/eod"
_TIMEOUT = 20
_EMPTY = pd.DataFrame(columns=["date", "ticker", "open", "high", "low", "close", "volume"])


def _fetch_one(ticker: str, start: str, end: str | None) -> pd.DataFrame:
    """Fetch one ticker's full daily OHLCV for [start, end]. Returns an
    empty frame -- never raises -- for a 404 or empty result, so one bad
    symbol in a 500+ ticker universe doesn't abort the whole run; same
    philosophy as yfinance_client's old per-batch try/except, just
    per-ticker now since each ticker is its own request.
    """
    url = f"{_BASE_URL}/{ticker}.US"
    params = {
        "api_token": EODHD_API_KEY,
        "fmt": "json",
        "period": "d",
        "from": start,
    }
    if end:
        params["to"] = end

    try:
        resp = requests.get(url, params=params, timeout=_TIMEOUT)
        if resp.status_code == 404:
            logger.warning(
                "No EODHD data for %s.US (404 -- delisted, renamed, or not "
                "an EODHD-covered US ticker)", ticker,
            )
            return _EMPTY
        resp.raise_for_status()
    except requests.RequestException:
        logger.exception("EODHD fetch failed for %s", ticker)
        return _EMPTY

    rows = resp.json()
    if not rows:
        return _EMPTY

    df = pd.DataFrame(rows)
    df["ticker"] = ticker
    # Use the split/dividend-adjusted close for the `close` column --
    # every breadth metric that reads price (pct_above_ma,
    # advance_decline_line, new_highs_lows, synthetic_index_price,
    # divergence detection) keys off `close` only, never open/high/low,
    # so an un-adjusted close would show a fake "crash" on every split.
    # Same reasoning yfinance_client's auto_adjust=True covered before.
    # open/high/low are left as EODHD's raw values -- nothing downstream
    # currently reads them, so there's no correctness issue in leaving
    # them un-adjusted.
    df["close"] = df["adjusted_close"]
    return df[["date", "ticker", "open", "high", "low", "close", "volume"]]


def fetch_bulk_ohlcv(
    tickers: list[str],
    start: str,
    end: str | None = None,
    max_workers: int = 10,
) -> pd.DataFrame:
    """Fetch OHLCV for many tickers, one EODHD API call per ticker, run
    concurrently since a 500-600 ticker backfill would otherwise take
    several minutes serially. max_workers is a politeness/local-resource
    knob, not a rate-limit workaround the way yfinance's batch_size/
    pause_seconds was -- EODHD's daily request quota has enormous
    headroom at this project's scale (check with get_user_details-style
    account info if that ever changes).

    Returns a long-format DataFrame: [date, ticker, open, high, low,
    close, volume] -- identical shape to the yfinance client this
    replaces, so upsert_prices/breadth_compute need no changes.
    """
    if not EODHD_API_KEY:
        raise RuntimeError(
            "EODHD_API_KEY is not set. Add it to .env (see .env.example). "
            "This is a new required setup step -- the yfinance client this "
            "replaced needed no API key at all."
        )

    frames = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_one, t, start, end): t for t in tickers}
        done = 0
        for future in as_completed(futures):
            frames.append(future.result())
            done += 1
            if done % 50 == 0 or done == len(tickers):
                logger.info("EODHD fetch progress: %d/%d tickers", done, len(tickers))

    if not frames:
        return _EMPTY

    result = pd.concat(frames, ignore_index=True)
    result = result.dropna(subset=["close"])
    return result.sort_values(["ticker", "date"]).reset_index(drop=True)
