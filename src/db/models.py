"""
Thin DB access layer. SQLite by default (data/breadth.db) for solo/local
use; the same schema.sql is Postgres-compatible if you outgrow file-based
storage (e.g. concurrent writes from Action + dashboard causing lock
contention) -- swap the connection factory below and point DATABASE_URL
at Postgres, the rest of the code doesn't need to change.

Phase 2 note (schema change): breadth_daily's primary key changed from
(date) to (index_key, date), and two new tables were added
(index_constituents, index_metadata). If you're upgrading an existing
deployment, `CREATE TABLE IF NOT EXISTS` will NOT alter your old
breadth_daily table's structure. Simplest fix: delete the old
data/breadth.db and re-run backfill_history + breadth_compute -- it's
cheap to regenerate and safer than an in-place migration.
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


# ---------------------------------------------------------------------
# Index registry / constituents
# ---------------------------------------------------------------------

def upsert_index_metadata(registry: dict[str, dict]) -> None:
    """registry: {index_key: {'label': ..., 'type': 'major'|'sector', ...}}"""
    with get_connection() as conn:
        rows = [(k, v["label"], v["type"]) for k, v in registry.items()]
        conn.executemany(
            "INSERT OR REPLACE INTO index_metadata (index_key, label, index_type) VALUES (?, ?, ?)",
            rows,
        )


def upsert_index_constituents(registry: dict[str, dict]) -> None:
    """registry: {index_key: {'tickers': [...], ...}}. Replaces each
    index's full membership list (not an incremental add), since
    membership can shrink (removals) as well as grow."""
    with get_connection() as conn:
        for index_key, meta in registry.items():
            conn.execute("DELETE FROM index_constituents WHERE index_key = ?", (index_key,))
            rows = [(index_key, t) for t in meta["tickers"]]
            conn.executemany(
                "INSERT OR REPLACE INTO index_constituents (index_key, ticker) VALUES (?, ?)",
                rows,
            )


def get_index_constituents(index_key: str) -> list[str]:
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT ticker FROM index_constituents WHERE index_key = ? ORDER BY ticker", (index_key,)
        )
        return [r[0] for r in cur.fetchall()]


def get_all_universe_tickers() -> list[str]:
    """Union of tickers across every registered index -- what daily_ingest
    needs to pull, deduped so a ticker in multiple indexes is fetched once."""
    with get_connection() as conn:
        cur = conn.execute("SELECT DISTINCT ticker FROM index_constituents ORDER BY ticker")
        return [r[0] for r in cur.fetchall()]


def get_index_registry() -> pd.DataFrame:
    """Returns [index_key, label, index_type] for every known index --
    what the dashboard selector is built from."""
    with get_connection() as conn:
        return pd.read_sql("SELECT * FROM index_metadata ORDER BY index_type, label", conn)


# ---------------------------------------------------------------------
# Breadth history
# ---------------------------------------------------------------------

def upsert_breadth_daily_bulk(df: pd.DataFrame) -> None:
    """Upsert many breadth_daily rows at once (full recomputed history
    for one or more indexes), rather than one row per call. Used so the
    dashboard's time-series charts have data -- writing only 'today's'
    row on every run leaves breadth_daily with as few rows as the number
    of times the job has run, which looks like an empty chart even once
    metrics exist.
    df columns must match breadth_daily's schema: index_key, date,
    pct_above_20ma, pct_above_50ma, pct_above_200ma, ad_line, new_highs,
    new_lows, up_down_vol_ratio, composite_score, regime,
    bearish_divergence, bullish_divergence.
    """
    with get_connection() as conn:
        df.to_sql("breadth_daily_staging", conn, if_exists="replace", index=False)
        cols = ", ".join(df.columns)
        conn.execute(
            f"INSERT OR REPLACE INTO breadth_daily ({cols}) "
            f"SELECT {cols} FROM breadth_daily_staging"
        )
        conn.execute("DROP TABLE breadth_daily_staging")


def get_latest_breadth(index_key: str, n_days: int = 30) -> pd.DataFrame:
    with get_connection() as conn:
        return pd.read_sql(
            "SELECT * FROM breadth_daily WHERE index_key = ? ORDER BY date DESC LIMIT ?",
            conn,
            params=(index_key, n_days),
        )


def get_breadth_history(index_key: str) -> pd.DataFrame:
    """Full history for one index, ascending by date -- what the
    dashboard charts read."""
    with get_connection() as conn:
        return pd.read_sql(
            "SELECT * FROM breadth_daily WHERE index_key = ? ORDER BY date",
            conn,
            params=(index_key,),
            parse_dates=["date"],
        )


def get_latest_snapshot_all_indexes() -> pd.DataFrame:
    """Most recent row per index -- used for a cross-index summary view
    (e.g. 'which sectors are strongest right now')."""
    with get_connection() as conn:
        return pd.read_sql(
            """
            SELECT b.* FROM breadth_daily b
            INNER JOIN (
                SELECT index_key, MAX(date) AS max_date
                FROM breadth_daily GROUP BY index_key
            ) latest
            ON b.index_key = latest.index_key AND b.date = latest.max_date
            """,
            conn,
        )


# ---------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------

def log_alert(index_key: str, date: str, alert_type: str, detail: str = "") -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO alerts_sent (index_key, date, alert_type, detail) VALUES (?, ?, ?, ?)",
            (index_key, date, alert_type, detail),
        )


def alert_already_sent_today(index_key: str, date: str, alert_type: str) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT 1 FROM alerts_sent WHERE index_key = ? AND date = ? AND alert_type = ? LIMIT 1",
            (index_key, date, alert_type),
        )
        return cur.fetchone() is not None
