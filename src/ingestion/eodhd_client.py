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

Transient failures (429s, 5xx, network timeouts): earlier versions of
this client treated ALL of these the same as a 404 -- log it and
silently return an empty frame for that ticker. That was fine back when
the full universe was ~1,500 tickers at max_workers=10 and EODHD never
actually throttled or hiccuped on this project in practice. Once Russell
1000/2000/3000 pushed the universe to ~2,987 tickers at the same
concurrency, sustained 429s started showing up on a first_time_setup.sh
run (fixed first), and then a plain SSL-handshake ReadTimeout showed up
too -- same underlying problem, just a different transient cause.
Silently swallowing either meant a ticker that was merely rate-limited
or momentarily unreachable (not actually missing/delisted) ended up
with zero price history for that run, quietly skewing whichever index
it belonged to.

_fetch_one now retries ALL of these with backoff before giving up:
network-level exceptions (timeouts, connection errors -- anything
requests.get() itself can raise), 429 (honoring EODHD's Retry-After
header when present), and 5xx server errors. A 404 and other 4xx errors
(bad request, auth, etc.) still fail fast with no retry, since those
aren't transient -- retrying a genuinely bad/delisted ticker just wastes
time. A ticker only ends up empty if it's still failing after every
retry, or if it's a genuine 404/4xx.
"""
from __future__ import annotations

import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests

logger = logging.getLogger(__name__)

EODHD_API_KEY = os.environ.get("EODHD_API_KEY")
_BASE_URL = "https://eodhd.com/api/eod"
_TIMEOUT = 30  # bumped from 20 -- a NAS's network + concurrent load made 20s tight
_MAX_RETRIES = 5  # for transient failures only -- 404/other 4xx still fail fast
_BASE_BACKOFF_SECONDS = 1.0
_EMPTY = pd.DataFrame(columns=["date", "ticker", "open", "high", "low", "close", "volume"])


def _backoff_delay(resp: requests.Response | None, attempt: int) -> float:
    """How long to wait before retrying a transient failure. Honors
    EODHD's Retry-After header if a response came back with one (429s
    mainly, occasionally a 503); otherwise exponential backoff (1s, 2s,
    4s, 8s, ...) plus a little jitter so a burst of tickers failing at
    the same moment don't all retry in lockstep and immediately
    re-trigger the same problem. resp is None for a network-level
    exception (no response to read a header from)."""
    if resp is not None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
    return _BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 0.5)


def _fetch_one(ticker: str, start: str, end: str | None) -> pd.DataFrame:
    """Fetch one ticker's full daily OHLCV for [start, end]. Returns an
    empty frame -- never raises -- for a 404, a non-retryable error, or a
    transient failure (network error, 429, 5xx) that's still failing
    after every retry, so one bad symbol in a multi-thousand-ticker
    universe doesn't abort the whole run; same philosophy as
    yfinance_client's old per-batch try/except, just per-ticker now since
    each ticker is its own request. Transient failures are retried first
    (see _backoff_delay) rather than treated as terminal immediately.
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

    resp = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=_TIMEOUT)
        except requests.RequestException as exc:
            if attempt == _MAX_RETRIES:
                logger.error(
                    "EODHD fetch failed for %s after %d attempts (network error: "
                    "%s) -- giving up; this ticker has no data this run, not "
                    "because it's missing/delisted. Re-running the same command "
                    "will retry it.",
                    ticker, _MAX_RETRIES, exc,
                )
                return _EMPTY
            delay = _backoff_delay(None, attempt)
            logger.warning(
                "EODHD network error for %s (%s) -- retrying in %.1fs (attempt %d/%d)",
                ticker, exc, delay, attempt, _MAX_RETRIES,
            )
            time.sleep(delay)
            continue

        if resp.status_code == 404:
            logger.warning(
                "No EODHD data for %s.US (404 -- delisted, renamed, or not "
                "an EODHD-covered US ticker)", ticker,
            )
            return _EMPTY

        if resp.status_code == 429 or resp.status_code >= 500:
            reason = "rate-limited (429)" if resp.status_code == 429 else f"server error ({resp.status_code})"
            if attempt == _MAX_RETRIES:
                logger.error(
                    "EODHD %s for %s on every retry (%d attempts) -- giving up; "
                    "this ticker has no data this run, not because it's "
                    "missing/delisted. Re-running the same command will retry it.",
                    reason, ticker, _MAX_RETRIES,
                )
                return _EMPTY
            delay = _backoff_delay(resp, attempt)
            logger.warning(
                "EODHD %s for %s -- retrying in %.1fs (attempt %d/%d)",
                reason, ticker, delay, attempt, _MAX_RETRIES,
            )
            time.sleep(delay)
            continue

        try:
            resp.raise_for_status()
        except requests.RequestException:
            logger.exception("EODHD fetch failed for %s", ticker)
            return _EMPTY

        break  # got a usable (non-429, non-5xx, non-404, non-error) response

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
    max_workers: int = 5,
) -> pd.DataFrame:
    """Fetch OHLCV for many tickers, one EODHD API call per ticker, run
    concurrently since a multi-thousand-ticker backfill would otherwise
    take a long time serially.

    max_workers default lowered from 10 to 5: at ~500-600 tickers (the
    original 3-major-index universe) EODHD never throttled this project
    at max_workers=10, but at ~2,987 tickers (once Russell 1000/2000/3000
    were added) it started returning 429s under sustained load at that
    concurrency. 5 is a conservative reduction, not a value confirmed
    against EODHD's actual published rate limit for this plan -- if 429s
    still show up in the logs at this setting, lower it further (see
    nas/first_time_setup.sh / scripts/backfill_history.py for where this
    would need to become a parameter if it's worth tuning regularly).
    Each individual 429 is also retried with backoff now regardless of
    max_workers -- see _fetch_one / _backoff_delay above -- so this knob
    is about avoiding 429s in the first place, not the only defense
    against them.

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

    empty_count = sum(1 for f in frames if f.empty)
    if empty_count:
        logger.warning(
            "%d/%d tickers returned no data this run (404s, or transient "
            "failures -- 429s, 5xx, network timeouts -- that were still "
            "failing after every retry -- see warnings/errors above for which "
            "and why, per ticker). Re-running the same command upserts rather "
            "than duplicates, so it's safe to just run it again to pick up "
            "whichever tickers failed.",
            empty_count, len(tickers),
        )

    result = pd.concat(frames, ignore_index=True)
    result = result.dropna(subset=["close"])
    return result.sort_values(["ticker", "date"]).reset_index(drop=True)
