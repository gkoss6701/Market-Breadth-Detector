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
> compatibility with the standalone `docker-compose` binary below. If
> your Container Station happens to have the newer `docker compose`
> plugin working, you'll see a harmless `the attribute 'version' is
> obsolete` warning -- ignore it, everything still works either way.

Every command below uses `docker-compose` (hyphenated) rather than
`docker compose` (a space, the newer CLI-plugin syntax), because on a
lot of QNAP models Container Station's `docker` CLI has no Compose
plugin at all -- it fails with `docker: 'compose' is not a docker
command`, and Container Station's own per-user Docker config directory
(where a plugin would need to live) is locked down tightly enough that
even an administrators-group SSH user can't read it, so it's not
practically fixable by dropping a plugin binary in place. The standalone
`docker-compose` binary sidesteps that whole mechanism -- installed
below in Prerequisites.

## 1. Prerequisites

- **Container Station** installed (App Center, if not already).
- **SSH access** enabled: Control Panel -> Network & File Services ->
  Telnet/SSH -> enable SSH.
- **The `docker-compose` binary**. Check first with `docker-compose
  version` -- if that already prints a version, skip this. Otherwise:
  ```bash
  uname -m   # confirm your NAS's CPU architecture first
  sudo curl -SL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)" \
    -o /usr/local/bin/docker-compose
  sudo chmod +x /usr/local/bin/docker-compose
  docker-compose version
  ```
  `$(uname -m)` resolves correctly for the two common QNAP
  architectures (`x86_64` for Intel/AMD models, `aarch64` for ARM ones).
  If your NAS reports something else (e.g. 32-bit `armv7l`), the
  release's filename doesn't match 1:1 -- check
  https://github.com/docker/compose/releases for the right asset name.
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
docker-compose build   # constituents/ needs no mkdir -- it's already there from git
docker-compose up -d dashboard
docker-compose ps        # should show market-breadth-dashboard as "healthy" after ~15-20s
```

Visit `http://<nas-ip>:8501` in a browser. It'll say "No indexes
registered yet" until step 5 runs.

`dashboard` has `restart: unless-stopped`, so it comes back up on its own
after a NAS reboot or container crash -- no extra QNAP configuration
needed for that part.

## 5. First-time data population (run once)

```bash
docker-compose run --rm pipeline bash nas/first_time_setup.sh
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

## 6. Schedule the recurring jobs

```bash
docker-compose up -d scheduler
docker-compose ps        # should show market-breadth-scheduler as "Up"
```

That's it -- no Control Panel configuration needed. The `scheduler`
service runs an in-container cron daemon (see
`nas/scheduler_entrypoint.sh` / `nas/scheduler.crontab`) that fires the
same two jobs automatically:

- **Daily Ingest + Compute** -- weekdays, 4:30pm ET (30 min after US
  market close).
- **Weekly Universe Refresh** -- Saturdays, 6am ET.

The schedule is pinned to `America/New_York` regardless of your NAS's own
system timezone, so it stays correct across DST changes on its own --
this replaces the older approach (QNAP's Control Panel -> System -> Task
Scheduler -> Create -> User Defined Script), which isn't available on
every QNAP model, ran in NAS local time, and needed a manual re-check
every DST change. `scheduler` has `restart: unless-stopped`, same as
`dashboard`, so it comes back on its own after a NAS reboot or container
crash.

If you're on a QNAP model that *does* have Task Scheduler and would
rather use it instead, that still works exactly as before: point a **User
Defined Script** task at `docker-compose run --rm pipeline bash
nas/daily_pipeline.sh` (weekdays, after market close, converting to NAS
local time) and `nas/weekly_refresh.sh` (Saturdays) -- just don't run
both that AND the `scheduler` service, or the jobs will fire twice.

To change the schedule or timezone, edit `nas/scheduler.crontab`, then
`docker-compose build && docker-compose up -d scheduler` to pick it up.

Both jobs already fail loudly (non-zero exit) and send a best-effort
Pushover notification on failure, using the same `.env` credentials --
so a broken scheduled run won't sit silently unnoticed the way a missed
check of the Actions tab might have.

## 7. Day-to-day operations

- **Dashboard logs**: `docker-compose logs -f dashboard`
- **Scheduler logs**: `docker-compose logs -f scheduler` (shows the
  installed crontab at startup and cron's own activity); actual job
  output goes to `./logs/cron.log` plus the same
  `./logs/daily_pipeline.log` / `./logs/weekly_refresh.log` files a
  manual run produces.
- **Manually trigger a run** (e.g. to test a fix without waiting for the
  schedule): `docker-compose run --rm pipeline bash nas/daily_pipeline.sh`
  (or `nas/weekly_refresh.sh`), run directly over SSH -- independent of
  whatever the `scheduler` service is doing.
- **Deploying a code update**: `git pull` (or re-copy the changed files),
  then `docker-compose build && docker-compose up -d dashboard scheduler`
  -- one build updates the image all three services share, so the next
  scheduled pipeline run automatically picks it up too.
