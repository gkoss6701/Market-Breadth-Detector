# Market Breadth Detector

Internal-market-breadth pipeline for pre-trade regime filtering on swing and
day trades. Computes advance-decline, new highs/lows, %-above-moving-average,
up/down volume, and a composite momentum gauge with regime classification
and price/breadth divergence detection -- **independently for every major
index and sector index**, not just one market-wide read.

Structured the same way as the Central Ohio Kayak Dashboard: scheduled
ingestion (GitHub Actions) -> compute -> persist (SQLite) -> Streamlit
dashboard + Twilio alerts.

## Status

Phase 2: multi-index. Prototyping on `yfinance`. Not yet validated for
live trading decisions -- see `Backtesting caveats` below before trusting
any regime signal with real capital.

## What's new in Phase 2

Phase 1 tracked one hand-picked ~30-ticker universe. Phase 2 replaces
that with a **registry of indexes**, each with its own dynamically-fetched
current constituent list and its own independent breadth history:

- **Major indexes**: S&P 500, Nasdaq-100, Dow Jones Industrial Average
  (Russell 2000 is *not* included -- see caveat below).
- **Sector indexes**: all 11 GICS sectors (Information Technology, Health
  Care, Financials, Consumer Discretionary, Communication Services,
  Industrials, Consumer Staples, Energy, Utilities, Real Estate,
  Materials), derived from the S&P 500's own sector classification rather
  than a separate data pull.
- The dashboard has a selector -- pick any index or sector and see its
  own composite score, regime, and charts, plus a cross-index summary
  table showing every index's latest snapshot at once.

**Russell 2000 is not implemented.** There is no reliable free source for
the full, current ~2000-ticker constituent list (Wikipedia doesn't
maintain a complete member list, and free APIs cap constituent
endpoints). See `fetch_russell2000()` in `src/ingestion/universe.py` for
the specific options (iShares IWM holdings CSV, or a paid vendor) if you
want to add it.

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

1. **Refresh Universe** (Actions tab, `workflow_dispatch`) -- or locally:
   `python -m scripts.refresh_universe`. Populates `index_constituents` /
   `index_metadata` for every major + sector index.
2. **Backfill History** (`workflow_dispatch`, default 2 years) -- or
   locally: `python -m scripts.backfill_history --years 2`. Pulls OHLCV
   for the full union of tickers across every index. This is a bigger
   pull than phase 1 (~500-600 unique tickers vs. ~30) -- expect several
   minutes and occasional yfinance rate-limit retries.
3. **Breadth Compute** (`workflow_dispatch`) -- or locally:
   `python -m scripts.breadth_compute`. Computes full history for every
   registered index in one pass.
4. From here on: **Daily Ingest** -> **Breadth Compute** (chained
   automatically) keeps everything current. **Refresh Universe** runs on
   its own weekly schedule (constituent lists change rarely, no need to
   re-pull daily).

Skipping step 1 means `daily_ingest`/`backfill_history` find
`index_constituents` empty and exit with a clear error rather than
silently doing nothing.

## Quickstart

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
src/ingestion/   -- data pulls: yfinance client + index registry (universe.py)
src/engine/      -- breadth metrics, composite score, divergence detection
src/backtest/    -- signal generators, walk-forward harness, weight optimizer
src/db/          -- SQLite schema + access layer (multi-index aware)
src/alerts/      -- Twilio SMS notifications (per-index, gated by ALERT_INDEX_KEYS)
scripts/         -- entry points run by GitHub Actions
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
- `fetch_nasdaq100()` / `fetch_dow30()` -- separate Wikipedia pages,
  located by searching for the table with the expected ticker column
  rather than a hardcoded table index (Wikipedia table ordering on a page
  can shift).

Each fetcher raises a clear `RuntimeError` with guidance if Wikipedia's
page layout changes and the expected table can't be found, rather than
silently returning an empty/wrong list.

## Data source

Starting on `yfinance` (free) for prototyping. Known limitations:
batches rather than true bulk pulls, occasional missing rows, rate limits
at this scale (~500-600 tickers). Swap `src/ingestion/yfinance_client.py`
for a Polygon.io or Tiingo bulk-endpoint client once the engine/backtest
logic is validated and you need daily production reliability at full
multi-index scale.

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

Three workflows now, each with a clear dependency order:

- `.github/workflows/refresh_universe.yml` -- weekly (Saturday), pulls
  current constituents for every index. Also runnable on demand.
- `.github/workflows/daily_ingest.yml` -- pulls prior day's OHLCV for the
  full multi-index ticker union, commits updated SQLite DB. Runs ~4:30pm
  ET on weekdays.
- `.github/workflows/breadth_compute.yml` -- chained after ingestion,
  computes metrics/regime for every index in one pass, fires Twilio
  alerts on regime flips or new divergence flags (deduped via
  `alerts_sent`, gated by `ALERT_INDEX_KEYS`).

Kept as separate workflows so failures at each stage are easy to isolate.

### Required GitHub Actions secrets (for Twilio alerts)

`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`,
`ALERT_TO_NUMBER`

### Alert scope (`ALERT_INDEX_KEYS`)

With 14 indexes (3 major + 11 sector) computed daily, alerting on every
single regime flip would be noisy. `ALERT_INDEX_KEYS` (env var,
comma-separated index_keys, e.g. `sp500,nasdaq100`) controls which
indexes actually fire SMS. Defaults to `sp500` only. Every index still
gets its regime/divergence computed and stored regardless -- this only
gates the SMS, not the data, so the dashboard always shows everything
even if you only get texted about the S&P 500.

### Scaling beyond SQLite

SQLite is fine for solo/local use and is what the Actions workflows commit
back to the repo. Multi-index scale (~500-600 tickers, 14 indexes x ~2
years of daily rows) is still comfortably within SQLite's range. If you
hit file-locking issues running the dashboard and the Action
concurrently, or want a shorter refresh cycle than daily, move to
Postgres (Supabase free tier is low-friction) -- the schema in
`src/db/schema.sql` is Postgres-compatible as written.

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
  workflows.
- Swap yfinance for a bulk vendor (Polygon.io/Tiingo) to handle the full
  multi-index ticker union more reliably at scale.
- Consider adding Russell 2000 via a paid data source if small-cap
  breadth becomes relevant to your trading.
- Sector relative-strength ranking (which sectors are leading/lagging
  right now) is available via the dashboard's cross-index summary table;
  a dedicated rotation view (e.g. a rank-over-time chart) is a natural
  extension.
