# Running on a QNAP NAS

This replaces the old GitHub Actions setup entirely. Nothing runs on
GitHub anymore -- the repo is just code backup now. All scheduling,
computation, and the dashboard run locally on your QNAP via Docker
(Container Station).

## What changed from the GitHub Actions version

- `daily_ingest.yml` / `breadth_compute.yml` / `refresh_universe.yml` /
  `backfill_history.yml` are gone. Their logic is unchanged (same
  `scripts/*.py` entry points) but they're now invoked by QNAP Task
  Scheduler instead of GitHub's cron.
- `data/breadth.db` is no longer committed to git. It lives on the NAS
  disk (mounted into the container via `docker-compose.yml`), which is
  the actual persistent storage now -- committing it to git every run
  was a workaround for GitHub Actions runners being ephemeral, and
  that's no longer the situation. **Make sure `data/` is covered by
  whatever NAS backup/snapshot routine you already run** -- it's the
  only copy of your breadth history now, where before every day's DB was
  also sitting in git history as a fallback.
- `constituents/*.csv` (the S&P 500/400/600, Nasdaq-100, Dow 30, and
  Russell 1000/2000/3000 base/fallback lists) IS committed to git,
  unlike `data/`. It ships
  with the repo so a fresh clone/extract already has current index
  membership. This directory is also mounted as a volume (see the
  comment above `version:` in `docker-compose.yml`) so that when the
  weekly `refresh_universe` run pulls a fresh list, that update
  persists on the NAS disk instead of being discarded when the
  `pipeline` container exits -- and so that if a live pull ever fails,
  the fallback read is against your NAS's own current copy of the file,
  not a stale one baked into an old image.
- Alerting moved from Twilio SMS to Pushover push notifications, and
  credentials live in a local `.env` file on the NAS (see below) rather
  than GitHub repository secrets, same as everything else that used to
  be a GitHub Actions secret.
- OHLCV ingestion moved from yfinance to EODHD (`src/ingestion/eodhd_client.py`)
  -- unlike yfinance, this needs an API key. `EODHD_API_KEY` goes in the
  same `.env` file.
- The old setup chained `daily_ingest` -> `breadth_compute` via GitHub's
  `workflow_run` trigger, mainly so failures were visible as two separate
  named steps in the Actions UI. QNAP Task Scheduler doesn't have an
  equivalent chaining mechanism, so `nas/daily_pipeline.sh` runs both
  steps in sequence inside one script instead -- simpler, and avoids any
  timing gap between two independently-scheduled tasks.

> `docker-compose.yml` keeps the old `version: "3.8"` key for
> compatibility with older Container Station versions that bundle Docker
> Compose v1. If your Container Station uses the newer v2 plugin, you'll
> see a harmless `the attribute 'version' is obsolete` warning -- ignore
> it, everything still works.

## 1. Prerequisites

- **Container Station** installed (App Center, if not already).
- **SSH access** enabled: Control Panel -> Network & File Services ->
  Telnet/SSH -> enable SSH.
- A shared folder to hold the project, e.g. create `Container` (if you
  don't already use one for other Docker projects) via Control Panel ->
  Shared Folders.

## 2. Get the code onto the NAS

SSH into the NAS (`ssh admin@<nas-ip>`), then either:

- **Git is available or installable** (QNAP's `Entware`/`opkg`, or a
  Git Server app from App Center): `git clone` the repo directly into
  the shared folder.
- **Simplest path**: extract the project zip locally and drag it into
  the shared folder over the network via File Station or SMB, so you end
  up with something like `/share/Container/market-breadth-detector/`
  containing `Dockerfile`, `docker-compose.yml`, `src/`, etc.

Either way, `cd` into that directory for every command below. Confirm
`constituents/*.csv` came along with the rest of the code (it's a
normal git-tracked directory, same as `src/`) -- a `git clone` or full
zip extract carries it automatically, but a partial manual copy might
miss it.

## 3. Configure secrets

```bash
cp .env.example .env
vi .env   # fill in EODHD_API_KEY, plus MARKET_BREADTH_PUSHOVER_API / PUSHOVER_KEY
```

`EODHD_API_KEY` is the same key/account you already use elsewhere --
this is just handing it to the standalone NAS script directly, since it
has no way to see the EODHD connection configured in Claude itself.

`.env` is gitignored -- it stays on the NAS only, never gets committed.

## 4. Build the image and start the dashboard

```bash
mkdir -p data logs
docker compose build   # constituents/ needs no mkdir -- it's already there from git
docker compose up -d dashboard
docker compose ps        # should show market-breadth-dashboard as "healthy" after ~15-20s
```

Visit `http://<nas-ip>:8501` in a browser. It'll say "No indexes
registered yet" until step 5 runs.

`dashboard` has `restart: unless-stopped`, so it comes back up on its own
after a NAS reboot or container crash -- no extra QNAP configuration
needed for that part.

## 5. First-time data population (run once)

```bash
docker compose run --rm pipeline bash nas/first_time_setup.sh
```

This runs `refresh_universe` -> `backfill_history --years 2` ->
`breadth_compute` in sequence, same order the README's Quickstart
describes. The backfill step pulls the deduped union across all 8 major
indexes -- now roughly 3,000 tickers (dominated by Russell 3000's own
~2,961, since S&P 400/600 add only a couple dozen names not already in
Russell 3000), up from the ~1,500-1,600 back when Russell wasn't
registered yet -- via EODHD (one
API call per ticker, run concurrently) and can take a few minutes. Watch
it complete, then refresh the dashboard -- it should now show real data
for every index.

## 6. Schedule the recurring jobs (QNAP Task Scheduler)

Control Panel -> System -> Task Scheduler -> Create -> **User Defined
Script**. Two entries:

**Daily Ingest + Compute** -- weekdays, after US market close in your
NAS's local timezone (the old GitHub Actions cron was `30 21 * * 1-5`
UTC, i.e. ~4:30pm ET; convert that to whatever your NAS's system
timezone is set to, and remember to re-check it around DST changes since
Task Scheduler follows NAS local time, not a fixed UTC offset the way
GitHub's cron did):

```bash
cd /share/Container/market-breadth-detector && docker compose run --rm pipeline bash nas/daily_pipeline.sh
```

**Weekly Universe Refresh** -- Saturdays, any time (markets closed, no
urgency):

```bash
cd /share/Container/market-breadth-detector && docker compose run --rm pipeline bash nas/weekly_refresh.sh
```

Before saving either task, SSH in and run `which docker` -- Task
Scheduler's script environment doesn't always inherit the same `PATH` as
an interactive SSH session, so if `docker` isn't on its default PATH
you'll need the full path (often `/usr/local/bin/docker`) in the script
line above.

Both scripts already fail loudly (non-zero exit) and send a best-effort
Pushover notification on failure, using the same `.env` credentials --
so a broken scheduled run won't sit silently unnoticed the way a missed
check of the
Actions tab might have.

## 7. Day-to-day operations

- **Dashboard logs**: `docker compose logs -f dashboard`
- **Pipeline run logs**: `./logs/daily_pipeline.log`,
  `./logs/weekly_refresh.log` on the NAS disk (also captured by QNAP
  Task Scheduler's own per-run output).
- **Manually trigger a run** (e.g. to test a fix without waiting for the
  schedule): same `docker compose run --rm pipeline ...` commands as
  above, run directly over SSH.
- **Deploying a code update**: `git pull` (or re-copy the changed files),
  then `docker compose build && docker compose up -d dashboard` -- one
  build updates the image both services share, so the next scheduled
  pipeline run automatically picks it up too.
