"""
Refreshes the index registry: pulls current constituents for every major
and sector index and replaces index_constituents / index_metadata.
Run weekly (constituent lists change infrequently -- daily refresh would
be wasted load on the source sites) via nas/weekly_refresh.sh, fired
automatically by the `scheduler` docker-compose service's in-container
cron (see nas/scheduler.crontab), or manually whenever you want to pick
up index changes immediately (e.g. after a known S&P 500 rebalance).

Resilient to a single source failing: all eight major sources (S&P
500/400/600, Nasdaq-100, Dow 30, Russell 1000/2000/3000) fall back to
their last known-good list in constituents/*.csv if the live pull fails,
rather than aborting the whole refresh -- see the CONSTITUENTS_CACHE_DIR
comment in src/ingestion/universe.py and constituents/README.md. A
source using its fallback still gets upserted into the DB below (with a
warning in the log), it's just not fresher than last time.

Must be run at least once before daily_ingest.py / breadth_compute.py --
they depend on index_constituents being populated.
"""
from __future__ import annotations

import logging

from src.db.models import init_db, upsert_index_constituents, upsert_index_metadata
from src.ingestion.universe import build_full_registry

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main():
    init_db()
    logger.info("Fetching current constituents for all indexes...")
    registry = build_full_registry()

    for key, meta in registry.items():
        logger.info("  %-32s %-6s %d tickers", key, meta["type"], len(meta["tickers"]))

    upsert_index_metadata(registry)
    upsert_index_constituents(registry)

    total_unique = len({t for meta in registry.values() for t in meta["tickers"]})
    logger.info("Refresh complete: %d indexes, %d unique tickers across all indexes",
                len(registry), total_unique)


if __name__ == "__main__":
    main()
