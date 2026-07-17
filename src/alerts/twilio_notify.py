"""
Twilio SMS alerting -- same pattern as the kayak dashboard's gauge alerts.
Dedupes via alerts_sent so a regime that stays flipped for multiple days
doesn't re-text you every run.

Phase 2: alerts are now per-index (index_key included in every call and
in the dedupe check), since each index/sector can flip regime
independently. With 14 indexes (3 major + 11 sector) computed daily,
alerting on every single flip would be noisy -- ALERT_INDEX_KEYS lets you
restrict which indexes actually fire SMS, defaulting to just 'sp500' if
unset. Every index still gets its regime/divergence computed and stored
either way; this only gates the SMS, not the data.
"""
from __future__ import annotations

import os

from twilio.rest import Client

from src.db.models import alert_already_sent_today, log_alert

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER")
ALERT_TO_NUMBER = os.environ.get("ALERT_TO_NUMBER")

# Comma-separated index_keys to alert on, e.g. "sp500,nasdaq100". Defaults
# to sp500 only so adding 10+ sector indexes doesn't suddenly 10x your
# SMS volume without an explicit opt-in.
ALERT_INDEX_KEYS = set(
    k.strip() for k in os.environ.get("ALERT_INDEX_KEYS", "sp500").split(",") if k.strip()
)


def send_sms(body: str) -> None:
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, ALERT_TO_NUMBER]):
        raise RuntimeError("Twilio env vars not fully configured; see README setup section.")
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    client.messages.create(body=body, from_=TWILIO_FROM_NUMBER, to=ALERT_TO_NUMBER)


def maybe_alert_regime_flip(index_key: str, index_label: str, date: str, prior_regime: str, new_regime: str) -> None:
    if index_key not in ALERT_INDEX_KEYS:
        return
    if prior_regime == new_regime:
        return
    if alert_already_sent_today(index_key, date, "regime_flip"):
        return
    body = f"[Breadth] {index_label}: regime flip {prior_regime} -> {new_regime} as of {date}"
    send_sms(body)
    log_alert(index_key, date, "regime_flip", body)


def maybe_alert_divergence(index_key: str, index_label: str, date: str, kind: str) -> None:
    """kind: 'bearish_divergence' | 'bullish_divergence'"""
    if index_key not in ALERT_INDEX_KEYS:
        return
    if alert_already_sent_today(index_key, date, kind):
        return
    body = f"[Breadth] {index_label}: {kind.replace('_', ' ').title()} detected as of {date}"
    send_sms(body)
    log_alert(index_key, date, kind, body)
