"""
bot_engine.py — the autonomous trading "brain".

This is a full rewrite from the old single-spot-price + LLM design. Bots now
consume the *exact same* structured pattern analysis that powers the Market
Dashboard (see ``market_data.get_market_analysis``), so the UI and the bots
always agree on what the chart is doing.

Strategy — "Buy the Dip, Sell the Peak" with hard capital protection:

  ENTRY (strict):
    Only deploy when the shared signal confirms a structural reversal / oversold
    dip (signal.action == BUY with conviction >= ENTRY_MIN_STRENGTH). Optional
    manual buy_limit and first_buy_price gates are respected on top of that.

  RISK (strict):
    On entry we arm an ATR-based trailing stop and an adaptive take-profit.
    Every cycle the trailing stop ratchets up with new highs and NEVER loosens,
    instantly cutting trades that roll over. Take-profit locks gains but ratchets
    higher while momentum stays bullish so winners are allowed to run.

  CAPITAL ROTATION:
    If a high-probability setup appears but buying power is fully deployed, the
    engine ranks the user's open positions, liquidates the weakest/stagnating
    one, and rotates that freed capital into the stronger setup.

Every scan, pattern match, stop adjustment and execution signal is logged to
both the terminal (logger) and the per-user ActivityLog table.
"""

from __future__ import annotations

import os
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from database import Bot, Trade, User, ActivityLog, SessionLocal
from brokers import place_order, get_account_info, BrokerError
from market_data import get_market_analysis
from pattern_analysis import Analysis

logger = logging.getLogger("alphabot.engine")

# ── Tunable strategy parameters ──────────────────────────────────────
ENTRY_MIN_STRENGTH = float(os.getenv("BOT_ENTRY_MIN_STRENGTH", "0.30"))
DEPLOY_FRACTION = float(os.getenv("BOT_DEPLOY_FRACTION", "0.95"))   # aggressive deployment
TRAIL_ATR_MULT = float(os.getenv("BOT_TRAIL_ATR_MULT", "1.5"))     # tight trailing stop
TP_ATR_MULT = float(os.getenv("BOT_TP_ATR_MULT", "3.0"))           # adaptive take-profit
TRAIL_PCT_FLOOR = float(os.getenv("BOT_TRAIL_PCT_FLOOR", "0.02"))  # min 2% trailing buffer
ROTATION_STAGNANT_PCT = float(os.getenv("BOT_ROTATION_STAGNANT_PCT", "0.5"))  # <0.5% = stagnating
CANDLE_LIMIT = int(os.getenv("BOT_CANDLE_LIMIT", "200"))


# ────────────────────────────────────────────────────────────────────
# Logging helper — writes to terminal AND the user-visible ActivityLog
# ────────────────────────────────────────────────────────────────────
def _log(db: Session, user_id: int, message: str, level: str = "INFO"):
    getattr(logger, level.lower(), logger.info)(message)
    try:
        db.add(ActivityLog(user_id=user_id, message=message, level=level))
        db.commit()
    except Exception as e:  # logging must never break the trading loop
        db.rollback()
        logger.warning("ActivityLog write failed: %s", e)


def _paper(owner: User) -> bool:
    return (owner.trading_mode or "paper") == "paper"


def _keys(owner: User) -> dict:
    return dict(
        alpaca_key=owner.alpaca_key, alpaca_secret=owner.alpaca_secret,
        okx_key=owner.okx_key, okx_secret=owner.okx_secret,
        okx_passphrase=owner.okx_pass,
    )


# ────────────────────────────────────────────────────────────────────
# Buying power lookup (broker-aware, never raises)
# ────────────────────────────────────────────────────────────────────
def _get_buying_power(owner: User, broker: str) -> float | None:
    """Return available cash/buying power, or None if it can't be determined."""
    try:
        info = get_account_info(
            broker=broker, alpaca_key=owner.alpaca_key, alpaca_secret=owner.alpaca_secret,
            okx_key=owner.okx_key, okx_secret=owner.okx_secret,
            okx_passphrase=owner.okx_pass, paper=_paper(owner),
        )
        if broker == "alpaca":
            return float(info.get("buying_power", 0.0))
        balances = info.get("balances", {})
        return float(balances.get("USDT", balances.get("USD", 0.0)))
    except Exception as e:
        logger.warning("Buying power lookup failed (%s): %s", broker, e)
        return None


