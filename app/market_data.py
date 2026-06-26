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

from app.brokers import get_candles, BrokerError
from app.pattern_analysis import Candle, analyze_candles, Analysis

logger = logging.getLogger("alphabot.marketdata")

_CACHE: dict[tuple, tuple[float, Analysis]] = {}
_CACHE_TTL_SECONDS = 20
_CACHE_LOCK = threading.Lock()

# OKX uses upper-case timeframes for some intervals; we normalize on input.
_TF_NORMALIZE = {"1H": "1h", "4H": "4h", "1D": "1d", "1W": "1w"}


def _normalize_timeframe(broker: str, timeframe: str) -> str:
    tf = _TF_NORMALIZE.get(timeframe, timeframe)
    return tf


def get_market_analysis(broker: str, symbol: str, timeframe: str = "1h",
                        limit: int = 200, *, alpaca_key=None, alpaca_secret=None,
                        okx_key=None, okx_secret=None, okx_passphrase=None,
                        paper: bool = True, use_cache: bool = True) -> Analysis:
    """
    Fetch candles for (broker, symbol, timeframe) and return a fully analyzed
    ``Analysis`` object. Raises ``BrokerError`` on data/connectivity failure so
    callers can decide how to degrade.
    """
    broker = (broker or "alpaca").lower()
    timeframe = _normalize_timeframe(broker, timeframe)
    cache_key = (broker, symbol.upper(), timeframe, limit)

    if use_cache:
        with _CACHE_LOCK:
            hit = _CACHE.get(cache_key)
            if hit and (time.time() - hit[0]) < _CACHE_TTL_SECONDS:
                logger.info("[CACHE HIT] %s %s %s", broker, symbol, timeframe)
                return hit[1]

    logger.info("[FETCH] %s %s tf=%s limit=%d", broker, symbol, timeframe, limit)
    raw = get_candles(
        broker=broker, symbol=symbol, timeframe=timeframe, limit=limit,
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
