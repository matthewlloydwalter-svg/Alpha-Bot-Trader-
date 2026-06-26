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

from app.database import SessionLocal, Bot, User
from app.markets_universe import MARKET_UNIVERSE
from app.credentials import resolve_credentials
from app.market_data import get_market_analysis
from app.market_store import upsert_quote
from app.brokers import BrokerError
from app.realtime import bus
from app import bot_engine

logger = logging.getLogger("alphabot.scheduler")

MARKET_POLL_INTERVAL = int(os.getenv("MARKET_POLL_INTERVAL", "30"))   # seconds
BOT_SCAN_INTERVAL = int(os.getenv("BOT_SCAN_INTERVAL", "60"))         # seconds
WATCHLIST_LIMIT = int(os.getenv("MARKET_WATCHLIST_LIMIT", "20"))      # symbols/broker
POLL_TIMEFRAME = os.getenv("MARKET_POLL_TIMEFRAME", "1h")

# Optional server-side data credentials so the watchlist (assets nobody has a
# bot on yet) can still be polled for Alpaca, which requires keys for data.
_ENV_ALPACA_KEY = os.getenv("ALPACA_DATA_KEY") or os.getenv("ALPACA_API_KEY")
_ENV_ALPACA_SECRET = os.getenv("ALPACA_DATA_SECRET") or os.getenv("ALPACA_SECRET_KEY")

_scheduler: BackgroundScheduler | None = None


def _collect_targets() -> dict[tuple[str, str], dict]:
    """
    Build the set of (broker, symbol) to poll plus the credentials to use.

    Bot-owned symbols use their owner's stored keys; the remaining watchlist
    falls back to server env keys (Alpaca) / public access (OKX).
    """
    targets: dict[tuple[str, str], dict] = {}
    db = SessionLocal()
    try:
        bots = db.query(Bot).filter(Bot.running == True, Bot.ticker.isnot(None)).all()  # noqa: E712
        owners: dict[int, User] = {}
        for b in bots:
            broker = (b.broker or "alpaca").lower()
            owner = owners.get(b.owner_id) or db.query(User).filter(User.id == b.owner_id).first()
            if owner is None:
                continue
            owners[b.owner_id] = owner
            paper = (owner.trading_mode or "paper") == "paper"
            creds = resolve_credentials(owner, broker, paper)
            targets[(broker, b.ticker.upper())] = {"creds": creds, "paper": paper}

        # Watchlist fallback for assets without a bot.
        for broker, cfg in MARKET_UNIVERSE.items():
            base_creds = {}
            if broker == "alpaca":
                if not (_ENV_ALPACA_KEY and _ENV_ALPACA_SECRET):
                    continue  # cannot fetch Alpaca data without keys
                base_creds = {"alpaca_key": _ENV_ALPACA_KEY, "alpaca_secret": _ENV_ALPACA_SECRET}
            for item in cfg.get("items", [])[:WATCHLIST_LIMIT]:
                key = (broker, item["symbol"].upper())
                targets.setdefault(key, {"creds": base_creds, "paper": True})
    finally:
        db.close()
    return targets


def poll_market_data() -> None:
    targets = _collect_targets()
    if not targets:
        logger.debug("[POLL] No symbols to poll this cycle.")
        return
    updated = 0
    db = SessionLocal()
    try:
        for (broker, symbol), meta in targets.items():
            try:
                analysis = get_market_analysis(
                    broker=broker, symbol=symbol, timeframe=POLL_TIMEFRAME,
                    limit=120, paper=meta.get("paper", True), use_cache=False,
                    **(meta.get("creds") or {}),
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
    logger.info("[POLL] Refreshed %d/%d market quotes.", updated, len(targets))


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