# ────────────────────────────────────────────────────────────────────
# Order helper — real order if keys present, otherwise a logged paper sim
# ────────────────────────────────────────────────────────────────────
def _execute(owner: User, broker: str, side: str, symbol: str,
             qty: float = None, notional: float = None) -> dict:
    try:
        order = place_order(
            broker=broker, side=side, symbol=symbol, qty=qty, notional=notional,
            alpaca_key=owner.alpaca_key, alpaca_secret=owner.alpaca_secret,
            okx_key=owner.okx_key, okx_secret=owner.okx_secret,
            okx_passphrase=owner.okx_pass, paper=_paper(owner),
        )
        order["simulated"] = False
        return order
    except BrokerError as e:
        # No/invalid keys → record a simulated fill so the strategy + ledger
        # remain observable in paper mode without crashing the cycle.
        logger.warning("[SIM] %s %s %s simulated (broker said: %s)", side, symbol, broker, e)
        return {"order_id": f"SIM-{datetime.utcnow().timestamp():.0f}",
                "status": "simulated", "symbol": symbol, "side": side, "simulated": True}


# ────────────────────────────────────────────────────────────────────
# Risk-management arming / ratcheting
# ────────────────────────────────────────────────────────────────────
def _arm_risk(bot: Bot, entry_price: float, atr: float | None,
              nearest_resistance: float | None):
    """Set the initial trailing stop and adaptive take-profit on entry."""
    bot.peak_price = entry_price
    atr = atr or (entry_price * TRAIL_PCT_FLOOR)
    trail_dist = max(atr * TRAIL_ATR_MULT, entry_price * TRAIL_PCT_FLOOR)
    bot.stop_price = round(entry_price - trail_dist, 6)

    tp_atr = entry_price + atr * TP_ATR_MULT
    # If resistance is overhead, aim just under it; otherwise use the ATR target.
    if nearest_resistance and nearest_resistance > entry_price:
        bot.take_profit_price = round(min(tp_atr, nearest_resistance * 0.998), 6)
    else:
        bot.take_profit_price = round(tp_atr, 6)


def _ratchet_risk(bot: Bot, price: float, atr: float | None, bullish: bool):
    """Tighten the trailing stop on new highs; let take-profit run if bullish."""
    if bot.peak_price is None or price > bot.peak_price:
        bot.peak_price = price
        atr = atr or (price * TRAIL_PCT_FLOOR)
        trail_dist = max(atr * TRAIL_ATR_MULT, price * TRAIL_PCT_FLOOR)
        new_stop = round(price - trail_dist, 6)
        if bot.stop_price is None or new_stop > bot.stop_price:
            bot.stop_price = new_stop  # never loosen
        if bullish and bot.take_profit_price is not None:
            ratcheted = round(price + (atr * TP_ATR_MULT), 6)
            if ratcheted > bot.take_profit_price:
                bot.take_profit_price = ratcheted


