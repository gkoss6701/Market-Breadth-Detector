# Constituent base/fallback lists

Each CSV here is the last known-good constituent list for one of the
eight major index sources: `sp500.csv` (ticker + GICS sector),
`nasdaq100.csv`, `dow30.csv`, `sp400.csv`, `sp600.csv`, `russell1000.csv`,
`russell2000.csv`, `russell3000.csv` (ticker only, except sp500).

Two jobs:

1. **Base snapshot.** These ship with the repo so a fresh deployment
   starts with a real, current constituent list instead of an empty
   registry, before `refresh_universe` has ever run successfully.
2. **Fallback cache.** `build_full_registry()` (in
   `src/ingestion/universe.py`) reads/writes these automatically:
   - A successful live pull for a source overwrites that source's file
     with the fresh result.
   - A failed live pull falls back to reading that source's existing
     file instead -- and does **not** touch it. A bad pull (site
     restructuring, a network blip, a block) never deletes or overwrites
     good data; it just means that source's list stays at its last
     known-good state for one more refresh cycle, logged as a warning
     rather than failing the whole run.

Each file's own last-modified time (`git log` on the file, or the
filesystem mtime on the NAS) is the "last refreshed" timestamp -- there's
no separate timestamp column by design, to keep these as plain diffable
ticker lists.

## Regenerating from scratch

Normally you never touch these by hand -- `python -m scripts.refresh_universe`
(weekly on the NAS via `nas/weekly_refresh.sh`) keeps them current
automatically. The snapshot committed here was pulled 2026-09-07:
S&P 500 (503 tickers, from Wikipedia), Dow 30 (30, Wikipedia), Nasdaq-100
(102, SlickCharts), S&P 400 (400, SlickCharts), S&P 600 (602, SlickCharts),
Russell 1000 (1013, ChartMill), Russell 2000 (1948, ChartMill), Russell
3000 (2961, ChartMill -- derived as the verified union of Russell 1000 +
Russell 2000 rather than re-transcribed, since the two have no overlap).

All eight sources go through the same fallback wrapper in
`src/ingestion/universe.py` (`_fetch_major_index_with_fallback` /
`_fetch_sp500_with_fallback`) -- there's no remaining source that isn't
covered. The one open caveat is specific to the three Russell/ChartMill
files: ChartMill's Cloudflare protection may or may not tolerate
unattended scripted requests from the NAS the way it did from an
interactive browser session (see `CHARTMILL_BASE_URL` in
`src/ingestion/universe.py`) -- if it starts blocking those, the
practical effect is simply that Russell refreshes keep falling back to
these cached files (logged as a warning) until someone regenerates them
manually the same way this snapshot was built.
