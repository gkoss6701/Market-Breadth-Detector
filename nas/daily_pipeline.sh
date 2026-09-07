#!/usr/bin/env bash
# Daily pipeline: ingest the latest OHLCV, then recompute breadth metrics
# for every registered index. This replaces the old daily_ingest.yml /
# breadth_compute.yml GitHub Actions -- run it via QNAP Task Scheduler,
# weekdays after US market close:
#
#   docker compose run --rm pipeline bash nas/daily_pipeline.sh
#
# Fails fast (set -e) and loudly: a partial run (ingest succeeded, compute
# didn't) is worse than an obviously-failed one, so this doesn't try to
# limp forward on error.
set -euo pipefail

LOG_DIR="${LOG_DIR:-/app/logs}"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/daily_pipeline.log"

log() { echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$LOG_FILE"; }

on_error() {
    local exit_code=$?
    log "FAILED (exit $exit_code) -- see above for the actual Python traceback."
    # Best-effort Pushover notification so a silent failure doesn't go
    # unnoticed until you happen to check the dashboard. Never let a
    # failure HERE mask the original pipeline failure -- hence the
    # `|| true` and the try/except inside the python call.
    python -c "
from src.alerts.pushover_notify import send_pushover
try:
    send_pushover('daily_pipeline.sh failed -- check logs/daily_pipeline.log on the NAS', title='Breadth NAS')
except Exception as e:
    print(f'(alert send also failed: {e})')
" 2>&1 | tee -a "$LOG_FILE" || true
    exit "$exit_code"
}
trap on_error ERR

log "=== daily_pipeline start ==="

log "Step 1/2: daily_ingest"
python -m scripts.daily_ingest 2>&1 | tee -a "$LOG_FILE"

log "Step 2/2: breadth_compute"
python -m scripts.breadth_compute 2>&1 | tee -a "$LOG_FILE"

log "=== daily_pipeline complete ==="
