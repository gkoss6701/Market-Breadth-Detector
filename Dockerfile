# Market Breadth Detector -- single image used for the always-on
# Streamlit dashboard, the on-demand pipeline jobs (daily ingest, breadth
# compute, weekly universe refresh), AND the `scheduler` service that
# fires those pipeline jobs on a schedule (see nas/scheduler_entrypoint.sh
# / nas/scheduler.crontab). Which one runs is decided by the `command:`
# in docker-compose.yml, not by anything baked in here.
#
# python:3.11-slim-bookworm (Debian 12) publishes both amd64 and arm64
# variants, so this builds on either an x86 or ARM-based NAS without
# changes. build-essential / libxml2-dev / libxslt-dev are included so
# `pip install` doesn't fail if a prebuilt wheel isn't available for your
# NAS's specific architecture (mainly a concern for lxml/pandas on less
# common platforms) -- costs a slightly larger image, buys a build that
# doesn't mysteriously fail on one NAS model and not another. cron/tzdata
# support the `scheduler` service -- some QNAP models don't expose
# Control Panel -> System -> Task Scheduler -> Create -> User Defined
# Script at all, so scheduling now lives inside the container instead of
# depending on QNAP OS features; tzdata specifically is needed so cron's
# TZ=America/New_York directive (see nas/scheduler.crontab) actually
# resolves to real DST rules rather than being silently ignored.
#
# Pinned to -bookworm (Debian 12) rather than plain `python:3.11-slim`
# (which now resolves to Debian 13 "trixie") specifically because of
# cron: trixie's `cron` package pulls in `cron-daemon-common`, which
# depends on `systemd | systemd-standalone-sysusers | systemd-sysusers`
# -- installing that inside a minimal container build (no init system
# running) fails outright ("Failed to copy permissions from /etc/group",
# confirmed live on this deployment). bookworm's cron package predates
# that systemd-sysusers dependency chain and installs cleanly. Don't
# casually bump this to plain `python:3.11-slim` again without checking
# whether that regression has been fixed upstream.
FROM python:3.11-slim-bookworm

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libxml2-dev \
        libxslt-dev \
        cron \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# BREADTH_DB_PATH is overridden by docker-compose.yml to point inside the
# mounted data volume -- this default only matters if the image is run
# directly without compose.
ENV BREADTH_DB_PATH=/app/data/breadth.db

EXPOSE 8501

# No CMD here on purpose -- docker-compose.yml sets an explicit command
# per service (dashboard vs. pipeline) so this image has one clear job
# per container instead of a default that's right for one service and
# wrong for the other.
