"""
Pushover alerting -- replaces the earlier Twilio SMS integration. Same
dedupe pattern (via alerts_sent, so a regime that stays flipped for
multiple days doesn't re-notify you every run) and same per-index gating,
just delivered as a push notification instead of a text message. Pushover
is a plain HTTPS POST API (https://pushover.net/api), so this needs no
SDK -- just `requests`, already a dependency.

Phase 2: alerts are per-index (index_key included in every call and in
the dedupe check), since each index/sector can flip regime
independently. With 19 indexes (8 major + 11 sector) computed daily,
alerting on every single flip would be noisy -- ALERT_INDEX_KEYS lets you
restrict which indexes actually fire a notification, defaulting to just
'sp500' if unset. Every index still gets its regime/divergence computed
and stored either way; this only gates the notification, not the data.

Two credentials, not one: Pushover's API needs an application token
(identifies THIS app to Pushover) AND a user/group key (identifies WHO
receives the notification -- i.e. your phone). MARKET_BREADTH_PUSHOVER_API
holds the former; PUSHOVER_KEY holds the latter, using
the same naming convention. Get both from https://pushover.net: create
an application (dashboard -> "Create an Application/API Token") for the
API token, and your user key is shown right on the dashboard's front
page.
"""
from __future__ import annotations

import os

import requests

from src.db.models import alert_already_sent_today, log_alert

PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"

MARKET_BREADTH_PUSHOVER_API = os.environ.get("MARKET_BREADTH_PUSHOVER_API")
PUSHOVER_KEY = os.environ.get("PUSHOVER_KEY")

# Comma-separated index_keys to alert on, e.g. "sp500,nasdaq100". Defaults
# to sp500 only so adding 10+ sector indexes doesn't suddenly 10x your
# notification volume without an explicit opt-in.
ALERT_INDEX_KEYS = set(
    k.strip() for k in os.environ.get("ALERT_INDEX_KEYS", "sp500").split(",") if k.strip()
)


def send_pushover(body: str, title: str = "Breadth") -> None:
    if not all([MARKET_BREADTH_PUSHOVER_API, PUSHOVER_KEY]):
        raise RuntimeError(
            "Pushover env vars not fully configured (need both "
            "MARKET_BREADTH_PUSHOVER_API and PUSHOVER_KEY); "
            "see README setup section."
        )
    response = requests.post(
        PUSHOVER_API_URL,
        data={
            "token": MARKET_BREADTH_PUSHOVER_API,
            "user": PUSHOVER_KEY,
            "message": body,
            "title": title,
        },
        timeout=10,
    )
    response.raise_for_status()


def maybe_alert_regime_flip(index_key: str, index_label: str, date: str, prior_regime: str, new_regime: str) -> None:
    if index_key not in ALERT_INDEX_KEYS:
        return
    if prior_regime == new_regime:
        return
    if alert_already_sent_today(index_key, date, "regime_flip"):
        return
    body = f"{index_label}: regime flip {prior_regime} -> {new_regime} as of {date}"
    send_pushover(body)
    log_alert(index_key, date, "regime_flip", body)


def maybe_alert_divergence(index_key: str, index_label: str, date: str, kind: str) -> None:
    """kind: 'bearish_divergence' | 'bullish_divergence'"""
    if index_key not in ALERT_INDEX_KEYS:
        return
    if alert_already_sent_today(index_key, date, kind):
        return
    body = f"{index_label}: {kind.replace('_', ' ').title()} detected as of {date}"
    send_pushover(body)
    log_alert(index_key, date, kind, body)
