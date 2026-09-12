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

# ── High-frequency sync cadence (Task 3) ─────────────────────────────
# The fast loop pulls fresh broker data for *bot-owned* symbols every
# MARKET_POLL_INTERVAL seconds, then pushes trade instructions BOT_EVAL_DELAY
# seconds after each successful pull (so bots always act on the freshest data).
# Default 5s pull + 1s eval delay = a 6s decision loop.
MARKET_POLL_INTERVAL = int(os.getenv("MARKET_POLL_INTERVAL", "5"))    # bot-symbol pull, seconds
BOT_EVAL_DELAY = int(os.getenv("BOT_EVAL_DELAY", "1"))               # eval fires this long after a pull
# Legacy safety-net eval interval (autonomous bots still get scanned if the fast
# loop ever stalls). Kept slower so it never competes with the chained eval.
BOT_SCAN_INTERVAL = int(os.getenv("BOT_SCAN_INTERVAL", "60"))         # seconds
# Display-only watchlist symbols (no bot on them) are polled on a slower cadence
# so the Markets tab stays fresh without burning broker rate limits at 5s.
WATCHLIST_POLL_INTERVAL = int(os.getenv("WATCHLIST_POLL_INTERVAL", "30"))  # seconds
WATCHLIST_LIMIT = int(os.getenv("MARKET_WATCHLIST_LIMIT", "40"))  # symbols/broker; 0 = entire universe
POLL_TIMEFRAME = os.getenv("MARKET_POLL_TIMEFRAME", "1h")

# Per-broker rate-limit backoff: after a 429/"too many requests", skip that
# broker's remaining symbols and stand down for RATE_LIMIT_BACKOFF seconds.
RATE_LIMIT_BACKOFF = int(os.getenv("RATE_LIMIT_BACKOFF", "30"))       # seconds
_broker_cooldown_until: dict[str, float] = {}

# Optional server-side data credentials so the watchlist (assets nobody has a
# bot on yet) can still be polled for Alpaca, which requires keys for data.
_ENV_ALPACA_KEY = os.getenv("ALPACA_DATA_KEY") or os.getenv("ALPACA_API_KEY")
_ENV_ALPACA_SECRET = os.getenv("ALPACA_DATA_SECRET") or os.getenv("ALPACA_SECRET_KEY")

_scheduler: BackgroundScheduler | None = None


def _collect_bot_targets() -> dict[tuple[str, str], dict]:
    """(broker, symbol) → creds for every symbol a running bot depends on."""
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
            paper = ((b.mode or owner.trading_mode or "paper").lower() == "paper")
            creds = resolve_credentials(owner, broker, paper)
            # Scattershot bots store comma-joined tickers — poll each leg.
            symbols = []
            raw = (b.ticker or "").upper().strip()
            if raw:
                symbols.extend([p.strip() for p in raw.split(",") if p.strip()])
            if (b.low_balance_strategy or "").lower() == "scattershot" and b.strategy_state:
                try:
                    import json
                    state = json.loads(b.strategy_state)
                    for leg in (state.get("legs") or []):
                        sym = (leg.get("ticker") or "").upper().strip()
                        if sym:
                            symbols.append(sym)
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            for sym in dict.fromkeys(symbols):  # preserve order, dedupe
                targets[(broker, sym)] = {"creds": creds, "paper": paper}
    finally:
        db.close()
    return targets


def _collect_watchlist_targets(exclude: set[tuple[str, str]] | None = None) -> dict[tuple[str, str], dict]:
    """
    (broker, symbol) → creds for display-only watchlist assets (Markets tab)
    that no bot is actively trading. ``exclude`` skips symbols already covered by
    the fast bot-symbol loop so we never double-fetch them.
    """
    exclude = exclude or set()
    targets: dict[tuple[str, str], dict] = {}
    for broker, cfg in MARKET_UNIVERSE.items():
        base_creds = {}
        if broker == "alpaca":
            if not (_ENV_ALPACA_KEY and _ENV_ALPACA_SECRET):
                continue  # cannot fetch Alpaca data without keys
            base_creds = {"alpaca_key": _ENV_ALPACA_KEY, "alpaca_secret": _ENV_ALPACA_SECRET}
        items = cfg.get("items", [])
        # 0 / negative = entire universe (Markets tab shows 100+ symbols).
        watch = items if WATCHLIST_LIMIT <= 0 else items[:WATCHLIST_LIMIT]
        for item in watch:
            key = (broker, item["symbol"].upper())
            if key in exclude:
                continue
            targets.setdefault(key, {"creds": base_creds, "paper": True})
    return targets


