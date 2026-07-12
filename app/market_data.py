"""
market_data.py — the bridge between raw exchange candles and the shared
pattern brain. It is the single source of truth that BOTH the dashboard API
and the autonomous bot engine call, guaranteeing the UI and the bots look at
the exact same analyzed market structure.

It also provides a tiny in-process TTL cache so a dashboard refresh and a bot
cycle hitting the same symbol within a few seconds don't double-charge the
exchange rate limits.
"""

from __future__ import annotations

import time
import logging
import threading
from datetime import datetime, timedelta, timezone

from app.brokers import get_candles, BrokerError
from app.pattern_analysis import Candle, analyze_candles, Analysis

logger = logging.getLogger("alphabot.marketdata")

_CACHE: dict[tuple, tuple[float, Analysis]] = {}
_CACHE_TTL_SECONDS = 20
_CACHE_LOCK = threading.Lock()

# OKX uses upper-case timeframes for some intervals; we normalize on input.
_TF_NORMALIZE = {
    "1H": "1h", "4H": "4h", "1D": "1d", "1W": "1w",
    # Alpaca-style aliases used by chart presets
    "1Min": "1m", "1MIN": "1m", "1min": "1m",
    "5Min": "5m", "5MIN": "5m", "5min": "5m",
    "15Min": "15m", "15MIN": "15m", "15min": "15m",
    "30Min": "30m", "30MIN": "30m", "30min": "30m",
    "4Hour": "4h", "4HOUR": "4h", "4hour": "4h",
    "1Hour": "1h", "1HOUR": "1h", "1hour": "1h",
}

# Chart UI presets → Alpaca-compatible bar size + lookback window.
# Alpaca's TradingView chart uses:
#   1D — intraday 5-minute bars for the current/last regular session (09:30–16:00 ET)
#   1M — daily bars for the last ~30 calendar days
#   3M — daily bars for the last ~90 calendar days
# OKX (24/7) keeps rolling UTC windows with coarser intraday bars for 1D.
CHART_PRESETS = {
    "1D": {
        "alpaca": {
            "alpaca_timeframe": "5Min",
            "timeframe": "5m",
            "session_window": True,
            "limit": 500,
        },
        "okx": {
            "timeframe": "15m",
            "lookback_days": 1,
            "limit": 500,
        },
    },
    "1M": {
        "alpaca": {
            "alpaca_timeframe": "1Day",
            "timeframe": "1d",
            "lookback_days": 30,
            "limit": 45,
        },
        "okx": {
            "timeframe": "4h",
            "lookback_days": 30,
            "limit": 500,
        },
    },
    "3M": {
        "alpaca": {
            "alpaca_timeframe": "1Day",
            "timeframe": "1d",
            "lookback_days": 90,
            "limit": 100,
        },
        "okx": {
            "timeframe": "1d",
            "lookback_days": 90,
            "limit": 100,
        },
    },
}


def resolve_chart_preset(preset: str, *, broker: str = "alpaca",
                         now: datetime | None = None) -> dict:
    """
    Translate a UI preset code (1D / 1M / 3M) into broker-compatible fetch args.

    Returns dict with:
      preset, timeframe (internal), alpaca_timeframe, start (UTC datetime),
      end (UTC datetime or None = now), lookback_days, limit, session_window
    """
    code = (preset or "").strip().upper()
    if code not in CHART_PRESETS:
        raise ValueError(f"Unknown chart preset '{preset}'. Expected one of: {', '.join(CHART_PRESETS)}.")
    broker_key = (broker or "alpaca").lower()
    spec = CHART_PRESETS[code].get(broker_key) or CHART_PRESETS[code]["alpaca"]
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)

    session_window = bool(spec.get("session_window"))
    end = None
    if session_window and broker_key == "alpaca":
        from app.market_hours import trading_session_bounds
        start, end = trading_session_bounds(now_utc)
        lookback_days = 1
    else:
        lookback_days = int(spec.get("lookback_days", 1))
        start = now_utc - timedelta(days=lookback_days)

    return {
        "preset": code,
        "timeframe": spec["timeframe"],
        "alpaca_timeframe": spec.get("alpaca_timeframe", spec["timeframe"]),
        "start": start,
        "end": end,
        "lookback_days": lookback_days,
        "limit": int(spec["limit"]),
        "session_window": session_window,
    }


def _normalize_timeframe(broker: str, timeframe: str) -> str:
    tf = _TF_NORMALIZE.get(timeframe, timeframe)
    return tf


def get_market_analysis(broker: str, symbol: str, timeframe: str = "1h",
                        limit: int = 200, *, start: datetime | None = None,
                        end: datetime | None = None,
                        preset: str | None = None,
                        alpaca_key=None, alpaca_secret=None,
                        okx_key=None, okx_secret=None, okx_passphrase=None,
                        paper: bool = True, use_cache: bool = True) -> Analysis:
    """
    Fetch candles for (broker, symbol, timeframe) and return a fully analyzed
    ``Analysis`` object. Raises ``BrokerError`` on data/connectivity failure so
    callers can decide how to degrade.

    Optional ``start`` (UTC) pins the historical window — used by chart presets
    so each refresh recalculates from current UTC time rather than a stale range.
    """
    broker = (broker or "alpaca").lower()
    timeframe = _normalize_timeframe(broker, timeframe)
    # Bucket start to the minute so a 15s refresh can still hit cache briefly,
    # while a later refresh with a newer wall-clock start fetches fresh bars.
    start_bucket = None
    end_bucket = None
    if start is not None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        start_bucket = int(start.timestamp()) // 60
    if end is not None:
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        end_bucket = int(end.timestamp()) // 60
    cache_key = (broker, symbol.upper(), timeframe, limit, preset or "", start_bucket, end_bucket)

    if use_cache:
        with _CACHE_LOCK:
            hit = _CACHE.get(cache_key)
            if hit and (time.time() - hit[0]) < _CACHE_TTL_SECONDS:
                logger.info("[CACHE HIT] %s %s %s preset=%s", broker, symbol, timeframe, preset)
                return hit[1]

    logger.info("[FETCH] %s %s tf=%s limit=%d preset=%s start=%s end=%s",
                broker, symbol, timeframe, limit, preset,
                start.isoformat() if start else None,
                end.isoformat() if end else None)
    raw = get_candles(
        broker=broker, symbol=symbol, timeframe=timeframe, limit=limit,
        start=start, end=end,
        alpaca_key=alpaca_key, alpaca_secret=alpaca_secret,
        okx_key=okx_key, okx_secret=okx_secret, okx_passphrase=okx_passphrase,
        paper=paper,
    )
    if not raw:
        raise BrokerError(f"No candle data returned for {broker}:{symbol}.")

    candles = [
        Candle(ts=r["ts"], open=r["open"], high=r["high"],
               low=r["low"], close=r["close"], volume=r.get("volume", 0.0))
        for r in raw
    ]
    analysis = analyze_candles(symbol=symbol.upper(), exchange=broker,
                               timeframe=timeframe, candles=candles)

    if use_cache:
        with _CACHE_LOCK:
            _CACHE[cache_key] = (time.time(), analysis)

    logger.info("[ANALYSIS READY] %s %s -> signal=%s strength=%.2f",
                broker, symbol, analysis.signal.action, analysis.signal.strength)
    return analysis
