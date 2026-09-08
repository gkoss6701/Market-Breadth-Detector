"""
Regression coverage for the date-format bug that silently zeroed out
Russell 2000 / S&P 400 / S&P 600 breadth data (see upsert_prices()'s
docstring in src/db/models.py and scripts/migrate_normalize_prices_dates.py
for the full story). Root cause: `prices`' PK is (date, ticker) and SQLite
compares TEXT primary keys byte-for-byte, so two different string
representations of the same calendar date ("2024-07-17" vs
"2024-07-17 00:00:00") were never recognized as the same row -- silently
duplicating history on re-ingest, and silently NaT-ing out whichever
format "lost" pandas' read-time format inference.
"""
import sqlite3

import pandas as pd
import pytest

from src.db import models as db_models


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Point src.db.models at a scratch SQLite file for this test only."""
    monkeypatch.setattr(db_models, "DB_PATH", tmp_path / "test_breadth.db")
    db_models.init_db()
    return db_models


def test_upsert_prices_normalizes_date_regardless_of_incoming_dtype(db):
    """A datetime64-dtype `date` column and a plain-string `date` column
    for the same calendar date must land in the DB as the identical TEXT
    value, or they'll silently be treated as different primary keys."""
    as_datetime = pd.DataFrame({
        "date": pd.to_datetime(["2024-07-17"]),
        "ticker": ["A"], "open": [1.0], "high": [1.0], "low": [1.0],
        "close": [100.0], "volume": [10],
    })
    as_string = pd.DataFrame({
        "date": ["2024-07-17"],
        "ticker": ["B"], "open": [1.0], "high": [1.0], "low": [1.0],
        "close": [50.0], "volume": [10],
    })
    db.upsert_prices(as_datetime)
    db.upsert_prices(as_string)

    with db.get_connection() as conn:
        rows = conn.execute("SELECT date, ticker, typeof(date) FROM prices ORDER BY ticker").fetchall()

    assert rows == [("2024-07-17", "A", "text"), ("2024-07-17", "B", "text")]


def test_upsert_prices_reingest_does_not_duplicate(db):
    """Re-ingesting the same ticker/date range a second time (e.g. a
    re-run backfill) must upsert in place, not add a second row -- this
    is the exact mechanism that inflated avg_rows_per_ticker on the live
    deployment when the incoming dtype shifted between ingest runs."""
    df = pd.DataFrame({
        "date": pd.to_datetime(["2024-07-17", "2024-07-18"]),
        "ticker": ["A", "A"], "open": [1.0, 1.0], "high": [1.0, 1.0],
        "low": [1.0, 1.0], "close": [100.0, 101.0], "volume": [10, 10],
    })
    db.upsert_prices(df)
    # Same dates, different incoming dtype (plain strings this time) --
    # simulates the historical bug's trigger condition.
    df_restring = df.copy()
    df_restring["date"] = df_restring["date"].dt.strftime("%Y-%m-%d")
    db.upsert_prices(df_restring)

    with db.get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    assert count == 2  # not 4


def test_migrate_normalize_prices_dates_collapses_legacy_mixed_format(db):
    """Simulates a DB that already has the pre-fix mixed-format corruption
    (as found live on Gary's NAS deployment) and confirms the migration
    script cleans it up: normalizes every date, collapses the resulting
    duplicates, and leaves a `prices` table with a genuinely-enforced PK."""
    from scripts.migrate_normalize_prices_dates import main as migrate_main

    with db.get_connection() as conn:
        conn.executemany(
            "INSERT INTO prices (date, ticker, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("2024-07-17 00:00:00", "A", 1, 1, 1, 100.0, 10),
                ("2024-07-17", "A", 1, 1, 1, 100.0, 10),  # same day, other format
                ("2024-09-09", "AA", 1, 1, 1, 50.0, 10),
            ],
        )
        conn.commit()

    migrate_main()

    with db.get_connection() as conn:
        rows = conn.execute("SELECT date, ticker FROM prices ORDER BY ticker").fetchall()
        pk_cols = [r[1] for r in conn.execute("PRAGMA table_info(prices)").fetchall() if r[5] > 0]
        # A real UNIQUE/PK constraint should now reject a literal duplicate insert.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO prices (date, ticker, open, high, low, close, volume) "
                "VALUES ('2024-09-09', 'AA', 1, 1, 1, 1, 1)"
            )

    assert rows == [("2024-07-17", "A"), ("2024-09-09", "AA")]
    assert pk_cols == ["date", "ticker"]