def _is_rate_limited(err: Exception) -> bool:
    """Best-effort detection of a broker rate-limit / throttling response."""
    s = str(err).lower()
    return any(t in s for t in ("429", "too many requests", "rate limit", "ratelimit", "rate-limit"))


def _poll_targets(targets: dict[tuple[str, str], dict], *, label: str) -> int:
    """
    Fetch + persist + stream quotes for a set of (broker, symbol) targets.

    Resilient by design: every symbol is wrapped in try/except so one bad symbol
    never stops the cycle, and a per-broker rate-limit cooldown skips a broker's
    remaining symbols after a 429 instead of hammering it further.
    """
    if not targets:
        return 0

    import time
    now = time.time()
    updated = 0
    db = SessionLocal()
    try:
        for (broker, symbol), meta in targets.items():
            # Respect an active per-broker rate-limit cooldown.
            cooldown = _broker_cooldown_until.get(broker, 0.0)
            if cooldown and time.time() < cooldown:
                continue
            try:
                analysis = get_market_analysis(
                    broker=broker, symbol=symbol, timeframe=POLL_TIMEFRAME,
                    limit=120, paper=meta.get("paper", True), use_cache=False,
                    **(meta.get("creds") or {}),
                )
            except BrokerError as e:
                if _is_rate_limited(e):
                    _broker_cooldown_until[broker] = time.time() + RATE_LIMIT_BACKOFF
                    logger.warning("[POLL:%s] %s rate-limited — backing off %ss.",
                                   label, broker, RATE_LIMIT_BACKOFF)
                    continue
                logger.debug("[POLL:%s] %s:%s skipped — %s", label, broker, symbol, e)
                continue
            except Exception as e:  # pragma: no cover — never let one symbol kill the loop
                if _is_rate_limited(e):
                    _broker_cooldown_until[broker] = time.time() + RATE_LIMIT_BACKOFF
                    logger.warning("[POLL:%s] %s rate-limited — backing off %ss.",
                                   label, broker, RATE_LIMIT_BACKOFF)
                    continue
                logger.debug("[POLL:%s] %s:%s error — %s", label, broker, symbol, e)
                continue

            # A good fetch clears any lingering cooldown for this broker.
            if _broker_cooldown_until.get(broker):
                _broker_cooldown_until.pop(broker, None)

            candle_ts = analysis.candles[-1]["time"] if analysis.candles else None
            try:
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
            except Exception as e:  # pragma: no cover
                logger.debug("[POLL:%s] persist failed %s:%s — %s", label, broker, symbol, e)
    finally:
        db.close()
    return updated


def _schedule_followup_eval() -> None:
    """
    Push trade instructions BOT_EVAL_DELAY seconds after a successful pull so
    bots always decide on the freshest data. A single reusable one-shot job id
    (replace_existing) means rapid polls collapse into one pending eval instead
    of stacking up.
    """
    if _scheduler is None:
        return
    from datetime import datetime as _dt, timedelta as _td
    try:
        _scheduler.add_job(
            evaluate_bots, "date",
            run_date=_dt.utcnow() + _td(seconds=BOT_EVAL_DELAY),
            id="bot_eval_followup",
            max_instances=1, coalesce=True, replace_existing=True,
            misfire_grace_time=BOT_EVAL_DELAY + 5,
        )
    except Exception as e:  # pragma: no cover
        logger.debug("[ENGINE] follow-up eval scheduling skipped: %s", e)


