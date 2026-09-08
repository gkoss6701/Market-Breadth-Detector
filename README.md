# Market Breadth Detector

Internal-market-breadth pipeline for pre-trade regime filtering on swing and
day trades. Computes advance-decline, new highs/lows, %-above-moving-average,
up/down volume, and a composite momentum gauge with regime classification
and price/breadth divergence detection -- **independently for every major
index and sector index**, not just one market-wide read.

Structured the same way as the Central Ohio Kayak Dashboard: scheduled
ingestion -> compute -> persist (SQLite) -> Streamlit dashboard + Pushover
alerts. Runs as Docker containers on a local NAS (QNAP), scheduled by an
in-container cron daemon (a `scheduler` service in docker-compose.yml)
rather than GitHub Actions or QNAP's own Task Scheduler -- see
[NAS_SETUP.md](NAS_SETUP.md) for the full deployment guide. GitHub now
holds code only, no automation and no committed database.

## Status

Phase 2: multi-index. Price data comes from EODHD (see `Data source`
below). Not yet validated for live trading decisions -- see
`Backtesting caveats` below before trusting any regime signal with real
capital.

## What's new in Phase 2

Phase 1 tracked one hand-picked ~30-ticker universe. Phase 2 replaces
that with a **registry of indexes**, each with its own dynamically-fetched
current constituent list and its own independent breadth history:

- **Major indexes**: S&P 500, Nasdaq-100, Dow Jones Industrial Average,
  S&P 400 (Mid Cap), S&P 600 (Small Cap), Russell 1000, Russell 2000,
  Russell 3000.
- **Sector indexes**: all 11 GICS sectors (Information Technology, Health
  Care, Financials, Consumer Discretionary, Communication Services,
  Industrials, Consumer Staples, Energy, Utilities, Real Estate,
  Materials), derived from the S&P 500's own sector classification rather
  than a separate data pull.
- The dashboard has a selector -- pick any index or sector and see its
  own composite score, regime, and charts, plus a cross-index summary
  table showing every index's latest snapshot at once.

**Russell 1000/2000/3000 source note.** Neither Wikipedia nor SlickCharts
publishes a full Russell constituent list (checked both directly).
ChartMill.com does, sourced from its JSON REST API rather than scraping
its (JS-rendered) page HTML -- see `_fetch_chartmill_index_tickers()` in
`src/ingestion/universe.py` for how, and the one open question: whether
ChartMill's Cloudflare protection permits unattended requests the way
this runs on the NAS (confirmed working live from a browser session,
not yet from a headless scheduled run). If it ever gets blocked, the
fetch fails loudly rather than silently returning a wrong list -- see the
exception message for next steps.

**Schema change / migration**: `breadth_daily`'s primary key changed from
`(date)` to `(index_key, date)`, and two new tables were added
(`index_constituents`, `index_metadata`). If you have an existing
`data/breadth.db` from phase 1, `CREATE TABLE IF NOT EXISTS` will **not**
migrate it -- simplest fix is to delete `data/breadth.db` and re-run the
setup sequence below from scratch. It's cheap to regenerate.

## First-time setup order (important)

There's now a dependency before ingestion can even start: the index
registry has to exist before `daily_ingest`/`backfill_history` know which
tickers to pull.

1. **Refresh Universe**: `python -m scripts.refresh_universe` (or, on the
   NAS, part of `nas/first_time_setup.sh`). Populates
   `index_constituents` / `index_metadata` for every major + sector index.
2. **Backfill History** (default 2 years): `python -m
   scripts.backfill_history --years 2`. Pulls OHLCV for the full union of
   tickers across every index. This is a bigger pull than phase 1 (~3,000
   unique tickers vs. ~30, dominated by the Russell 3000 -- S&P 400/600
   are largely distinct from the S&P 500 by construction, not a subset the
   way sectors are, but mostly overlap with the Russell indexes) --
   expect several minutes via EODHD (one API call per ticker, run
   concurrently; see `Data source` below).
