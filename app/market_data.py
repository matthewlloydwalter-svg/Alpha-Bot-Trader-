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
    "30Min": "30m", "30MIN": "30m", "30min": "30m",
    "1Hour": "1h", "1HOUR": "1h", "1hour": "1h",
}

# Chart UI presets → Alpaca-compatible bar size + lookback window.
# start_date is always computed fresh from UTC "now" so refreshes never go stale.
CHART_PRESETS = {
    "1D": {"alpaca_timeframe": "1Min", "timeframe": "1m", "lookback_days": 1, "limit": 1500},
    "1M": {"alpaca_timeframe": "30Min", "timeframe": "30m", "lookback_days": 30, "limit": 2000},
    "3M": {"alpaca_timeframe": "1Hour", "timeframe": "1h", "lookback_days": 90, "limit": 2500},
}


def resolve_chart_preset(preset: str, *, now: datetime | None = None) -> dict:
    """
    Translate a UI preset code (1D / 1M / 3M) into Alpaca-compatible fetch args.

    Returns dict with:
      preset, timeframe (internal), alpaca_timeframe, start (UTC datetime),
      lookback_days, limit
    """
    code = (preset or "").strip().upper()
    if code not in CHART_PRESETS:
        raise ValueError(f"Unknown chart preset '{preset}'. Expected one of: {', '.join(CHART_PRESETS)}.")
    spec = CHART_PRESETS[code]
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)
    start = now_utc - timedelta(days=int(spec["lookback_days"]))
    return {
        "preset": code,
        "timeframe": spec["timeframe"],
        "alpaca_timeframe": spec["alpaca_timeframe"],
        "start": start,
        "lookback_days": int(spec["lookback_days"]),
        "limit": int(spec["limit"]),
    }


def _normalize_timeframe(broker: str, timeframe: str) -> str:
    tf = _TF_NORMALIZE.get(timeframe, timeframe)
    return tf


def get_market_analysis(broker: str, symbol: str, timeframe: str = "1h",
                        limit: int = 200, *, start: datetime | None = None,
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
    if start is not None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        start_bucket = int(start.timestamp()) // 60
    cache_key = (broker, symbol.upper(), timeframe, limit, preset or "", start_bucket)

    if use_cache:
        with _CACHE_LOCK:
            hit = _CACHE.get(cache_key)
            if hit and (time.time() - hit[0]) < _CACHE_TTL_SECONDS:
                logger.info("[CACHE HIT] %s %s %s preset=%s", broker, symbol, timeframe, preset)
                return hit[1]

    logger.info("[FETCH] %s %s tf=%s limit=%d preset=%s start=%s",
                broker, symbol, timeframe, limit, preset,
                start.isoformat() if start else None)
    raw = get_candles(
        broker=broker, symbol=symbol, timeframe=timeframe, limit=limit,
        start=start,
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
