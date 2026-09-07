#!/usr/bin/env bash
# Weekly universe refresh: repopulates index_constituents / index_metadata
# for every major + sector index. This replaces the old
# refresh_universe.yml GitHub Action -- run it via QNAP Task Scheduler,
# Saturdays (constituent lists change infrequently, no need for daily):
#
#   docker compose run --rm pipeline bash nas/weekly_refresh.sh
set -euo pipefail

LOG_DIR="${LOG_DIR:-/app/logs}"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/weekly_refresh.log"

log() { echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$LOG_FILE"; }

on_error() {
    local exit_code=$?
    log "FAILED (exit $exit_code) -- see above for the actual Python traceback."
    python -c "
from src.alerts.pushover_notify import send_pushover
try:
    send_pushover('weekly_refresh.sh failed -- check logs/weekly_refresh.log on the NAS', title='Breadth NAS')
except Exception as e:
    print(f'(alert send also failed: {e})')
" 2>&1 | tee -a "$LOG_FILE" || true
    exit "$exit_code"
}
trap on_error ERR

log "=== weekly_refresh start ==="
python -m scripts.refresh_universe 2>&1 | tee -a "$LOG_FILE"
log "=== weekly_refresh complete ==="