3. **Breadth Compute**: `python -m scripts.breadth_compute`. Computes
   full history for every registered index in one pass.
4. From here on (on the NAS): **Daily Ingest** -> **Breadth Compute**
   run in sequence via `nas/daily_pipeline.sh`, scheduled by NAS Task
   Scheduler. **Refresh Universe** runs on its own weekly schedule via
   `nas/weekly_refresh.sh` (constituent lists change rarely, no need to
   re-pull daily). See [NAS_SETUP.md](NAS_SETUP.md) for the full setup.

Skipping step 1 means `daily_ingest`/`backfill_history` find
`index_constituents` empty and exit with a clear error rather than
silently doing nothing.

## Quickstart

**Running this for real (NAS/Docker)**: see [NAS_SETUP.md](NAS_SETUP.md)
for the full QNAP + Container Station deployment guide -- that's the
supported way to run this continuously with scheduled updates.

**Local development** (editing code, running tests, poking at the
dashboard without Docker):

```bash
pip install -r requirements.txt

# 1. Populate the index registry (S&P 500, Nasdaq-100, Dow 30, 11 sectors)
python -m scripts.refresh_universe

# 2. One-time backfill across the full multi-index ticker union
python -m scripts.backfill_history --years 2

# 3. Compute breadth metrics for every index
python -m scripts.breadth_compute

# 4. Run tests
pytest

# 5. Local dashboard (index selector included)
streamlit run dashboard/streamlit_app.py
```

## Architecture

```
src/ingestion/   -- data pulls: EODHD client + index registry (universe.py)
src/engine/      -- breadth metrics, composite score, divergence detection
src/backtest/    -- signal generators, walk-forward harness, weight optimizer
src/db/          -- SQLite schema + access layer (multi-index aware)
src/alerts/      -- Pushover notifications (per-index, gated by ALERT_INDEX_KEYS)
nas/             -- shell scripts the `scheduler` service's cron invokes (see NAS_SETUP.md)
scripts/         -- entry points the nas/ scripts call
dashboard/       -- Streamlit app with index selector
examples/        -- standalone runnable walkthrough (single-index, unaffected by phase 2)
tests/           -- lookahead-safety, metric sanity, and multi-index scoping checks
```

## Index registry (`src/ingestion/universe.py`)

`build_full_registry()` fetches all current constituents in one pass:

- `fetch_sp500_with_sectors()` -- Wikipedia's S&P 500 table, which
  conveniently includes a GICS Sector column per company. This single
  pull backs both the `sp500` index AND all 11 sector indexes (sector
  indexes are just this same data grouped by sector, not a separate
  scrape).
