"""
One-time cleanup for an existing data/breadth.db: collapses the
date-format inconsistency described in src/db/models.py's upsert_prices()
docstring and scripts/breadth_compute.py's read-side comment.

Background: before upsert_prices() started normalizing dates on write,
`prices` could accumulate rows in two different TEXT shapes for what is
actually the same calendar date -- "2024-07-17 00:00:00" (from an ingest
where the incoming DataFrame's `date` column was a datetime64/Timestamp
dtype) alongside plain "2024-07-17" (from an ingest where it was already
a plain string). Since prices' PRIMARY KEY is (date, ticker) and SQLite
compares TEXT primary keys byte-for-byte, those two strings are different
keys as far as INSERT OR REPLACE is concerned, even though they name the
same trading day. Two concrete symptoms on a live deployment:

  - Silent duplication: a ticker whose history spans the format change
    ends up with two rows for the same actual trading day, inflating
    row counts (avg_rows_per_ticker exceeding the number of distinct
    dates is the tell).
  - Silent breadth outage: pd.read_sql(..., parse_dates=["date"]) infers
    a single date format from whichever row is physically first in the
    table (SQLite's natural row order) and turns every row that doesn't
    match into NaT. Whichever indexes' tickers were dominated by the
    "losing" format end up with breadth_daily rows that never compute --
    with no error and no log line, because the loop in breadth_compute.py
    used to silently `continue` on an empty result. On this deployment
    that was Russell 2000, S&P 400, and S&P 600 (Russell 3000 looked fine
    only because it's the union of Russell 1000 -- unaffected -- and
    Russell 2000, and the healthy half masked the broken half).

What this script does:
  1. Renames the current `prices` table to `prices_backup_pre_date_fix`.
     Nothing is deleted -- inspect or drop that table yourself once
     you've confirmed the migration looks right.
  2. Creates a fresh `prices` table from the current schema.sql (a real
     CREATE TABLE, so its PRIMARY KEY is genuinely enforced from the
     start -- sidesteps the separate "CREATE TABLE IF NOT EXISTS never
     retroactively adds a PK" gotcha documented in models.py/README.md).
  3. Re-inserts every row from the backup with `date` normalized to
     plain YYYY-MM-DD (any " HH:MM:SS" suffix stripped), using
     INSERT OR IGNORE so that if two backup rows land on the same
     (date, ticker) after normalization, only one survives. Both rows
     represent the same trading day from the same data source, so which
     one is kept doesn't matter for correctness.
  4. Prints before/after row counts and the duplicate-group count so you
     can see exactly what changed.

Safe to re-run. If `prices_backup_pre_date_fix` already exists from a
previous run of this script, it's reused as the source of truth (not
overwritten) so re-running never loses the pre-migration snapshot.

Run once, before your next breadth_compute run:
    docker-compose run --rm pipeline python -m scripts.migrate_normalize_prices_dates
"""
from __future__ import annotations

import logging
import re

from src.db.models import SCHEMA_PATH, get_connection

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BACKUP_TABLE = "prices_backup_pre_date_fix"


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


_PRICES_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?prices\s*\(", re.IGNORECASE
)


def _prices_create_sql() -> str:
    """Pull just the `prices` CREATE TABLE statement out of schema.sql, so
    this script always matches whatever the current schema actually is
    rather than hardcoding a copy that could drift out of sync."""
    for stmt in SCHEMA_PATH.read_text().split(";"):
        if _PRICES_TABLE_RE.search(stmt):
            return stmt.strip() + ";"
    raise RuntimeError(f"Could not find a `prices` CREATE TABLE statement in {SCHEMA_PATH}")


def main():
    with get_connection() as conn:
        if _table_exists(conn, BACKUP_TABLE):
            logger.info(
                "%s already exists from a previous run -- reusing it as the source "
                "of truth instead of the current `prices` table.", BACKUP_TABLE,
            )
        else:
            if not _table_exists(conn, "prices"):
                logger.error("No `prices` table found -- nothing to migrate.")
                return
            before_count = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
            logger.info("Backing up current `prices` (%d rows) to %s", before_count, BACKUP_TABLE)
            conn.execute(f"ALTER TABLE prices RENAME TO {BACKUP_TABLE}")

        backup_count = conn.execute(f"SELECT COUNT(*) FROM {BACKUP_TABLE}").fetchone()[0]
        mixed_format = conn.execute(
            f"SELECT COUNT(*) FROM {BACKUP_TABLE} WHERE length(date) != 10"
        ).fetchone()[0]
        dupe_groups = conn.execute(
            f"""SELECT COUNT(*) FROM (
                    SELECT substr(date, 1, 10) AS d, ticker
                    FROM {BACKUP_TABLE}
                    GROUP BY d, ticker
                    HAVING COUNT(*) > 1
                )"""
        ).fetchone()[0]
        logger.info(
            "%s: %d total rows, %d with a non-normalized date, %d (date,ticker) "
            "groups that will collapse into one row",
            BACKUP_TABLE, backup_count, mixed_format, dupe_groups,
        )

        logger.info("Creating a fresh `prices` table with a genuinely-enforced PRIMARY KEY")
        conn.execute("DROP TABLE IF EXISTS prices")
        conn.execute(_prices_create_sql())

        conn.execute(
            f"""
            INSERT OR IGNORE INTO prices (date, ticker, open, high, low, close, volume)
            SELECT substr(date, 1, 10), ticker, open, high, low, close, volume
            FROM {BACKUP_TABLE}
            """
        )
        after_count = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
        logger.info(
            "Migration complete: %d rows in the new `prices` table (was %d in the backup, "
            "%d duplicate rows collapsed). The pre-migration data is preserved in %s -- "
            "drop it yourself once you've spot-checked the result, e.g.:\n"
            "  docker-compose run --rm pipeline python -m scripts.breadth_compute\n"
            "and confirm Russell 2000 / S&P 400 / S&P 600 now show data.",
            after_count, backup_count, backup_count - after_count, BACKUP_TABLE,
        )


if __name__ == "__main__":
    main()