# ────────────────────────────────────────────────────────────────────
# Capital rotation
# ────────────────────────────────────────────────────────────────────
def _attempt_capital_rotation(db: Session, owner: User, candidate_bot: Bot,
                              candidate_strength: float, needed_notional: float) -> bool:
    """
    Free capital by liquidating the weakest/stagnating open position so the
    stronger candidate setup can be funded. Returns True if capital was freed.
    """
    open_bots = [
        b for b in db.query(Bot).filter(
            Bot.owner_id == owner.id, Bot.in_position == True  # noqa: E712
        ).all() if b.id != candidate_bot.id and b.avg_entry_price
    ]
    if not open_bots:
        _log(db, owner.id, "[ROTATION] No open positions available to rotate capital from.")
        return False

    ranked = []
    for b in open_bots:
        try:
            a = _safe_analysis(b, owner)
            cur = a.last_price if a else b.avg_entry_price
        except Exception:
            cur = b.avg_entry_price
        perf_pct = ((cur - b.avg_entry_price) / b.avg_entry_price * 100) if b.avg_entry_price else 0.0
        ranked.append((perf_pct, cur, b))

    ranked.sort(key=lambda x: x[0])  # weakest first
    worst_perf, worst_price, worst_bot = ranked[0]

    if worst_perf >= ROTATION_STAGNANT_PCT:
        _log(db, owner.id,
             f"[ROTATION] Best exit candidate '{worst_bot.name}' is performing "
             f"+{worst_perf:.2f}% — above stagnation threshold; NOT rotating capital.")
        return False

    _log(db, owner.id,
         f"[ROTATION] Liquidating weakest position '{worst_bot.name}' ({worst_bot.ticker}) "
         f"at {worst_perf:+.2f}% to fund stronger setup '{candidate_bot.ticker}' "
         f"(strength {candidate_strength:.2f}).", "WARNING")

    _close_position(db, owner, worst_bot, worst_price, reason="capital rotation")
    return True


# ────────────────────────────────────────────────────────────────────
# Position close
# ────────────────────────────────────────────────────────────────────
def _close_position(db: Session, owner: User, bot: Bot, price: float, reason: str) -> dict:
    qty = bot.shares_held or 0
    order = _execute(owner, bot.broker, "sell", bot.ticker, qty=qty)
    gain = (price - (bot.avg_entry_price or price)) * qty
    bot.realized_pnl = (bot.realized_pnl or 0) + gain
    bot.trade_count = (bot.trade_count or 0) + 1
    db.add(Trade(owner_id=owner.id, bot_id=bot.id, ticker=bot.ticker, side="sell",
                 qty=qty, price=price, broker=bot.broker, mode=owner.trading_mode,
                 broker_order_id=order["order_id"], status=order["status"]))
    _log(db, owner.id,
         f"[EXECUTE] SELL {qty:.6f} {bot.ticker} @ {price:.4f} ({reason}). "
         f"Realized P&L: {gain:+.2f}.",
         "WARNING" if gain < 0 else "INFO")
    bot.in_position = False
    bot.shares_held = 0
    bot.avg_entry_price = None
    bot.peak_price = None
    bot.stop_price = None
    bot.take_profit_price = None
    db.commit()
    return {"order": order, "realized_gain": round(gain, 4)}


# ────────────────────────────────────────────────────────────────────
# Analysis fetch wrapper
# ────────────────────────────────────────────────────────────────────
def _safe_analysis(bot: Bot, owner: User) -> Analysis | None:
    try:
        return get_market_analysis(
            broker=bot.broker or owner.active_broker or "alpaca",
            symbol=bot.ticker, timeframe=bot.timeframe or "1h", limit=CANDLE_LIMIT,
            alpaca_key=owner.alpaca_key, alpaca_secret=owner.alpaca_secret,
            okx_key=owner.okx_key, okx_secret=owner.okx_secret,
            okx_passphrase=owner.okx_pass, paper=_paper(owner),
        )
    except BrokerError as e:
        logger.warning("Analysis fetch failed for bot %s (%s): %s", bot.id, bot.ticker, e)
        return None


