"""
Twilio SMS alerting -- same pattern as the kayak dashboard's gauge alerts.
Dedupes via alerts_sent so a regime that stays flipped for multiple days
doesn't re-text you every run.
"""
from __future__ import annotations

import os

from twilio.rest import Client

from src.db.models import alert_already_sent_today, log_alert

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER")
ALERT_TO_NUMBER = os.environ.get("ALERT_TO_NUMBER")


def send_sms(body: str) -> None:
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, ALERT_TO_NUMBER]):
        raise RuntimeError("Twilio env vars not fully configured; see README setup section.")
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    client.messages.create(body=body, from_=TWILIO_FROM_NUMBER, to=ALERT_TO_NUMBER)


def maybe_alert_regime_flip(date: str, prior_regime: str, new_regime: str) -> None:
    if prior_regime == new_regime:
        return
    if alert_already_sent_today(date, "regime_flip"):
        return
    body = f"[Breadth] Regime flip {prior_regime} -> {new_regime} as of {date}"
    send_sms(body)
    log_alert(date, "regime_flip", body)


def maybe_alert_divergence(date: str, kind: str) -> None:
    """kind: 'bearish_divergence' | 'bullish_divergence'"""
    if alert_already_sent_today(date, kind):
        return
    body = f"[Breadth] {kind.replace('_', ' ').title()} detected as of {date}"
    send_sms(body)
    log_alert(date, kind, body)
