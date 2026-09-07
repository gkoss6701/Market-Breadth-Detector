#!/usr/bin/env bash
# One-time setup: populates the index registry, backfills 2 years of
# price history, then computes breadth for every index. Run this once
# after `docker compose build`, before turning on the scheduled tasks in
# QNAP Task Scheduler:
#
#   docker compose run --rm pipeline bash nas/first_time_setup.sh
#
# Safe to re-run (every step upserts rather than appends), but the
# backfill step alone can take several minutes (~500-600 tickers via
# yfinance, with rate-limit retries) -- don't expect it to be instant.
# Not wired to the failure-notification trap the scheduled scripts use since this
# one is meant to be run interactively and watched, not unattended.
set -euo pipefail

LOG_DIR="${LOG_DIR:-/app/logs}"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/first_time_setup.log"

log() { echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$LOG_FILE"; }

log "=== first_time_setup start ==="

log "Step 1/3: refresh_universe"
python -m scripts.refresh_universe 2>&1 | tee -a "$LOG_FILE"

log "Step 2/3: backfill_history --years 2"
python -m scripts.backfill_history --years 2 2>&1 | tee -a "$LOG_FILE"

log "Step 3/3: breadth_compute"
python -m scripts.breadth_compute 2>&1 | tee -a "$LOG_FILE"

log "=== first_time_setup complete -- the dashboard should now show data ==="