def poll_market_data() -> None:
    """
    Fast loop: pull fresh broker data for every bot-owned symbol, then chain a
    bot-evaluation pass 1s later. Runs on MARKET_POLL_INTERVAL (default 5s).
    """
    try:
        targets = _collect_bot_targets()
        if targets:
            updated = _poll_targets(targets, label="bots")
            logger.info("[POLL] Refreshed %d/%d bot-symbol quotes.", updated, len(targets))
        else:
            logger.debug("[POLL] No bot-owned symbols to poll this cycle.")
    except Exception as e:  # pragma: no cover — the loop must never die
        logger.error("[POLL] Fast market poll failed: %s", e)
    finally:
        # Always push instructions afterward: autonomous bots (no fixed ticker)
        # aren't in `targets` but still need to be evaluated every cycle.
        _schedule_followup_eval()


def poll_watchlist() -> None:
    """
    Slow loop: refresh display-only Markets-tab symbols that no bot trades, on a
    relaxed cadence so we keep the UI fresh without burning broker rate limits.
    """
    try:
        bot_keys = set(_collect_bot_targets().keys())
        targets = _collect_watchlist_targets(exclude=bot_keys)
        if not targets:
            return
        updated = _poll_targets(targets, label="watchlist")
        logger.info("[POLL] Refreshed %d/%d watchlist quotes.", updated, len(targets))
    except Exception as e:  # pragma: no cover
        logger.error("[POLL] Watchlist poll failed: %s", e)


def evaluate_bots() -> None:
    # AdSense login bypass is HTTP-only — this loop still trades for every real
    # owner's running bots using credentials stored on their User row.
    try:
        summary = bot_engine.run_all_active_bots()
        if summary.get("scanned"):
            logger.info("[ENGINE] Bot evaluation cycle: %s", summary)
    except Exception as e:  # pragma: no cover
        logger.error("[ENGINE] Bot evaluation cycle failed: %s", e)


def send_monthly_statements() -> None:
    """Daily cron: emails monthly statements on the 1st (idempotent per user)."""
    try:
        from app import statements
        statements.send_due_statements()
    except Exception as e:  # pragma: no cover
        logger.error("[STATEMENTS] Monthly statement job failed: %s", e)


def start_scheduler() -> BackgroundScheduler | None:
    """Start and return the background trading scheduler when enabled."""
    global _scheduler
    if os.getenv("ENGINE_ENABLED", "1") not in ("1", "true", "True", "yes"):
        logger.info("[ENGINE] Background engine disabled (ENGINE_ENABLED=0).")
        return None
    if _scheduler is not None:
        return _scheduler

    from datetime import datetime as _dt

    sched = BackgroundScheduler(timezone="UTC")

    # NOTE: do NOT pass next_run_time=None — that pauses the job in APScheduler
    # and it will NEVER fire automatically. Pass next_run_time=now so the first
    # interval fire is immediate (no separate boot job that can overlap).
    now = _dt.utcnow()
    # Fast loop: pull bot-owned symbols every MARKET_POLL_INTERVAL s; it chains a
    # bot-eval BOT_EVAL_DELAY s after each pull (see poll_market_data).
    sched.add_job(poll_market_data, "interval", seconds=MARKET_POLL_INTERVAL,
                  id="market_poll", max_instances=1, coalesce=True, next_run_time=now)
    # Slow loop: refresh display-only watchlist symbols on a relaxed cadence.
    sched.add_job(poll_watchlist, "interval", seconds=WATCHLIST_POLL_INTERVAL,
                  id="watchlist_poll", max_instances=1, coalesce=True, next_run_time=now)
    # Safety-net eval so autonomous bots still run if the fast loop ever stalls.
    sched.add_job(evaluate_bots, "interval", seconds=BOT_SCAN_INTERVAL,
                  id="bot_eval", max_instances=1, coalesce=True, next_run_time=now)
    # Monthly statements: run daily at 13:00 UTC; the job itself only sends on
    # the 1st and is idempotent per user, so restarts can't double-send.
    sched.add_job(send_monthly_statements, "cron", hour=13, minute=0,
                  id="monthly_statements", max_instances=1, coalesce=True)
    sched.start()
    _scheduler = sched
    logger.info(
        "[ENGINE] Background engine started — bot-symbol pull every %ss, eval +%ss after each pull, "
        "watchlist every %ss, safety-net eval every %ss.",
        MARKET_POLL_INTERVAL, BOT_EVAL_DELAY, WATCHLIST_POLL_INTERVAL, BOT_SCAN_INTERVAL,
    )

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
