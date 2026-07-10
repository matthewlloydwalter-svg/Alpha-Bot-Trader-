"""
market_hours.py — US stock market session logic (Eastern Time).

The regular US equities session is 09:30–16:00 ET, Monday–Friday. We use pytz
(already a dependency) so we never depend on the host's tz database, and we
always compute against America/New_York so DST (EST/EDT) is handled correctly.

Crypto (OKX) trades 24/7, so ``market_open_for_broker`` only gates Alpaca.

NOTE: US market holidays (e.g. Thanksgiving, July 4th) are NOT modeled here —
only weekends + daily hours. Add an NYSE holiday calendar if you need that.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytz

ET = pytz.timezone("America/New_York")
OPEN_H, OPEN_M = 9, 30
CLOSE_H, CLOSE_M = 16, 0


def _now_et(now_utc: datetime | None = None) -> datetime:
    now = now_utc or datetime.now(pytz.utc)
    if now.tzinfo is None:
        now = pytz.utc.localize(now)
    return now.astimezone(ET)


def is_market_open(now_utc: datetime | None = None) -> bool:
    """True if the US equities market is in its regular session right now."""
    et = _now_et(now_utc)
    if et.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        return False
    open_t = et.replace(hour=OPEN_H, minute=OPEN_M, second=0, microsecond=0)
    close_t = et.replace(hour=CLOSE_H, minute=CLOSE_M, second=0, microsecond=0)
    return open_t <= et < close_t


def next_market_open(now_utc: datetime | None = None) -> datetime:
    """Return the next 09:30 ET session open, as a UTC-aware datetime."""
    et = _now_et(now_utc)
    candidate = et.replace(hour=OPEN_H, minute=OPEN_M, second=0, microsecond=0)
    if et >= candidate:              # today's open already passed → look ahead
        candidate = candidate + timedelta(days=1)
    while candidate.weekday() >= 5:  # skip Sat/Sun
        candidate = candidate + timedelta(days=1)
    # Re-localize so a DST boundary crossed by the timedelta stays correct.
    candidate = ET.localize(candidate.replace(tzinfo=None))
    return candidate.astimezone(pytz.utc)


def market_open_for_broker(broker: str | None, now_utc: datetime | None = None) -> bool:
    """Crypto (OKX) is always open; equities (Alpaca/default) follow ET hours."""
    if (broker or "alpaca").lower() == "okx":
        return True
    return is_market_open(now_utc)


def market_status(now_utc: datetime | None = None) -> dict:
    """Serializable market-status payload for the frontend.

    ``next_open_epoch`` is UTC seconds — the browser renders it in the user's
    local timezone automatically (ET 09:30 shows as 06:30 PT, 22:30 CST, etc.).
    """
    now = now_utc or datetime.now(pytz.utc)
    et = _now_et(now)
    nxt = next_market_open(now)
    return {
        "open": is_market_open(now),
        "now_utc": now.astimezone(pytz.utc).isoformat(),
        "now_et": et.isoformat(),
        "next_open_utc": nxt.isoformat(),
        "next_open_epoch": int(nxt.timestamp()),
        "session": {"open_et": "09:30", "close_et": "16:00", "tz": "America/New_York"},
    }
