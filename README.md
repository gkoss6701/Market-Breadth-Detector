# Market Breadth Detector

Internal-market-breadth pipeline for pre-trade regime filtering on swing and
day trades. Computes advance-decline, new highs/lows, %-above-moving-average,
up/down volume, and a composite momentum gauge with regime classification
and price/breadth divergence detection.

Structured the same way as the Central Ohio Kayak Dashboard: scheduled
ingestion (GitHub Actions) -> compute -> persist (SQLite) -> Streamlit
dashboard + Twilio alerts.

## Status

Prototyping on `yfinance` for a ~30-ticker starter universe. Not yet
validated for live trading decisions -- see `Backtesting caveats` below
before trusting any regime signal with real capital.

## First-time setup order (important)

The rolling windows in `src/engine/metrics.py` need real history to
produce anything -- `pct_above_200ma` needs 200 trading days, new-high/low
counts use a 252-day lookback, and the composite z-score needs 60 days
just for its first non-NaN value. `daily_ingest.py` only pulls a small
trailing window per run (by design, to stay fast/cheap daily), so on a
brand new repo it can take the better part of a year of daily runs to
accumulate enough history on its own. **Run the one-time backfill first**
or the dashboard will show rows with `composite_score`/`regime` all blank
even though the workflows "succeeded":

1. Run **Backfill History (run once)** from the Actions tab (`workflow_dispatch`,
   default 2 years) -- or locally: `python -m scripts.backfill_history --years 2`
2. Then run **Breadth Compute** once to populate `breadth_daily` from the
   backfilled prices.
3. From here on, **Daily Ingest** -> **Breadth Compute** (chained
   automatically) keeps it current.

## Quickstart

```bash
pip install -r requirements.txt

# 1. One-time backfill (only needed once, or if starting fresh)
python -m scripts.backfill_history --years 2

# 2. Compute breadth metrics from backfilled history
python -m scripts.breadth_compute

# 3. End-to-end example on a small universe (fetch, compute, backtest)
python -m examples.run_backtest_example

# 4. Run tests
pytest

# 5. Local dashboard
streamlit run dashboard/streamlit_app.py
```

## Architecture

```
src/ingestion/   -- data pulls (yfinance now; Polygon/Tiingo later at scale)
src/engine/      -- breadth metrics, composite score, divergence detection
src/backtest/    -- signal generators, walk-forward harness, weight optimizer
src/db/          -- SQLite schema + access layer
src/alerts/      -- Twilio SMS notifications
scripts/         -- entry points run by GitHub Actions
dashboard/       -- Streamlit app
examples/        -- standalone runnable walkthrough
tests/           -- lookahead-safety and metric sanity checks
```

## Data source

Starting on `yfinance` (free) for prototyping. Known limitations:
batches rather than true bulk pulls, occasional missing rows, rate limits
at full S&P 500 scale. Swap `src/ingestion/yfinance_client.py` for a
Polygon.io or Tiingo bulk-endpoint client once the engine/backtest logic
is validated and you need daily production reliability at ~500 tickers.

**Survivorship bias**: `src/ingestion/universe.py` pulls the *current*
S&P 500 list. Backtesting with today's constituents against years of
history excludes stocks that were removed (usually because they were
struggling), which inflates historical breadth readings. Documented in
the module; a static frozen `data/universe.csv` is provided for
reproducible runs, but it is not point-in-time-accurate historically.

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
  includes a regression test (truncate-and-recompute) to catch
  accidental leakage if you modify these.

## Infrastructure

- `.github/workflows/daily_ingest.yml` -- pulls prior day's OHLCV,
  commits updated SQLite DB. Runs ~4:30pm ET on weekdays.
- `.github/workflows/breadth_compute.yml` -- chained after ingestion,
  computes the day's metrics/regime, fires Twilio alerts on regime
  flips or new divergence flags (deduped via `alerts_sent` table).

Kept as two separate workflows so ingestion failures and compute failures
are easy to isolate -- same principle as the kayak dashboard's separation
of gauge-check from alert logic.

### Required GitHub Actions secrets (for Twilio alerts)

`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`,
`ALERT_TO_NUMBER`

### Scaling beyond SQLite

SQLite is fine for solo/local use and is what the Actions workflows commit
back to the repo. If you hit file-locking issues running the dashboard
and the Action concurrently, or want to scale past a ~30-500 ticker
universe with frequent writes, move to Postgres (Supabase free tier is
low-friction) -- the schema in `src/db/schema.sql` is Postgres-compatible
as written.

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

## Sector-level breadth (`src/engine/sector_breadth.py`)

Reuses the same metric functions from `engine/metrics.py`, scoped to a
sector's ticker subset (`data/sector_map.csv`) instead of the full
universe. Answers "is this rally broad-based or concentrated in one
sector" and surfaces rotation via `sector_relative_strength` (ranks
sectors by composite score per day). Sector universes are much smaller
than the full index, so sector-level regime reads are noisier than the
market-wide composite -- the module uses a wider 90-day z-score window
by default to compensate, and single-day sector regime flips should be
treated with more skepticism than market-wide ones.

## Next steps

- Run `optimize_weights` / `evaluate_on_holdout` against real multi-year
  history (not the synthetic data used to smoke-test the module) once
  yfinance ingestion has accumulated enough history.
- Swap yfinance for a bulk vendor once ready to run the full S&P 500
  universe daily.
- Extend `data/sector_map.csv` to the full universe (currently covers
  only the ~30-ticker starter list).
