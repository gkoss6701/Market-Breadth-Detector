"""
Refreshes the index registry: pulls current constituents for every major
and sector index and replaces index_constituents / index_metadata.
Run weekly (constituent lists change infrequently -- daily refresh would
be wasted Wikipedia load) via .github/workflows/refresh_universe.yml, or
manually whenever you want to pick up index changes immediately (e.g.
after a known S&P 500 rebalance).

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
    logger.info("Fetching current constituents for all indexes (Wikipedia)...")
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
