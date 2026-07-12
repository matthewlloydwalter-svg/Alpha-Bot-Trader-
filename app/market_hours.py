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


def session_date_et(now_utc: datetime | None = None) -> str:
    """Return the current US equities session date as YYYY-MM-DD in ET."""
    return _now_et(now_utc).strftime("%Y-%m-%d")


def minutes_since_open(now_utc: datetime | None = None) -> float | None:
    """Minutes elapsed since today's 09:30 ET open, or None if the market is closed."""
    et = _now_et(now_utc)
    if et.weekday() >= 5:
        return None
    open_t = et.replace(hour=OPEN_H, minute=OPEN_M, second=0, microsecond=0)
    close_t = et.replace(hour=CLOSE_H, minute=CLOSE_M, second=0, microsecond=0)
    if et < open_t or et >= close_t:
        return None
    return (et - open_t).total_seconds() / 60.0


def minutes_until_close(now_utc: datetime | None = None) -> float | None:
    """Minutes remaining until today's 16:00 ET close, or None if the market is closed."""
    et = _now_et(now_utc)
    if et.weekday() >= 5:
        return None
    open_t = et.replace(hour=OPEN_H, minute=OPEN_M, second=0, microsecond=0)
    close_t = et.replace(hour=CLOSE_H, minute=CLOSE_M, second=0, microsecond=0)
    if et < open_t or et >= close_t:
        return None
    return (close_t - et).total_seconds() / 60.0


def is_open_entry_window(window_minutes: int = 45, now_utc: datetime | None = None) -> bool:
    """True during the first ``window_minutes`` after the regular session open."""
    mins = minutes_since_open(now_utc)
    return mins is not None and 0 <= mins <= window_minutes


def is_eod_exit_window(window_minutes: int = 20, now_utc: datetime | None = None) -> bool:
    """True during the final ``window_minutes`` before the regular session close."""
    mins = minutes_until_close(now_utc)
    return mins is not None and 0 <= mins <= window_minutes


def _session_open_close_et(day_et) -> tuple:
    """09:30 and 16:00 ET on the calendar day of ``day_et`` (ET-aware)."""
    open_et = day_et.replace(hour=OPEN_H, minute=OPEN_M, second=0, microsecond=0)
    close_et = day_et.replace(hour=CLOSE_H, minute=CLOSE_M, second=0, microsecond=0)
    return open_et, close_et


def _previous_weekday_et(day_et, *, skip: int = 1):
    """Walk backward ``skip`` weekdays from ``day_et`` (ET-aware)."""
    d = day_et
    steps = 0
    while steps < skip:
        d = d - timedelta(days=1)
        if d.weekday() < 5:
            steps += 1
    return d


def trading_session_bounds(now_utc: datetime | None = None) -> tuple[datetime, datetime]:
    """
    UTC (start, end) for the Alpaca-style 1D intraday chart window.

    - During the regular session: 09:30 ET today → now.
    - After today's close: 09:30–16:00 ET today.
    - Before today's open or on weekends: the previous completed session
      (09:30–16:00 ET on the last weekday).
    """
    now = now_utc or datetime.now(pytz.utc)
    if now.tzinfo is None:
        now = pytz.utc.localize(now)
    else:
        now = now.astimezone(pytz.utc)
    et = now.astimezone(ET)
    open_today, close_today = _session_open_close_et(et)

    if et.weekday() < 5:
        if open_today <= et < close_today:
            return open_today.astimezone(pytz.utc), now
        if et >= close_today:
            return open_today.astimezone(pytz.utc), close_today.astimezone(pytz.utc)
        prev = _previous_weekday_et(et)
        open_prev, close_prev = _session_open_close_et(prev)
        return open_prev.astimezone(pytz.utc), close_prev.astimezone(pytz.utc)

    prev = _previous_weekday_et(et)
    open_prev, close_prev = _session_open_close_et(prev)
    return open_prev.astimezone(pytz.utc), close_prev.astimezone(pytz.utc)


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
