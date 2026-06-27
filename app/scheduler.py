"""
scheduler.py — the always-on background engine.

Two independent worker loops run on a thread-backed APScheduler, completely
decoupled from any frontend HTTP request:

  1. ``poll_market_data`` — fetches live market data 24/7 and continuously
     writes it into Postgres (``market_quotes``), then streams each update over
     SSE. This is what keeps prices fresh (no more MSFT frozen at $385) whether
     or not a browser is open.

  2. ``evaluate_bots`` — continuously re-evaluates every running bot against the
     freshly-stored market state and executes trades automatically.

The loops are configured with ``max_instances=1`` + ``coalesce=True`` so a slow
cycle can never stack up or run concurrently with itself.
"""

from __future__ import annotations

import os
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.database import SessionLocal, Bot
from app.markets_universe import MARKET_UNIVERSE
from app.credentials import resolve_data_credentials, has_data_credentials
from app.market_data import get_market_analysis
from app.market_store import upsert_quote
from app.brokers import BrokerError
from app.realtime import bus
from app import bot_engine

logger = logging.getLogger("alphabot.scheduler")

MARKET_POLL_INTERVAL = int(os.getenv("MARKET_POLL_INTERVAL", "30"))   # seconds
BOT_SCAN_INTERVAL = int(os.getenv("BOT_SCAN_INTERVAL", "30"))         # seconds
WATCHLIST_LIMIT = int(os.getenv("MARKET_WATCHLIST_LIMIT", "20"))      # symbols/broker
POLL_TIMEFRAME = os.getenv("MARKET_POLL_TIMEFRAME", "1h")

_scheduler: BackgroundScheduler | None = None


def _collect_symbols() -> set[tuple[str, str]]:
    """
    Build the set of (broker, symbol) to poll: every running bot's ticker plus a
    per-broker watchlist so the universal charts stay fresh even for assets
    nobody has a bot on yet. Credentials are NOT decided here — market data
    always uses the GLOBAL data keys (see resolve_data_credentials).
    """
    symbols: set[tuple[str, str]] = set()
    db = SessionLocal()
    try:
        bots = db.query(Bot).filter(Bot.running == True, Bot.ticker.isnot(None)).all()  # noqa: E712
        for b in bots:
            symbols.add(((b.broker or "alpaca").lower(), b.ticker.upper()))
    finally:
        db.close()

    for broker, cfg in MARKET_UNIVERSE.items():
        if not has_data_credentials(broker):
            continue  # e.g. Alpaca with no global ALPACA_DATA_KEY configured
        for item in cfg.get("items", [])[:WATCHLIST_LIMIT]:
            symbols.add((broker, item["symbol"].upper()))
    return symbols


def poll_market_data() -> None:
    symbols = _collect_symbols()
    if not symbols:
        logger.debug("[POLL] No symbols to poll this cycle.")
        return
    updated = 0
    db = SessionLocal()
    try:
        for (broker, symbol) in symbols:
            if not has_data_credentials(broker):
                continue
            try:
                # GLOBAL market-data credentials only — never a user's keys.
                analysis = get_market_analysis(
                    broker=broker, symbol=symbol, timeframe=POLL_TIMEFRAME,
                    limit=120, paper=True, use_cache=False,
                    **resolve_data_credentials(broker),
                )
            except BrokerError as e:
                logger.debug("[POLL] %s:%s skipped — %s", broker, symbol, e)
                continue
            except Exception as e:  # pragma: no cover
                logger.debug("[POLL] %s:%s error — %s", broker, symbol, e)
                continue

            candle_ts = analysis.candles[-1]["time"] if analysis.candles else None
            upsert_quote(db, broker, symbol, analysis.last_price,
                         signal_action=analysis.signal.action,
                         signal_strength=analysis.signal.strength,
                         candle_ts=candle_ts)
            bus.publish("market_quote", {
                "broker": broker, "symbol": symbol, "price": analysis.last_price,
                "signal_action": analysis.signal.action,
                "signal_strength": analysis.signal.strength,
            })
            updated += 1
    finally:
        db.close()
    logger.info("[POLL] Refreshed %d/%d market quotes.", updated, len(symbols))


def evaluate_bots() -> None:
    try:
        summary = bot_engine.run_all_active_bots()
        if summary.get("scanned"):
            logger.info("[ENGINE] Bot evaluation cycle: %s", summary)
    except Exception as e:  # pragma: no cover
        logger.error("[ENGINE] Bot evaluation cycle failed: %s", e)


def start_scheduler() -> BackgroundScheduler | None:
    global _scheduler
    if os.getenv("ENGINE_ENABLED", "1") not in ("1", "true", "True", "yes"):
        logger.info("[ENGINE] Background engine disabled (ENGINE_ENABLED=0).")
        return None
    if _scheduler is not None:
        return _scheduler

    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(poll_market_data, "interval", seconds=MARKET_POLL_INTERVAL,
                  id="market_poll", max_instances=1, coalesce=True,
                  next_run_time=None)
    sched.add_job(evaluate_bots, "interval", seconds=BOT_SCAN_INTERVAL,
                  id="bot_eval", max_instances=1, coalesce=True,
                  next_run_time=None)
    sched.start()
    _scheduler = sched
    logger.info("[ENGINE] Background engine started (market=%ss, bots=%ss).",
                MARKET_POLL_INTERVAL, BOT_SCAN_INTERVAL)
    # Kick an immediate first market poll so the DB is warm without waiting a full interval.
    try:
        sched.add_job(poll_market_data, id="market_poll_initial", max_instances=1)
    except Exception:
        pass
    return sched


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None
        logger.info("[ENGINE] Background engine stopped.")