# ────────────────────────────────────────────────────────────────────
# Core single-bot cycle (operates on a provided Analysis — testable)
# ────────────────────────────────────────────────────────────────────
def run_bot_cycle(db: Session, bot: Bot, analysis: Analysis) -> dict:
    owner: User = bot.owner
    price = analysis.last_price
    sig = analysis.signal
    atr = analysis.indicators.get("atr")
    nearest_resistance = analysis.levels.get("nearest_resistance")

    # Persist the latest analysis snapshot for the UI's "Internal Bot Status".
    bot.last_signal = sig.action
    bot.last_analysis_at = datetime.utcnow()
    bot.last_pattern_summary = (
        f"{sig.bias.title()} | {sig.headline} "
        f"(conviction {sig.confidence}, strength {sig.strength:.2f})"
    )

    _log(db, owner.id,
         f"[SCAN] {bot.ticker} @ {price:.4f} | signal={sig.action} "
         f"strength={sig.strength:.2f} bias={sig.bias} | "
         f"patterns={[p['name'] for p in analysis.patterns]}")

    if not bot.running:
        db.commit()
        return {"action": "SKIPPED", "reason": "Bot is paused.",
                "analysis": _analysis_brief(analysis)}

    result = {"action": "HOLD", "price": price, "reason": "",
              "analysis": _analysis_brief(analysis), "order": None}

    # ── MANAGE OPEN POSITION ──────────────────────────────────────
    if bot.in_position and bot.avg_entry_price:
        _ratchet_risk(bot, price, atr, bullish=(sig.bias == "bullish"))
        profit_pct = (price - bot.avg_entry_price) / bot.avg_entry_price * 100

        # 1) Trailing stop — hard capital protection, always fires.
        if bot.stop_price is not None and price <= bot.stop_price:
            _log(db, owner.id,
                 f"[STOP] {bot.ticker} hit trailing stop {bot.stop_price:.4f} "
                 f"(entry {bot.avg_entry_price:.4f}, {profit_pct:+.2f}%).", "WARNING")
            closed = _close_position(db, owner, bot, price, reason="trailing stop")
            result.update({"action": "SELL", "reason": "Trailing stop hit", **closed})
            return result

        # 2) Take-profit — lock gains at the peak target.
        if bot.take_profit_price is not None and price >= bot.take_profit_price:
            _log(db, owner.id,
                 f"[TAKE-PROFIT] {bot.ticker} reached target {bot.take_profit_price:.4f} "
                 f"({profit_pct:+.2f}%).")
            closed = _close_position(db, owner, bot, price, reason="take-profit target")
            result.update({"action": "SELL", "reason": "Take-profit target hit", **closed})
            return result

        # 3) Structural peak/breakdown signal (respect manual constraints).
        if sig.action == "SELL" and sig.strength >= ENTRY_MIN_STRENGTH:
            sell_ok = (not bot.sell_limit) or price >= bot.sell_limit
            profit_ok = (not bot.min_profit_pct) or profit_pct >= bot.min_profit_pct
            if sell_ok and profit_ok:
                _log(db, owner.id,
                     f"[SIGNAL] Structural peak on {bot.ticker} ({sig.headline}).")
                closed = _close_position(db, owner, bot, price, reason="structural peak signal")
                result.update({"action": "SELL", "reason": sig.headline, **closed})
                return result

        db.commit()
        result["reason"] = (
            f"Holding {bot.ticker} {profit_pct:+.2f}% | stop {bot.stop_price} "
            f"| target {bot.take_profit_price}"
        )
        return result

    # ── LOOK FOR AN ENTRY ─────────────────────────────────────────
    if sig.action != "BUY" or sig.strength < ENTRY_MIN_STRENGTH:
        db.commit()
        result["reason"] = f"No confirmed dip/reversal (signal {sig.action} {sig.strength:.2f})."
        return result

    # Manual gates layered on top of the structural signal.
    if bot.buy_limit and price > bot.buy_limit:
        db.commit()
        result["reason"] = f"Setup confirmed but price {price:.4f} above buy limit {bot.buy_limit}."
        return result
    if bot.first_buy_price and not bot.first_buy_done and price > bot.first_buy_price:
        db.commit()
        result["reason"] = f"Waiting for first-buy trigger at {bot.first_buy_price}."
        return result

    notional = round((bot.funds_allocated or 0) * DEPLOY_FRACTION, 2)
    if notional <= 0:
        db.commit()
        result["reason"] = "No funds allocated to this bot."
        return result

    # Capital availability + rotation.
    buying_power = _get_buying_power(owner, bot.broker or "alpaca")
    if buying_power is not None and buying_power < notional:
        _log(db, owner.id,
             f"[CAPITAL] Setup on {bot.ticker} needs ${notional:.2f} but only "
             f"${buying_power:.2f} free — evaluating capital rotation.", "WARNING")
        freed = _attempt_capital_rotation(db, owner, bot, sig.strength, notional)
        if not freed:
            db.commit()
            result.update({"action": "WAIT",
                           "reason": "Setup confirmed but capital fully deployed; no weaker position to rotate."})
            return result
        buying_power = _get_buying_power(owner, bot.broker or "alpaca")
        if buying_power is not None:
            notional = min(notional, round(buying_power * DEPLOY_FRACTION, 2))

    # Execute the entry.
    order = _execute(owner, bot.broker, "buy", bot.ticker, notional=notional)
    bot.in_position = True
    bot.avg_entry_price = price
    bot.shares_held = notional / price if price else 0
    bot.trade_count = (bot.trade_count or 0) + 1
    bot.first_buy_done = True
    _arm_risk(bot, price, atr, nearest_resistance)
    db.add(Trade(owner_id=owner.id, bot_id=bot.id, ticker=bot.ticker, side="buy",
                 notional=notional, price=price, broker=bot.broker, mode=owner.trading_mode,
                 broker_order_id=order["order_id"], status=order["status"]))
    _log(db, owner.id,
         f"[EXECUTE] BUY ${notional:.2f} of {bot.ticker} @ {price:.4f} "
         f"({sig.headline}). Stop {bot.stop_price:.4f} | Target {bot.take_profit_price:.4f}.")
    db.commit()
    result.update({"action": "BUY", "reason": sig.headline, "order": order,
                   "notional": notional, "stop_price": bot.stop_price,
                   "take_profit_price": bot.take_profit_price})
    return result