- `fetch_dow30()` -- a separate Wikipedia article,
  [`List_of_Dow_Jones_Industrial_Average_companies`](https://en.wikipedia.org/wiki/List_of_Dow_Jones_Industrial_Average_companies)
  (the main `Dow_Jones_Industrial_Average` page itself dropped its
  component table at some point and now only has historical annual
  returns -- the code was still pointed at that page until this was
  fixed).
- `fetch_nasdaq100()` / `fetch_sp400()` / `fetch_sp600()` -- SlickCharts
  (`slickcharts.com/nasdaq100`, `/sp400`, `/sp600`), not Wikipedia.
  Wikipedia's Nasdaq-100 page no longer carries a per-company table at
  all (checked directly: only sector-weight percentages and
  historical-return tables remain, and there's no separate "List of
  Nasdaq-100 companies" article the way there is for the Dow), and
  Wikipedia has no equivalent page for S&P 400/600 at all. All three
  share `_fetch_slickcharts_index()`. SlickCharts is a third-party site,
  not an official index source -- same caveat as the EODHD-vs-Wikipedia
  choice below, don't treat it as guaranteed-stable.
- `fetch_russell1000()` / `fetch_russell2000()` / `fetch_russell3000()`
  -- ChartMill.com, via its JSON REST API rather than scraping the page
  HTML (ChartMill's page is a JS-rendered SPA; a plain `requests.get()`
  on the page URL itself would return an empty shell). All three share
  `_fetch_chartmill_index_tickers()`: resolve the index's numeric id from
  its URL slug, then page through its member list. See the
  `CHARTMILL_BASE_URL` comment in `src/ingestion/universe.py` for the one
  open question (unattended-request behavior against ChartMill's
  Cloudflare protection).

All seven scraped/API fetchers validate the result count against an
`expected_rows` range and raise a clear error with guidance if it falls
outside that range or the expected shape isn't found, rather than
silently returning an empty/wrong list. The four Wikipedia/SlickCharts
fetchers locate their table by searching for the expected ticker column
rather than a hardcoded table index (source page table ordering can
shift) -- though as of Sept 2026 that guard had a gap: a table with a
purely numeric column (e.g. a "Year" column of 4-digit years) could
score as "ticker-like" under the content-based fallback check and get
picked by mistake instead of raising. The ticker-detection regex now
requires at least one letter to close that gap.

### Constituent base/fallback lists (`constituents/`)

All eight major sources (S&P 500 w/ sector, Dow 30, Nasdaq-100, S&P 400,
S&P 600, Russell 1000, Russell 2000, Russell 3000) each have a
git-tracked CSV snapshot in `constituents/` -- see
`constituents/README.md` for the exact contract. Short version:
`build_full_registry()` writes each source's fresh list to its CSV on a
successful pull, and reads the existing CSV instead (without touching
it) if the live pull fails, so one bad scrape never deletes or
overwrites the last known-good list -- it just means that source stays
at its previous state for one more weekly refresh cycle, logged as a
warning rather than aborting the whole registry build. This also fixed
a pre-existing issue where any single fetcher failing used to abort
`build_full_registry()` entirely, discarding sources that *had*
refreshed successfully in the same run.

The three Russell/ChartMill sources carry one extra open caveat beyond
the other five: it's unconfirmed whether ChartMill's Cloudflare
protection tolerates unattended scripted requests from the NAS the way
it did from the interactive browser session used to build the base
files -- if it starts blocking, Russell refreshes just keep falling back
to the cached files (logged as a warning each run) rather than breaking
anything, same as any other source hitting this fallback path.

In Docker, `constituents/` is mounted as a volume (`./constituents:/app/constituents`
in `docker-compose.yml`), not just baked into the image -- otherwise a
successful cache write from inside a `docker-compose run --rm pipeline
...` invocation would be lost the instant that container is removed,
which is every pipeline run.

## Data source

OHLCV comes from EODHD (`src/ingestion/eodhd_client.py`), not yfinance.
Requires `EODHD_API_KEY` in the environment (see `.env.example`) --
yfinance needed no API key at all, so this is a new required setup step.
One HTTP call per ticker (EODHD's plan here doesn't include a bulk
multi-ticker historical-price endpoint), run concurrently
(`max_workers`, default 5) since the full multi-index universe is a few
thousand tickers; each call returns adjusted-close alongside raw OHLCV,
and `close` is set to the adjusted value so moving averages/new-highs
don't show a fake "crash" on every split -- same reasoning as yfinance's
old `auto_adjust=True`. This replaced yfinance specifically because it
lacked a true bulk endpoint and had frequent rate-limit/partial-failure
behavior at ~500-600 ticker scale. EODHD wasn't immune to the same
problem once the universe grew past that scale, though: `max_workers`
was lowered from 10 to 5 after Russell 1000/2000/3000 pushed the full
union to ~2,987 tickers and 429s started appearing under sustained load
at the old concurrency -- `_fetch_one` in `eodhd_client.py` now also
retries a 429 with backoff (honoring `Retry-After` when EODHD sends it)
instead of treating it as a terminal failure, so a rate-limited ticker
gets a second chance instead of silently ending up with no data for
that run. Constituent
lists still come from Wikipedia/SlickCharts scraping (see above), not
EODHD -- EODHD's official index-constituents endpoint
(`mp_index_components`) is a separate Marketplace add-on this project's
current plan doesn't include (confirmed via a live 403), so switching to
it would mean either upgrading that plan or keeping the scraped sources
as-is.

**Survivorship bias**: every index fetcher returns *current* constituents.
Backtesting with today's membership against years of history excludes
stocks that were removed (usually because they were struggling), which
inflates historical breadth readings. Same caveat as phase 1, now applies
per-index. Accept it for prototyping and document it in any backtest
report, or pay for point-in-time membership (Polygon.io, Norgate, CRSP)
before trusting a backtest for live decisions.

## Backtesting caveats

- **Two separate questions, don't conflate them**: does a breadth regime
  *filter* improve a price signal's odds (`backtest_filter`), vs. does a
  divergence flag work as a standalone *timing* signal
  (`backtest_divergence_signal`).
- **Train/holdout discipline**: tune composite weights and regime
  thresholds only on the `train` window (`run_walk_forward`); the
  `holdout` window is reported once, not iterated on.
- **Sample size**: every backtest summary reports `n_trades` and a
  `low_sample_warning` flag below 30 observations. A Sharpe improvement
  on 15 trades is noise, not a result -- always check this before trusting
  a headline number.
- **Costs**: `apply_transaction_costs` subtracts a basis-point estimate
  from every trade return by default. Don't compare a filtered strategy
  (fewer, more selective trades) against an unfiltered one without costs
  applied to both -- it makes the filter look better than it is.
- **Lookahead**: all rolling calculations in `src/engine/metrics.py` and
  `src/backtest/runner.py` use only trailing data. `tests/test_engine.py`
  includes regression tests (truncate-and-recompute) to catch accidental
  leakage if you modify these.
- **Backtest tooling is still single-index** (`src/backtest/`). Phase 2
  added multi-index breadth *computation and display*; extending
  `optimize_weights`/`run_walk_forward` to validate weights per-index
  (rather than one global set of weights/thresholds applied everywhere)
  is a natural phase 3.

## Infrastructure

Runs entirely on a local NAS via Docker (`Dockerfile` +
`docker-compose.yml`), scheduled by an in-container cron daemon rather
than GitHub Actions or QNAP's own Task Scheduler (not every QNAP model
exposes that). Full deployment walkthrough: **[NAS_SETUP.md](NAS_SETUP.md)**.
Short version:

- `docker-compose up -d dashboard` -- always-on Streamlit UI
  (`restart: unless-stopped`), reachable at `http://<nas-ip>:8501`.
- `docker-compose up -d scheduler` -- always-on (`restart: unless-stopped`)
  cron daemon that fires the two jobs below automatically, pinned to
  `America/New_York` so it's correct across DST without any NAS-timezone
  conversion. See `nas/scheduler.crontab` for the exact schedule.
- `docker-compose run --rm pipeline bash nas/daily_pipeline.sh` -- runs
  `daily_ingest` then `breadth_compute` in sequence. Fired by `scheduler`
  weekdays after US market close; also runnable manually any time.
- `docker-compose run --rm pipeline bash nas/weekly_refresh.sh` -- runs
  `refresh_universe`. Fired by `scheduler` weekly (constituent lists
  change infrequently); also runnable manually any time.
- `nas/first_time_setup.sh` -- one-time chain of
  refresh -> backfill -> compute, run manually before starting `scheduler`.

Both scheduled scripts fail fast and loudly (non-zero exit) and send a
best-effort Pushover notification on failure, so a broken run doesn't
sit unnoticed.

### Required secrets (for Pushover alerts)

Set in a local `.env` file on the NAS (see `.env.example`), not GitHub
secrets -- nothing runs on GitHub anymore: `MARKET_BREADTH_PUSHOVER_API`
(your Pushover application token) and `PUSHOVER_KEY`
(your Pushover user key) -- both from https://pushover.net.

### Alert scope (`ALERT_INDEX_KEYS`)

With 19 indexes (8 major + 11 sector) computed daily, alerting on every
single regime flip would be noisy. `ALERT_INDEX_KEYS` (env var,
comma-separated index_keys, e.g. `sp500,nasdaq100`) controls which
indexes actually fire a Pushover notification. Defaults to `sp500` only.
Every index still gets its regime/divergence computed and stored
regardless -- this only gates the notification, not the data, so the
dashboard always shows everything even if you only get pinged about the
S&P 500.

### Data persistence and backups

`data/breadth.db` lives on the NAS disk (mounted into both containers via
the `./data` volume in `docker-compose.yml`) -- it's no longer committed
to git the way the old GitHub Actions setup committed it after every run
(that was a workaround for Actions runners being ephemeral; the NAS disk
is genuinely persistent, so it isn't needed here). Make sure `data/` is
covered by whatever backup/snapshot routine you already run on the NAS --
it's the only copy of your breadth history now.

`constituents/*.csv` is the opposite case: it IS committed to git (ships
with the repo as a current base snapshot) AND mounted as a volume
(`./constituents`), so weekly `refresh_universe` writes on the NAS
persist across pipeline runs instead of being baked-image dead weight.
See [Constituent base/fallback lists](#constituent-basefallback-lists-constituents)
above.

### Scaling beyond SQLite

SQLite is fine for solo/local use on a NAS. Multi-index scale
(~3,000 tickers, 19 indexes x ~2 years of daily rows) is still
comfortably within SQLite's range. If you hit file-locking issues running
the dashboard and
a scheduled pipeline run concurrently, or want a shorter refresh cycle
than daily, move to Postgres (a small Postgres container alongside these
two, or Supabase's free tier) -- the schema in `src/db/schema.sql` is
Postgres-compatible as written.

## Weight/threshold validation (`src/backtest/optimize.py`)

`optimize_weights` grid-searches composite weight combinations, but is
structurally limited to a train-window slice you pass it -- it has no
access to holdout data, so it can't overfit to it by construction.
`evaluate_on_holdout` scores exactly one chosen config against holdout,
meant to be called once. Every result carries `n_trades` and
`low_sample_warning`; `optimize_weights` prefers a winner outside the
low-sample zone when one exists, but always inspect the full results
table (`results_table`), not just the argmax -- a narrow winner
surrounded by much worse neighbors in the grid is a red flag even
within train.

Currently uses one global weight config for every index. Sector
universes are much smaller than the S&P 500 (tens of tickers vs.
hundreds), so their z-scores are noisier -- if you validate weights per
index in phase 3, expect sector indexes to want a wider z-score window
(phase 1's sector module used 90 days vs. the market-wide default of 60)
to compensate.

## Next steps

- Validate composite weights/regime thresholds per-index (not just
  market-wide) once enough real history has accumulated via the daily
  pipeline.
- Consider upgrading the EODHD plan to include the Marketplace
  `mp_index_components` endpoint -- would replace the Wikipedia/SlickCharts/
  ChartMill scraping in `src/ingestion/universe.py` with a single official,
  vendor-maintained source across 100+ indexes. Not urgent now that Russell
  1000/2000/3000 are covered via ChartMill, but would remove the one open
  question there (unattended-request behavior against ChartMill's
  Cloudflare protection -- see the `Russell 1000/2000/3000 source note`
  above) and consolidate four scrape targets into one paid API.
- Confirm the ChartMill-sourced Russell fetchers actually succeed on a
  real unattended NAS run (`nas/weekly_refresh.sh`) rather than only from
  a live browser session -- see the source note above.
- Sector relative-strength ranking (which sectors are leading/lagging
  right now) is available via the dashboard's cross-index summary table;
  a dedicated rotation view (e.g. a rank-over-time chart) is a natural
  extension.
