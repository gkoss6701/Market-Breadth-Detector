# Market Breadth Detector -- single image used for both the always-on
# Streamlit dashboard and the on-demand pipeline jobs (daily ingest,
# breadth compute, weekly universe refresh). Which one runs is decided by
# the `command:` in docker-compose.yml, not by anything baked in here.
#
# python:3.11-slim publishes both amd64 and arm64 variants, so this builds
# on either an x86 or ARM-based NAS without changes. build-essential /
# libxml2-dev / libxslt-dev are included so `pip install` doesn't fail if
# a prebuilt wheel isn't available for your NAS's specific architecture
# (mainly a concern for lxml/pandas on less common platforms) -- costs a
# slightly larger image, buys a build that doesn't mysteriously fail on
# one NAS model and not another.
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libxml2-dev \
        libxslt-dev \
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