def _analysis_brief(a: Analysis) -> dict:
    return {
        "signal": a.signal.to_dict(),
        "indicators": a.indicators,
        "patterns": a.patterns,
        "last_price": a.last_price,
    }


# ────────────────────────────────────────────────────────────────────
# Public entry points
# ────────────────────────────────────────────────────────────────────
def run_cycle(bot_id: int) -> dict:
    """
    Run one full decision cycle for a single bot by id. Opens its own DB
    session so it is safe to call from an API route or a background scheduler.
    """
    db = SessionLocal()
    try:
        bot = db.query(Bot).filter(Bot.id == bot_id).first()
        if not bot:
            return {"action": "ERROR", "reason": "Bot not found."}
        analysis = _safe_analysis(bot, bot.owner)
        if analysis is None:
            bot.last_pattern_summary = "Market data unavailable (check broker keys/connectivity)."
            db.commit()
            return {"action": "ERROR",
                    "reason": f"Could not fetch market data for {bot.ticker}."}
        return run_bot_cycle(db, bot, analysis)
    finally:
        db.close()


def run_all_active_bots() -> dict:
    """Scan + act on every running bot. Intended for a background scheduler."""
    db = SessionLocal()
    summary = {"scanned": 0, "actions": []}
    try:
        bots = db.query(Bot).filter(Bot.running == True).all()  # noqa: E712
        logger.info("[ENGINE] Scanning %d active bots", len(bots))
        for bot in bots:
            summary["scanned"] += 1
            analysis = _safe_analysis(bot, bot.owner)
            if analysis is None:
                continue
            res = run_bot_cycle(db, bot, analysis)
            if res.get("action") not in (None, "HOLD", "SKIPPED", "WAIT"):
                summary["actions"].append({"bot_id": bot.id, "ticker": bot.ticker,
                                           "action": res["action"]})
    except Exception as e:
        logger.error("run_all_active_bots failed: %s", e)
    finally:
        db.close()
    return summary
