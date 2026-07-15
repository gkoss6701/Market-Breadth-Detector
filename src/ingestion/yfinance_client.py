"""
yfinance-based data client. Starting point for prototyping -- swap for a
bulk-endpoint vendor (Polygon.io, Tiingo) once the engine/backtest logic is
validated and you need daily production reliability at 500-ticker scale.

Known yfinance limitations to design around:
  - No true bulk "all tickers, one call" endpoint like Polygon's grouped-daily.
    yf.download() with a ticker list batches internally but still issues
    many requests -- rate limits and partial failures are common at scale.
  - Occasional missing/NaN rows for illiquid names or around delistings.
  - Auto-adjusts splits/dividends by default (auto_adjust=True), which is
    what you want for breadth math -- keep it on.
"""
from __future__ import annotations

import logging
import time

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


def fetch_bulk_ohlcv(
    tickers: list[str],
    start: str,
    end: str | None = None,
    batch_size: int = 50,
    pause_seconds: float = 1.0,
) -> pd.DataFrame:
    """Fetch OHLCV for many tickers, batched to reduce rate-limit failures.

    Returns a long-format DataFrame with columns:
    [date, ticker, open, high, low, close, volume]
    """
    frames = []
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i : i + batch_size]
        logger.info("Fetching batch %d-%d of %d", i, i + len(batch), len(tickers))
        try:
            raw = yf.download(
                batch,
                start=start,
                end=end,
                group_by="ticker",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
        except Exception:
            logger.exception("Batch fetch failed for tickers: %s", batch)
            continue

        frames.append(_reshape_batch(raw, batch))
        time.sleep(pause_seconds)  # be polite; reduces throttling

    if not frames:
        return pd.DataFrame(columns=["date", "ticker", "open", "high", "low", "close", "volume"])

    result = pd.concat(frames, ignore_index=True)
    result = result.dropna(subset=["close"])
    return result.sort_values(["ticker", "date"]).reset_index(drop=True)


def _reshape_batch(raw: pd.DataFrame, batch: list[str]) -> pd.DataFrame:
    """Reshape yfinance's wide multi-ticker output into long format."""
    rows = []
    # Single-ticker download returns a flat frame, not multiindex columns.
    if len(batch) == 1:
        df = raw.copy()
        df["ticker"] = batch[0]
        df = df.reset_index().rename(
            columns={
                "Date": "date",
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            }
        )
        return df[["date", "ticker", "open", "high", "low", "close", "volume"]]

    for ticker in batch:
        if ticker not in raw.columns.get_level_values(0):
            continue
        sub = raw[ticker].copy()
        sub["ticker"] = ticker
        sub = sub.reset_index().rename(
            columns={
                "Date": "date",
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            }
        )
        rows.append(sub[["date", "ticker", "open", "high", "low", "close", "volume"]])

    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(
        columns=["date", "ticker", "open", "high", "low", "close", "volume"]
    )
