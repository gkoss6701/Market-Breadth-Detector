#!/usr/bin/env bash
# Entrypoint for the `scheduler` docker-compose service -- runs cron in
# the foreground so the container stays up (restart: unless-stopped keeps
# it alive across NAS reboots/crashes, same as the dashboard service),
# firing nas/daily_pipeline.sh and nas/weekly_refresh.sh on the schedule
# in nas/scheduler.crontab. Replaces QNAP Task Scheduler for NAS models
# that don't expose Control Panel -> System -> Task Scheduler -> Create ->
# User Defined Script (confirmed missing on at least one QNAP model this
# was deployed to) -- this runs the same way on every QNAP model, and on
# any other docker-compose host, since it doesn't touch QNAP OS config at
# all.
set -euo pipefail

LOG_DIR="${LOG_DIR:-/app/logs}"
mkdir -p "$LOG_DIR"

# cron's child processes get a minimal, mostly-empty environment by
# default -- they do NOT inherit the variables docker-compose passed to
# this container via env_file/.env (EODHD_API_KEY, PUSHOVER creds, etc.),
# even though this entrypoint process has them. Write out just the
# specific variables the pipeline scripts actually need, each properly
# shell-quoted with bash's `%q` -- deliberately NOT a blanket dump of the
# whole environment (`printenv > file` + `. file`): the container's full
# environment can contain values with spaces/quotes/other shell
# metacharacters that break naive sourcing (verified while testing this
# script), and an explicit allowlist is both safer and self-documenting
# about exactly what the scheduled jobs depend on. nas/scheduler.crontab
# invokes these jobs via `bash -c` specifically (not the cron default
# /bin/sh) so this %q-quoted syntax is guaranteed to parse correctly.
: > /etc/environment
for var in EODHD_API_KEY MARKET_BREADTH_PUSHOVER_API PUSHOVER_KEY ALERT_INDEX_KEYS BREADTH_DB_PATH; do
    val="${!var:-}"
    if [ -n "$val" ]; then
        printf 'export %s=%q\n' "$var" "$val" >> /etc/environment
    fi
done
chmod 644 /etc/environment

# Install the checked-in crontab. /etc/cron.d files need root ownership
# and must not be group/other-writable, or cron silently ignores them.
cp /app/nas/scheduler.crontab /etc/cron.d/market-breadth
chmod 644 /etc/cron.d/market-breadth
chown root:root /etc/cron.d/market-breadth

echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] scheduler starting -- installed crontab:"
cat /etc/cron.d/market-breadth

# -f: stay in the foreground (PID 1 of this container) instead of
# daemonizing and exiting, which is what "docker-compose up -d scheduler"
# needs to keep the container alive.
exec cron -f
