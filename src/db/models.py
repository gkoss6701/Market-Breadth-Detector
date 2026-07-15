"""
Thin DB access layer. SQLite by default (data/breadth.db) for solo/local
use; the same schema.sql is Postgres-compatible if you outgrow file-based
storage (e.g. concurrent writes from Action + dashboard causing lock
contention) -- swap the connection factory below and point DATABASE_URL
at Postgres, the rest of the code doesn't need to change.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pandas as pd

DB_PATH = Path(os.environ.get("BREADTH_DB_PATH", "data/breadth.db"))
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA_PATH.read_text())


def upsert_prices(df: pd.DataFrame) -> None:
    """df columns: date, ticker, open, high, low, close, volume"""
    with get_connection() as conn:
        df.to_sql("prices_staging", conn, if_exists="replace", index=False)
        conn.execute(
            """
            INSERT OR REPLACE INTO prices (date, ticker, open, high, low, close, volume)
            SELECT date, ticker, open, high, low, close, volume FROM prices_staging
            """
        )
        conn.execute("DROP TABLE prices_staging")


def upsert_breadth_daily(row: dict) -> None:
    with get_connection() as conn:
        cols = ", ".join(row.keys())
        placeholders = ", ".join("?" for _ in row)
        conn.execute(
            f"INSERT OR REPLACE INTO breadth_daily ({cols}) VALUES ({placeholders})",
            list(row.values()),
        )


def upsert_breadth_daily_bulk(df: pd.DataFrame) -> None:
    """Upsert many breadth_daily rows at once (full recomputed history),
    rather than one row per call. Used so the dashboard's time-series
    charts have data -- writing only 'today's' row on every run leaves
    breadth_daily with as few rows as the number of times the job has
    run, which looks like an empty chart even once metrics exist.
    df columns must match breadth_daily's schema (date, pct_above_20ma,
    pct_above_50ma, pct_above_200ma, ad_line, new_highs, new_lows,
    up_down_vol_ratio, composite_score, regime, bearish_divergence,
    bullish_divergence).
    """
    with get_connection() as conn:
        df.to_sql("breadth_daily_staging", conn, if_exists="replace", index=False)
        cols = ", ".join(df.columns)
        conn.execute(
            f"INSERT OR REPLACE INTO breadth_daily ({cols}) "
            f"SELECT {cols} FROM breadth_daily_staging"
        )
        conn.execute("DROP TABLE breadth_daily_staging")


def get_latest_breadth(n_days: int = 30) -> pd.DataFrame:
    with get_connection() as conn:
        return pd.read_sql(
            "SELECT * FROM breadth_daily ORDER BY date DESC LIMIT ?",
            conn,
            params=(n_days,),
        )


def log_alert(date: str, alert_type: str, detail: str = "") -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO alerts_sent (date, alert_type, detail) VALUES (?, ?, ?)",
            (date, alert_type, detail),
        )


def alert_already_sent_today(date: str, alert_type: str) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT 1 FROM alerts_sent WHERE date = ? AND alert_type = ? LIMIT 1",
            (date, alert_type),
        )
        return cur.fetchone() is not None
