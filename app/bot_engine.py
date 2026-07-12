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
import json
import logging
import threading
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.database import Bot, Trade, User, ActivityLog, SessionLocal
from app.brokers import place_order, get_account_info, liquidate_position, BrokerError
from app.market_data import get_market_analysis
from app.markets_universe import MARKET_UNIVERSE
from app.credentials import resolve_credentials, has_credentials
from app.market_hours import market_open_for_broker, is_open_entry_window, is_eod_exit_window, session_date_et, next_market_open
from app.news_analysis import get_asset_sentiment
from app.pattern_analysis import Analysis

logger = logging.getLogger("alphabot.engine")

# ── Tunable strategy parameters ──────────────────────────────────────
ENTRY_MIN_STRENGTH = float(os.getenv("BOT_ENTRY_MIN_STRENGTH", "0.38"))
DEPLOY_FRACTION = float(os.getenv("BOT_DEPLOY_FRACTION", "0.90"))   # slightly more conservative sizing
EXIT_MODE = os.getenv("BOT_EXIT_MODE", "fixed_pct").strip().lower()  # fixed_pct or atr
TRAIL_ATR_MULT = float(os.getenv("BOT_TRAIL_ATR_MULT", "1.6"))     # slightly wider stop to avoid noise
TP_ATR_MULT = float(os.getenv("BOT_TP_ATR_MULT", "3.2"))           # slightly more patient profit target
TRAIL_PCT_FLOOR = float(os.getenv("BOT_TRAIL_PCT_FLOOR", "0.02"))  # min 2% trailing buffer


def _get_exit_percentages() -> tuple[float, float]:
    """Read stop/take-profit percentages from Railway-style env vars."""
    stop_raw = (
        os.getenv("STOP_LOSS_PERCENT")
        or os.getenv("BOT_STOP_LOSS_PCT")
        or os.getenv("STOP_LOSS_PCT")
        or "0.005"
    )
    take_raw = (
        os.getenv("TAKE_PROFIT_PERCENT")
        or os.getenv("BOT_TAKE_PROFIT_PCT")
        or os.getenv("TAKE_PROFIT_PCT")
        or "0.03"
    )
    return float(stop_raw), float(take_raw)


def _get_display_risk_targets(entry_price: float | None) -> tuple[float | None, float | None]:
    """Calculate stop/target prices from the entry price and current env vars."""
    if entry_price is None or entry_price <= 0:
        return None, None
    stop_pct, take_pct = _get_exit_percentages()
    stop_dist = max(float(entry_price) * stop_pct, 0.01)
    target_dist = max(float(entry_price) * take_pct, 0.01)
    return round(float(entry_price) - stop_dist, 6), round(float(entry_price) + target_dist, 6)


def _market_collision_blocked(db: Session, owner: User, bot: Bot, ticker: str | None, quality_score: float) -> tuple[bool, str | None]:
    """Prevent multiple bots from entering the same ticker simultaneously unless the setup is exceptional."""
    if not ticker:
        return False, None
    ticker = ticker.upper()
    conflicts = (
        db.query(Bot)
        .filter(
            Bot.owner_id == owner.id,
            Bot.id != bot.id,
            Bot.in_position == True,  # noqa: E712
            Bot.ticker.isnot(None),
        )
        .all()
    )
    for other in conflicts:
        if ticker in _bot_held_tickers(other):
            if quality_score >= 1.0:
                return False, None
            return True, f"{ticker} is already occupied by another active bot ({other.name})"
    return False, None


def _bot_held_tickers(bot: Bot) -> set[str]:
    """Symbols a bot currently occupies (handles scattershot multi-leg tickers)."""
    held: set[str] = set()
    raw = (bot.ticker or "").upper().strip()
    if raw:
        for part in raw.split(","):
            part = part.strip()
            if part:
                held.add(part)
    if (bot.low_balance_strategy or "").lower() == "scattershot":
        for leg in (_load_strategy_state(bot).get("legs") or []):
            sym = (leg.get("ticker") or "").upper().strip()
            if sym:
                held.add(sym)
    return held


CONSERVATIVE_ENTRY_FILTER = os.getenv("BOT_CONSERVATIVE_ENTRY_FILTER", "1").strip().lower() in ("1", "true", "yes", "on")
TREND_CONFIRMATION_FILTER = os.getenv("BOT_TREND_CONFIRMATION_FILTER", "1").strip().lower() in ("1", "true", "yes", "on")
VOLATILITY_SIZING = os.getenv("BOT_VOLATILITY_SIZING", "1").strip().lower() in ("1", "true", "yes", "on")
QUALITY_SETUP_SCORING = os.getenv("BOT_QUALITY_SETUP_SCORING", "1").strip().lower() in ("1", "true", "yes", "on")
MIN_SETUP_QUALITY_SCORE = float(os.getenv("BOT_MIN_SETUP_QUALITY_SCORE", "0.60"))
RISK_PER_TRADE_PCT = float(os.getenv("BOT_RISK_PER_TRADE_PCT", "0.01"))
MAX_POSITION_PCT = float(os.getenv("BOT_MAX_POSITION_PCT", "0.10"))
ROTATION_STAGNANT_PCT = float(os.getenv("BOT_ROTATION_STAGNANT_PCT", "0.5"))  # <0.5% = stagnating
CANDLE_LIMIT = int(os.getenv("BOT_CANDLE_LIMIT", "200"))
AUTO_SCAN_LIMIT = int(os.getenv("BOT_AUTO_SCAN_LIMIT", "12"))      # markets scanned per autonomous cycle
NEWS_OVERLAY = os.getenv("BOT_NEWS_OVERLAY", "1") in ("1", "true", "True", "yes")
# Cross-bot capital rotation lets a strong setup liquidate ANOTHER bot's open
# position. This is the behaviour that made "Tester 3" appear to sell "Tester 1"
# stock, so it is now OFF by default — a bot only ever touches its own position
# unless an operator explicitly opts in.
CAPITAL_ROTATION_ENABLED = os.getenv("BOT_CAPITAL_ROTATION", "0") in ("1", "true", "True", "yes")
SCATTERSHOT_LEG_COUNT = int(os.getenv("BOT_SCATTERSHOT_LEG_COUNT", "5"))
SCATTERSHOT_LEG_NOTIONAL = float(os.getenv("BOT_SCATTERSHOT_LEG_NOTIONAL", "1.0"))
SCATTERSHOT_OPEN_WINDOW_MIN = int(os.getenv("BOT_SCATTERSHOT_OPEN_WINDOW_MIN", "45"))
SCATTERSHOT_EOD_WINDOW_MIN = int(os.getenv("BOT_SCATTERSHOT_EOD_WINDOW_MIN", "20"))
MICRO_EOD_WINDOW_MIN = int(os.getenv("BOT_MICRO_EOD_WINDOW_MIN", "20"))
MIN_SWING_HOLD_DAYS = int(os.getenv("BOT_MIN_SWING_HOLD_DAYS", "3"))


# ────────────────────────────────────────────────────────────────────
# Realtime event emission (best-effort; never breaks the trading loop)
# ────────────────────────────────────────────────────────────────────
def _emit(event_type: str, data: dict, user_id: int | None = None) -> None:
    try:
        from app.realtime import bus
        bus.publish(event_type, data, user_id=user_id)
    except Exception as e:  # pragma: no cover - telemetry must never raise
        logger.debug("realtime emit failed (%s): %s", event_type, e)


def _emit_portfolio(user_id: int) -> None:
    _emit("portfolio_update", {"user_id": user_id}, user_id=user_id)


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


def _bot_trading_mode(owner: User, bot: Bot | None = None) -> str:
    """Prefer the bot's assigned paper/live mode; fall back to the account mode."""
    if bot is not None and (bot.mode or "").strip():
        return (bot.mode or "paper").lower()
    return (owner.trading_mode or "paper").lower()


def _paper(owner: User, bot: Bot | None = None) -> bool:
    return _bot_trading_mode(owner, bot) == "paper"


def _creds(owner: User, broker: str, bot: Bot | None = None) -> dict:
    """Mode-aware broker credentials (paper vs live, with legacy fallback)."""
    return resolve_credentials(owner, broker, _paper(owner, bot))


def _asset_meta(broker: str, symbol: str) -> tuple[str, str]:
    """Return (display name, asset_class) for a symbol from the universe."""
    cfg = MARKET_UNIVERSE.get((broker or "alpaca").lower(), {})
    for item in cfg.get("items", []):
        if item["symbol"].upper() == (symbol or "").upper():
            return item.get("name", symbol), cfg.get("asset_class", "Equity")
    return symbol, cfg.get("asset_class", "Equity")


# ────────────────────────────────────────────────────────────────────
# Buying power lookup (broker-aware, never raises)
# ────────────────────────────────────────────────────────────────────
def _get_buying_power(owner: User, broker: str, bot: Bot | None = None) -> float | None:
    """Return available cash/buying power, or None if it can't be determined."""
    try:
        info = get_account_info(broker=broker, paper=_paper(owner, bot), **_creds(owner, broker, bot))
        if broker == "alpaca":
            return float(info.get("buying_power", 0.0))
        balances = info.get("balances", {})
        return float(balances.get("USDT", balances.get("USD", 0.0)))
    except Exception as e:
        logger.warning("Buying power lookup failed (%s): %s", broker, e)
        return None


def _classify_alpaca_account(info: dict) -> str:
    """
    Alpaca accounts are all margin-enabled, but multiplier=1 (<$2k equity) behaves
    like a cash/limited-margin account for GFV-safe strategy enforcement.
    """
    multiplier = info.get("multiplier")
    try:
        mult = int(float(multiplier)) if multiplier is not None else None
    except (TypeError, ValueError):
        mult = None
    if mult == 1:
        return "cash"
    if mult is not None and mult >= 2:
        return "margin"
    equity = info.get("equity")
    try:
        equity_value = float(equity) if equity is not None else None
    except (TypeError, ValueError):
        equity_value = None
    if equity_value is not None:
        return "margin" if equity_value >= 2000 else "cash"
    return "cash"


def _resolve_non_marginable_buying_power(info: dict, account_type: str) -> float | None:
    """Best-effort settled/non-marginable buying power for GFV guards."""
    for key in ("non_marginable_buying_power", "buying_power", "cash"):
        raw = info.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _get_alpaca_account_context(owner: User, broker: str, bot: Bot | None = None) -> dict:
    """Inspect Alpaca account metadata needed for cash-account GFV protections."""
    if (broker or "alpaca").lower() != "alpaca":
        return {"account_type": "margin", "equity": None, "non_marginable_buying_power": None, "multiplier": None}
    try:
        info = get_account_info(broker=broker, paper=_paper(owner, bot), **_creds(owner, broker, bot))
        if info.get("error"):
            # Lookup failed (bad keys / broker down). Create-time GFV checks only
            # act on account_type=="cash", so "unknown" skips them. Trading-time
            # strategy guards treat "unknown" as fail-closed for cash strategies.
            return {"account_type": "unknown", "equity": None, "non_marginable_buying_power": None, "multiplier": None}
        equity = info.get("equity")
        non_marginable = info.get("non_marginable_buying_power")
        multiplier = info.get("multiplier")
        try:
            equity_value = float(equity) if equity is not None else None
        except (TypeError, ValueError):
            equity_value = None
        try:
            non_marginable_value = float(non_marginable) if non_marginable is not None else None
        except (TypeError, ValueError):
            non_marginable_value = None
        # Alpaca cash accounts report multiplier "1"; margin is "2" or "4".
        try:
            mult_value = float(multiplier) if multiplier is not None else None
        except (TypeError, ValueError):
            mult_value = None
        if mult_value is not None:
            account_type = "cash" if mult_value <= 1 else "margin"
        else:
            # Fallback only when multiplier is unavailable — do not treat equity
            # size as a proxy for margin eligibility.
            account_type = "cash"
        return {
            "account_type": account_type,
            "equity": equity_value,
            "non_marginable_buying_power": non_marginable_value,
            "multiplier": mult_value,
        }
    except Exception as e:
        logger.warning("Alpaca account context lookup failed: %s", e)
        return {"account_type": "unknown", "equity": None, "non_marginable_buying_power": None, "multiplier": None}


def _strategy_allows_cash_guard(bot: Bot) -> bool:
    strategy = (bot.low_balance_strategy or "standard").lower()
    return strategy in {"one_shot_daily", "micro_trader", "swing_trader", "scattershot"}


def _strategy_label(strategy: str | None) -> str:
    mapping = {
        "standard": "Standard",
        "one_shot_daily": "One-Shot Daily",
        "micro_trader": "Micro-Trader",
        "swing_trader": "Swing Trader",
        "scattershot": "Scattershot",
    }
    return mapping.get((strategy or "standard").lower(), "Standard")


def _strategy_tooltip(strategy: str | None) -> str:
    mapping = {
        "standard": "Uses the standard allocation logic without low-balance adjustments.",
        "one_shot_daily": "Uses 100% of your allocated funds for a single high-confidence trade today. Halts trading after selling until funds settle tomorrow.",
        "micro_trader": "Executes multiple small day trades ($1.00 each) on a single stock to capture small movements without spending unsettled cash.",
        "swing_trader": "Buys a stock and holds it for several days or weeks to ride larger trends. Safely avoids daily cash settlement rules.",
        "scattershot": "Diversifies your risk by buying $1.00 of 5 different stocks simultaneously at the market open, selling them before the close.",
    }
    return mapping.get((strategy or "standard").lower(), mapping["standard"])


def _strategy_name(bot: Bot) -> str:
    return (bot.low_balance_strategy or "standard").lower()


def _load_strategy_state(bot: Bot) -> dict:
    raw = bot.strategy_state
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _save_strategy_state(bot: Bot, state: dict | None) -> None:
    bot.strategy_state = json.dumps(state) if state else None


def _scattershot_active(bot: Bot) -> bool:
    state = _load_strategy_state(bot)
    return bool(state.get("legs"))


def bot_needs_liquidation(bot: Bot) -> bool:
    """
    True when delete/manual-sell must call liquidate_bot before treating the bot
    as flat. Covers scattershot baskets whose exposure lives in strategy_state
    even when shares_held looks empty.
    """
    if _strategy_name(bot) == "scattershot" and (
        _scattershot_active(bot) or bot.in_position or "," in (bot.ticker or "")
    ):
        return True
    return bool(bot.in_position and (bot.shares_held or 0) > 0)


def _swing_hold_met(bot: Bot) -> bool:
    if not bot.position_opened_at:
        return True
    held_days = (datetime.utcnow() - bot.position_opened_at).total_seconds() / 86400.0
    return held_days >= MIN_SWING_HOLD_DAYS


def _requires_same_day_exit(bot: Bot) -> bool:
    if _strategy_name(bot) != "micro_trader" or not bot.in_position:
        return False
    if is_eod_exit_window(MICRO_EOD_WINDOW_MIN):
        return True
    if bot.position_opened_at and session_date_et() != session_date_et(bot.position_opened_at):
        return True
    return False


def _cooldown_active(bot: Bot) -> bool:
    return bool(bot.strategy_cooldown_until and bot.strategy_cooldown_until > datetime.utcnow())


def _set_next_session_cooldown(bot: Bot) -> None:
    nxt = next_market_open()
    bot.strategy_cooldown_until = datetime.utcfromtimestamp(nxt.timestamp())


def _enforce_low_balance_strategy(db: Session, owner: User, bot: Bot, price: float, analysis: Analysis | None) -> tuple[bool, dict]:
    """Apply GFV-safe Low-balance strategy rules for cash accounts and allow margin accounts to opt in voluntarily."""
    account_context = _get_alpaca_account_context(owner, bot.broker or "alpaca", bot=bot)
    account_type = account_context.get("account_type", "cash")
    non_marginable = account_context.get("non_marginable_buying_power")
    strategy = (bot.low_balance_strategy or "standard").lower()

    if strategy == "standard" or not _strategy_allows_cash_guard(bot):
        return True, {"account_type": account_type, "reason": "standard"}

    # Broker metadata unavailable — fail closed for cash-sensitive strategies
    # rather than treating the account as margin and skipping GFV gates.
    if account_type == "unknown":
        return False, {"account_type": account_type, "reason": "account metadata unavailable"}

    # Cash accounts must respect unsettled-funds limits; margin can opt into the
    # same sizing rules without the non-marginable buying-power gate.
    is_cash = account_type == "cash"

    if strategy == "one_shot_daily":
        if bot.strategy_cooldown_until and bot.strategy_cooldown_until > datetime.utcnow():
            return False, {"account_type": account_type, "reason": "cooldown active"}
        if is_cash:
            if non_marginable is None:
                return False, {"account_type": account_type, "reason": "cash-account metadata unavailable"}
            if non_marginable <= 0:
                return False, {"account_type": account_type, "reason": "no non-marginable buying power"}
            return True, {
                "account_type": account_type,
                "reason": "one-shot allowed",
                "notional": min(float(bot.funds_allocated or 0), non_marginable),
            }
        return True, {
            "account_type": account_type,
            "reason": "one-shot allowed",
            "notional": float(bot.funds_allocated or 0),
        }

    if strategy == "micro_trader":
        if is_cash:
            if non_marginable is None or non_marginable <= 0:
                return False, {"account_type": account_type, "reason": "no non-marginable buying power"}
        return True, {"account_type": account_type, "reason": "micro trader", "notional": 1.0}

    if strategy == "swing_trader":
        return True, {"account_type": account_type, "reason": "swing holds overnight"}

    if strategy == "scattershot":
        if is_cash:
            if non_marginable is None or non_marginable <= 0:
                return False, {"account_type": account_type, "reason": "no non-marginable buying power"}
        return True, {"account_type": account_type, "reason": "scattershot", "notional": 1.0}

    return True, {"account_type": account_type, "reason": "standard"}


def _resolve_leg_exit_price(db: Session, owner: User, bot: Bot, symbol: str) -> float:
    broker = bot.broker or owner.active_broker or "alpaca"
    try:
        from app.market_store import get_quote
        q = get_quote(db, broker, symbol)
        if q and q.price:
            return float(q.price)
    except Exception:
        pass
    a = _safe_analysis(bot, owner, symbol=symbol)
    if a and a.last_price:
        return float(a.last_price)
    return 0.0


def _pick_scattershot_symbols(db: Session, owner: User, bot: Bot, count: int) -> list[str]:
    """Pick diversified symbols for a scattershot basket near the open."""
    broker = bot.broker or owner.active_broker or "alpaca"
    items = MARKET_UNIVERSE.get(broker.lower(), {}).get("items", [])
    if not items:
        return []

    scan_n = min(max(AUTO_SCAN_LIMIT * 2, count * 3), len(items))
    ranked: list[tuple[float, str]] = []
    seen: set[str] = set()
    for item in items[:scan_n]:
        sym = item["symbol"]
        analysis = _safe_analysis(bot, owner, symbol=sym)
        if analysis is None:
            continue
        sig = analysis.signal
        blocked, _ = _market_collision_blocked(db, owner, bot, sym, _setup_quality_score(analysis))
        if blocked:
            continue
        score = _setup_quality_score(analysis)
        if sig.action == "BUY" and sig.strength >= ENTRY_MIN_STRENGTH * 0.85:
            ranked.append((score + sig.strength, sym))
            seen.add(sym)

    ranked.sort(key=lambda x: x[0], reverse=True)
    picks = [sym for _, sym in ranked[:count]]
    if len(picks) >= count:
        return picks[:count]

    for item in items:
        sym = item["symbol"]
        if sym in seen or sym in picks:
            continue
        blocked, _ = _market_collision_blocked(db, owner, bot, sym, 0.5)
        if blocked:
            continue
        picks.append(sym)
        if len(picks) >= count:
            break
    return picks[:count]


def _close_scattershot_basket(db: Session, owner: User, bot: Bot, reason: str) -> dict:
    """Liquidate every leg in an open scattershot basket and reset bot state."""
    state = _load_strategy_state(bot)
    legs = list(state.get("legs") or [])
    if not legs:
        # Legs missing but ticker/position flags still set — attempt broker
        # unwind from the comma-joined ticker before clearing local state.
        orphan_syms = [s.strip().upper() for s in (bot.ticker or "").split(",") if s.strip()]
        if orphan_syms and (bot.in_position or bot.shares_held or "," in (bot.ticker or "")):
            failed: list[str] = []
            for sym in orphan_syms:
                try:
                    _liquidate(owner, bot.broker, sym, bot=bot)
                    # no_position is fine here: legs state is already empty, so
                    # broker-flat means there is nothing left to unwind.
                except Exception as e:
                    failed.append(sym)
                    _log(db, owner.id, f"[SCATTER-SHOT] Orphan liquidate {sym} failed: {e}", "ERROR")
            if failed:
                bot.last_pattern_summary = (
                    f"Scattershot orphan unwind incomplete — failed: {', '.join(failed)}"
                )
                db.commit()
                return {
                    "action": "WAIT",
                    "reason": "Scattershot orphan unwind incomplete.",
                    "failed": failed,
                }
        if bot.in_position or bot.shares_held or bot.strategy_state:
            bot.in_position = False
            bot.shares_held = 0
            bot.avg_entry_price = None
            bot.peak_price = None
            bot.stop_price = None
            bot.take_profit_price = None
            bot.position_opened_at = None
            _save_strategy_state(bot, None)
            if bot.auto_select:
                bot.ticker = None
            bot.last_pattern_summary = "Scattershot basket already flat (no legs to close)."
            db.commit()
        return {"action": "FLAT", "reason": "No scattershot legs to close."}

    total_gain = 0.0
    total_notional = 0.0
    closed_symbols: list[str] = []
    failed_symbols: list[str] = []
    remaining_legs: list[dict] = []
    for leg in legs:
        sym = leg.get("ticker")
        qty = float(leg.get("qty") or 0)
        entry = float(leg.get("entry_price") or 0)
        if not sym or qty <= 0:
            continue
        price = _resolve_leg_exit_price(db, owner, bot, sym) or entry
        try:
            order = _liquidate(owner, bot.broker, sym, bot=bot)
        except Exception as e:
            failed_symbols.append(sym)
            remaining_legs.append(leg)
            _log(db, owner.id, f"[SCATTER-SHOT] SELL {sym} failed: {e}", "ERROR")
            continue
        if (order or {}).get("status") == "no_position" and qty > 0:
            # Align with single-leg _close_position: fail closed so a transient
            # broker "flat" response cannot wipe a tracked leg.
            failed_symbols.append(sym)
            remaining_legs.append(leg)
            _log(db, owner.id,
                 f"[SCATTER-SHOT] SELL {sym} reported no_position while leg qty={qty} "
                 f"— refusing to clear leg.", "ERROR")
            continue
        gain = (price - entry) * qty
        notional = round(qty * price, 6)
        total_gain += gain
        total_notional += notional
        closed_symbols.append(sym)
        db.add(Trade(owner_id=owner.id, bot_id=bot.id, bot_uuid=bot.uuid, ticker=sym,
                     side="sell", qty=qty, notional=notional, price=price,
                     broker=bot.broker, mode=_bot_trading_mode(owner, bot),
                     broker_order_id=order.get("order_id"), status=order.get("status")))
        _log(db, owner.id,
             f"[SCATTER-SHOT] SELL {qty:.6f} {sym} @ {price:.4f} ({reason}). Leg P&L: {gain:+.2f}.",
             "WARNING" if gain < 0 else "INFO")

    if failed_symbols:
        # Keep remaining legs in strategy state; do not mark the whole basket flat.
        state = _load_strategy_state(bot) or {}
        state["legs"] = remaining_legs
        _save_strategy_state(bot, state)
        bot.last_pattern_summary = (
            f"Scattershot partial exit ({reason}) — failed: {', '.join(failed_symbols)}"
        )
        if closed_symbols:
            bot.realized_pnl = (bot.realized_pnl or 0) + total_gain
            bot.trade_count = (bot.trade_count or 0) + len(closed_symbols)
        db.commit()
        _emit_portfolio(owner.id)
        return {
            "action": "WAIT",
            "reason": "Partial scattershot exit — some legs still open.",
            "closed": closed_symbols,
            "failed": failed_symbols,
        }

    bot.realized_pnl = (bot.realized_pnl or 0) + total_gain
    bot.trade_count = (bot.trade_count or 0) + len(closed_symbols)
    bot.in_position = False
    bot.shares_held = 0
    bot.avg_entry_price = None
    bot.peak_price = None
    bot.stop_price = None
    bot.take_profit_price = None
    bot.position_opened_at = None
    _save_strategy_state(bot, None)
    _set_next_session_cooldown(bot)
    if bot.auto_select:
        bot.ticker = None
    else:
        bot.ticker = None
    bot.last_pattern_summary = (
        f"Scattershot basket closed ({reason}) — {len(closed_symbols)} legs, "
        f"P&L {total_gain:+.2f}. Waiting for next session open."
    )
    db.commit()
    _emit("trade", {
        "bot_id": bot.id, "bot_uuid": bot.uuid, "bot_name": bot.name,
        "ticker": ",".join(closed_symbols), "side": "sell",
        "qty": None, "notional": round(total_notional, 4), "price": None,
        "realized_pnl": round(total_gain, 4), "reason": reason,
    }, user_id=owner.id)
    _emit_portfolio(owner.id)
    return {
        "order": {"status": "closed_basket", "symbols": closed_symbols},
        "realized_gain": round(total_gain, 4),
        "legs_closed": len(closed_symbols),
    }


def _open_scattershot_basket(db: Session, owner: User, bot: Bot) -> dict:
    """Deploy a $1 × N scattershot basket during the market-open window."""
    symbols = _pick_scattershot_symbols(db, owner, bot, SCATTERSHOT_LEG_COUNT)
    if len(symbols) < SCATTERSHOT_LEG_COUNT:
        bot.last_pattern_summary = (
            f"Scattershot waiting — only found {len(symbols)}/{SCATTERSHOT_LEG_COUNT} "
            "available symbols without collisions."
        )
        db.commit()
        return {"action": "WAIT", "reason": "Insufficient symbols for scattershot basket."}

    allowed, guard = _enforce_low_balance_strategy(db, owner, bot, 0.0, None)
    if not allowed:
        bot.last_pattern_summary = f"Scattershot entry blocked ({guard.get('reason')})."
        db.commit()
        return {"action": "WAIT", "reason": guard.get("reason"), "guard": guard}

    legs: list[dict] = []
    total_notional = 0.0
    total_qty = 0.0
    for sym in symbols:
        analysis = _safe_analysis(bot, owner, symbol=sym)
        price = analysis.last_price if analysis else None
        if not price or price <= 0:
            continue
        notional = SCATTERSHOT_LEG_NOTIONAL
        try:
            order = _execute(owner, bot.broker, "buy", sym, notional=notional, bot=bot)
        except BrokerError as e:
            _log(db, owner.id, f"[SCATTER-SHOT] BUY {sym} failed: {e}")
            continue
        if order.get("simulated") and not _paper(owner, bot):
            continue
        qty = round(notional / price, 8)
        legs.append({
            "ticker": sym,
            "qty": qty,
            "entry_price": price,
            "notional": notional,
            "order_id": order.get("order_id"),
        })
        total_notional += notional
        total_qty += qty
        db.add(Trade(owner_id=owner.id, bot_id=bot.id, bot_uuid=bot.uuid, ticker=sym,
                     side="buy", qty=qty, notional=notional, price=price,
                     broker=bot.broker, mode=_bot_trading_mode(owner, bot),
                     broker_order_id=order["order_id"], status=order["status"]))
        _log(db, owner.id,
             f"[SCATTER-SHOT] BUY ${notional:.2f} ({qty:.6f}) {sym} @ {price:.4f}.")

    if len(legs) < SCATTERSHOT_LEG_COUNT:
        if legs:
            # Persist partial fills then immediately unwind so broker shares
            # are never left untracked / unmanaged.
            _save_strategy_state(bot, {
                "type": "scattershot",
                "session_date": session_date_et(),
                "legs": legs,
            })
            bot.in_position = True
            bot.ticker = ",".join(leg["ticker"] for leg in legs)
            bot.shares_held = round(total_qty, 8)
            bot.avg_entry_price = round(total_notional / total_qty, 6) if total_qty else None
            closed = _close_scattershot_basket(
                db, owner, bot, reason="incomplete scattershot deploy"
            )
            bot.last_pattern_summary = (
                f"Scattershot deploy incomplete — only {len(legs)}/{SCATTERSHOT_LEG_COUNT} "
                f"legs filled; unwound {closed.get('legs_closed', 0)} partial fill(s)."
            )
            db.commit()
            return {
                "action": "WAIT",
                "reason": "Scattershot basket could not be fully deployed.",
                **closed,
            }
        bot.last_pattern_summary = (
            f"Scattershot deploy incomplete — only {len(legs)}/{SCATTERSHOT_LEG_COUNT} legs filled."
        )
        db.commit()
        return {"action": "WAIT", "reason": "Scattershot basket could not be fully deployed."}

    state = {
        "type": "scattershot",
        "session_date": session_date_et(),
        "legs": legs,
    }
    _save_strategy_state(bot, state)
    bot.in_position = True
    bot.ticker = ",".join(leg["ticker"] for leg in legs)
    bot.shares_held = round(total_qty, 8)
    bot.avg_entry_price = round(total_notional / total_qty, 6) if total_qty else None
    bot.position_opened_at = datetime.utcnow()
    bot.trade_count = (bot.trade_count or 0) + len(legs)
    bot.last_signal = "BUY"
    bot.last_analysis_at = datetime.utcnow()
    bot.last_pattern_summary = (
        f"Scattershot basket live — {len(legs)} legs ({bot.ticker}) for ${total_notional:.2f}. "
        "Will exit before the close."
    )
    db.commit()
    _emit_portfolio(owner.id)
    return {
        "action": "BUY",
        "reason": "Scattershot basket deployed at the open.",
        "legs": legs,
        "notional": round(total_notional, 2),
    }


def _run_scattershot_cycle(db: Session, bot: Bot) -> dict:
    """Dedicated cycle for the scattershot low-balance strategy."""
    owner = bot.owner
    bot.last_analysis_at = datetime.utcnow()

    if not bot.running:
        db.commit()
        return {"action": "SKIPPED", "reason": "Bot is paused."}

    state = _load_strategy_state(bot)
    legs = state.get("legs") or []
    session_day = state.get("session_date")

    # Corrupt/partial state: marked in position but no legs to manage — attempt
    # broker liquidations from the joined ticker (if any), then clear local state
    # so we never open a duplicate basket on top of orphan holdings.
    if bot.in_position and not legs:
        symbols = [p.strip() for p in (bot.ticker or "").split(",") if p.strip()]
        failed: list[str] = []
        for sym in symbols:
            try:
                _liquidate(owner, bot.broker, sym, bot=bot)
                _log(db, owner.id, f"[SCATTER-SHOT] Reconcile liquidate {sym}.", "WARNING")
            except Exception as e:
                failed.append(sym)
                _log(db, owner.id, f"[SCATTER-SHOT] Reconcile liquidate {sym} failed: {e}", "ERROR")
        # Fail closed: do not clear local state while broker liquidations failed.
        if failed:
            bot.last_pattern_summary = (
                "Scattershot reconcile incomplete — still holding "
                + ", ".join(failed)
                + ". Will retry."
            )
            db.commit()
            return {
                "action": "WAIT",
                "reason": "Scattershot reconcile could not liquidate all orphan symbols.",
                "failed": failed,
            }
        bot.in_position = False
        bot.shares_held = 0
        bot.avg_entry_price = None
        bot.peak_price = None
        bot.stop_price = None
        bot.take_profit_price = None
        bot.position_opened_at = None
        _save_strategy_state(bot, None)
        if bot.auto_select:
            bot.ticker = None
        bot.last_pattern_summary = (
            "Scattershot reconciled — cleared stuck in-position state"
            + (f" (attempted {', '.join(symbols)})" if symbols else "")
            + "."
        )
        db.commit()
        _emit_portfolio(owner.id)
        return {
            "action": "SELL" if symbols else "FLAT",
            "reason": "Reconciled scattershot without leg state.",
            "symbols": symbols,
        }

    if legs:
        if session_day and session_day != session_date_et():
            closed = _close_scattershot_basket(db, owner, bot, reason="stale overnight basket")
            return {"action": "SELL", "reason": "Closed stale scattershot basket.", **closed}
        mins_left = None
        try:
            from app.market_hours import minutes_until_close
            mins_left = minutes_until_close()
        except Exception:
            pass
        if is_eod_exit_window(SCATTERSHOT_EOD_WINDOW_MIN):
            closed = _close_scattershot_basket(db, owner, bot, reason="scattershot end-of-day exit")
            return {"action": "SELL", "reason": "Scattershot end-of-day exit.", **closed}
        tickers = ", ".join(leg.get("ticker", "?") for leg in legs)
        bot.last_pattern_summary = (
            f"Scattershot holding {len(legs)} legs ({tickers}). "
            f"{'Exiting before close.' if mins_left is not None and mins_left <= SCATTERSHOT_EOD_WINDOW_MIN + 5 else 'Monitoring until the close window.'}"
        )
        db.commit()
        return {"action": "HOLD", "reason": bot.last_pattern_summary, "legs": legs}

    if _cooldown_active(bot):
        bot.last_pattern_summary = "Scattershot cooldown active — waiting for the next session open."
        db.commit()
        return {"action": "WAIT", "reason": "Scattershot cooldown active."}

    if not is_open_entry_window(SCATTERSHOT_OPEN_WINDOW_MIN):
        bot.last_pattern_summary = (
            "Scattershot standing by — deploys $1 × 5 legs during the first "
            f"{SCATTERSHOT_OPEN_WINDOW_MIN} minutes after the open."
        )
        db.commit()
        return {"action": "WAIT", "reason": "Outside scattershot open window."}

    return _open_scattershot_basket(db, owner, bot)


# ────────────────────────────────────────────────────────────────────
# Order helper — real order if keys present, otherwise a logged paper sim
# ────────────────────────────────────────────────────────────────────
def _execute(owner: User, broker: str, side: str, symbol: str,
             qty: float = None, notional: float = None, bot: Bot | None = None) -> dict:
    paper = _paper(owner, bot)
    try:
        order = place_order(
            broker=broker, side=side, symbol=symbol, qty=qty, notional=notional,
            paper=paper, **_creds(owner, broker, bot),
        )
        order["simulated"] = False
        return order
    except BrokerError as e:
        # Live mode: never invent a fill — DB would diverge from the broker.
        if not paper:
            logger.error("[LIVE] %s %s %s FAILED (no simulated fill): %s", side, symbol, broker, e)
            raise
        # Paper mode only: record a simulated fill so strategies stay observable
        # without broker keys during local/dev use.
        logger.warning("[SIM] %s %s %s simulated (broker said: %s)", side, symbol, broker, e)
        return {"order_id": f"SIM-{datetime.utcnow().timestamp():.0f}",
                "status": "simulated", "symbol": symbol, "side": side, "simulated": True}


def _liquidate(owner: User, broker: str, symbol: str, bot: Bot | None = None) -> dict:
    """
    Robustly exit a position via the broker: cancel the symbol's open orders,
    wait for them to clear, then liquidate the full real quantity. Used for ALL
    position exits (stops, take-profit, structural sells, rotation, manual sell,
    delete) so reserved shares can never trigger an "insufficient qty" error.
    Falls back to a simulated fill when keys are missing.
    """
    has_keys = has_credentials(owner, (broker or "alpaca"), _paper(owner, bot))
    try:
        order = liquidate_position(broker=broker, symbol=symbol,
                                   paper=_paper(owner, bot), **_creds(owner, broker, bot))
        order["simulated"] = False
        return order
    except BrokerError as e:
        if has_keys:
            # Real account with real keys: never fake a successful exit — a
            # silent "simulated" fill would mark the position closed in our DB
            # while the shares are still sitting at the broker. Surface it so
            # the caller (stop-loss, manual sell, delete) fails safe instead.
            logger.error("[LIQUIDATE] %s %s FAILED with live keys: %s", symbol, broker, e)
            raise
        logger.warning("[SIM] liquidate %s %s simulated (broker said: %s)", symbol, broker, e)
        return {"order_id": f"SIM-{datetime.utcnow().timestamp():.0f}",
                "status": "simulated", "symbol": symbol, "side": "sell", "simulated": True}


# ────────────────────────────────────────────────────────────────────
# Risk-management arming / ratcheting
# ────────────────────────────────────────────────────────────────────
def _arm_risk(bot: Bot, entry_price: float, atr: float | None,
              nearest_resistance: float | None):
    """Set the initial stop-loss and take-profit on entry."""
    bot.peak_price = entry_price

    if EXIT_MODE == "atr":
        atr = atr or (entry_price * TRAIL_PCT_FLOOR)
        trail_dist = max(atr * TRAIL_ATR_MULT, entry_price * TRAIL_PCT_FLOOR)
        bot.stop_price = round(entry_price - trail_dist, 6)

        tp_atr = entry_price + atr * TP_ATR_MULT
        # If resistance is overhead, aim just under it; otherwise use the ATR target.
        if nearest_resistance and nearest_resistance > entry_price:
            bot.take_profit_price = round(min(tp_atr, nearest_resistance * 0.998), 6)
        else:
            bot.take_profit_price = round(tp_atr, 6)
        return

    stop_pct, take_pct = _get_exit_percentages()
    stop_dist = max(entry_price * stop_pct, 0.01)
    target_dist = max(entry_price * take_pct, 0.01)
    bot.stop_price = round(entry_price - stop_dist, 6)
    bot.take_profit_price = round(entry_price + target_dist, 6)


def _ratchet_risk(bot: Bot, price: float, atr: float | None, bullish: bool):
    """Manage exits for ATR mode or fixed-percentage mode."""
    new_high = bot.peak_price is None or price > bot.peak_price
    if new_high:
        bot.peak_price = price

    # Fixed-percentage mode keeps the stop/target armed at entry.
    if EXIT_MODE != "atr" or not new_high:
        return

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
        ).all()
        if b.id != candidate_bot.id
        and b.avg_entry_price
        # Scattershot baskets need the multi-leg closer; never rotate them via
        # single-symbol liquidation (comma-joined ticker breaks the broker call).
        and _strategy_name(b) != "scattershot"
        and "," not in (b.ticker or "")
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

    try:
        _close_position(db, owner, worst_bot, worst_price, reason="capital rotation")
    except BrokerError as e:
        _log(db, owner.id, f"[ROTATION] Close failed for '{worst_bot.name}': {e}", "ERROR")
        return False
    except Exception as e:
        _log(db, owner.id, f"[ROTATION] Unexpected close failure for '{worst_bot.name}': {e}", "ERROR")
        return False
    return True


# ────────────────────────────────────────────────────────────────────
# Position close
# ────────────────────────────────────────────────────────────────────
def _close_position(db: Session, owner: User, bot: Bot, price: float, reason: str) -> dict:
    qty = bot.shares_held or 0
    ticker = bot.ticker
    # Cancel-then-close: never a naive fixed-qty sell, so open orders reserving
    # shares can't cause an "insufficient quantity" rejection.
    order = _liquidate(owner, bot.broker, ticker, bot=bot)
    if (order or {}).get("status") == "no_position" and qty > 0:
        # Broker says flat but we thought we held shares — fail closed so we
        # don't write a fake SELL and wipe local state while unsure.
        raise BrokerError(
            f"Liquidation of {ticker} reported no_position while bot still tracked "
            f"{qty} shares — refusing to clear local state."
        )
    gain = (price - (bot.avg_entry_price or price)) * qty
    notional = round(qty * price, 6)
    bot.realized_pnl = (bot.realized_pnl or 0) + gain
    bot.trade_count = (bot.trade_count or 0) + 1
    # ACID: the ledger row, attribution and bot state mutate inside one
    # transaction committed atomically below — every sell records the exact
    # quantity, price, timestamp and an immutable link to THIS bot.
    db.add(Trade(owner_id=owner.id, bot_id=bot.id, bot_uuid=bot.uuid, ticker=ticker,
                 side="sell", qty=qty, notional=notional, price=price,
                 broker=bot.broker, mode=_bot_trading_mode(owner, bot),
                 broker_order_id=order["order_id"], status=order["status"]))
    _log(db, owner.id,
         f"[EXECUTE] SELL {qty:.6f} {ticker} @ {price:.4f} ({reason}). "
         f"Realized P&L: {gain:+.2f}.",
         "WARNING" if gain < 0 else "INFO")
    bot.in_position = False
    bot.shares_held = 0
    bot.avg_entry_price = None
    bot.peak_price = None
    bot.stop_price = None
    bot.take_profit_price = None
    bot.position_opened_at = None
    _save_strategy_state(bot, None)
    if (bot.low_balance_strategy or "standard").lower() == "one_shot_daily":
        bot.strategy_cooldown_until = datetime.utcnow() + timedelta(hours=24)
    # Fully autonomous bots release the ticker so the next cycle re-scans the
    # whole market for the freshest dip rather than re-buying the same asset.
    if bot.auto_select:
        _log(db, owner.id, f"[AUTO] '{bot.name}' released {ticker} — will re-scan all markets next cycle.")
        bot.ticker = None
    db.commit()
    _emit("trade", {
        "bot_id": bot.id, "bot_uuid": bot.uuid, "bot_name": bot.name,
        "ticker": ticker, "side": "sell", "qty": round(qty, 8),
        "notional": notional, "price": price, "realized_pnl": round(gain, 4),
        "reason": reason,
    }, user_id=owner.id)
    _emit_portfolio(owner.id)
    return {"order": order, "realized_gain": round(gain, 4)}


# ────────────────────────────────────────────────────────────────────
# Analysis fetch wrapper
# ────────────────────────────────────────────────────────────────────
def _safe_analysis(bot: Bot, owner: User, symbol: str = None) -> Analysis | None:
    broker = bot.broker or owner.active_broker or "alpaca"
    sym = symbol or bot.ticker
    if not sym:
        return None
    try:
        return get_market_analysis(
            broker=broker, symbol=sym, timeframe=bot.timeframe or "1h", limit=CANDLE_LIMIT,
            paper=_paper(owner, bot), **_creds(owner, broker, bot),
        )
    except BrokerError as e:
        logger.warning("Analysis fetch failed for bot %s (%s): %s", bot.id, sym, e)
        return None


# ────────────────────────────────────────────────────────────────────
# News sanity overlay (NOT the primary driver — just a veto on strong bad news)
# ────────────────────────────────────────────────────────────────────
def _news_sanity(broker: str, symbol: str) -> dict:
    """Return the news verdict. Always safe — failures degrade to neutral."""
    if not NEWS_OVERLAY:
        return {"label": "neutral", "score": 0.0, "veto": False,
                "note": "News overlay disabled.", "headlines": []}
    try:
        name, asset_class = _asset_meta(broker, symbol)
        return get_asset_sentiment(symbol, name, asset_class)
    except Exception as e:
        logger.debug("News sanity failed for %s: %s", symbol, e)
        return {"label": "neutral", "score": 0.0, "veto": False,
                "note": f"News check skipped ({e}).", "headlines": []}


def _passes_conservative_entry_filter(analysis: Analysis) -> bool:
    """Require a stronger bullish setup before opening a new position."""
    if not CONSERVATIVE_ENTRY_FILTER:
        return True

    sig = analysis.signal
    if sig.action != "BUY" or sig.strength < ENTRY_MIN_STRENGTH:
        return False

    bullish_patterns = [p for p in (analysis.patterns or []) if p.get("bias") == "bullish"]
    if not bullish_patterns:
        return False

    trend = (analysis.indicators or {}).get("trend") or "neutral"
    if trend == "down":
        return False
    if trend == "up":
        return True

    # Neutral trend needs more confirmation; require at least two bullish patterns.
    return len(bullish_patterns) >= 2


def _passes_trend_confirmation_filter(analysis: Analysis) -> bool:
    """Require a bullish trend backdrop before entry."""
    if not TREND_CONFIRMATION_FILTER:
        return True

    indicators = analysis.indicators or {}
    trend = indicators.get("trend") or "neutral"
    if trend != "up":
        return False

    price = analysis.last_price or 0.0
    sma20 = indicators.get("sma20")
    sma50 = indicators.get("sma50")
    ema12 = indicators.get("ema12")
    ema26 = indicators.get("ema26")
    macd = indicators.get("macd")
    macd_signal = indicators.get("macd_signal")

    if price and sma20 is not None and sma50 is not None and sma20 > sma50:
        return True
    if ema12 is not None and ema26 is not None and ema12 > ema26:
        return True
    if macd is not None and macd_signal is not None and macd > macd_signal:
        return True
    return False


def _setup_quality_score(analysis: Analysis) -> float:
    """Score a setup from 0.0 to 1.0 so only high-quality setups get entered."""
    sig = analysis.signal
    score = max(0.0, min(1.0, float(sig.strength or 0.0)))

    bullish_patterns = [p for p in (analysis.patterns or []) if p.get("bias") == "bullish"]
    score += min(0.30, 0.10 * len(bullish_patterns))

    trend = (analysis.indicators or {}).get("trend") or "neutral"
    if trend == "up":
        score += 0.10
    elif trend == "down":
        score -= 0.20

    rsi = (analysis.indicators or {}).get("rsi")
    if rsi is not None:
        if rsi <= 35:
            score += 0.08
        elif rsi >= 70:
            score -= 0.12

    price = analysis.last_price or 0.0
    nearest_resistance = (analysis.levels or {}).get("nearest_resistance")
    if price and nearest_resistance and nearest_resistance > 0:
        dist_pct = (nearest_resistance - price) / price * 100 if price else 0.0
        if 0 <= dist_pct <= 1.5:
            score -= 0.08

    return max(0.0, min(1.0, score))


def _passes_quality_setup_filter(analysis: Analysis) -> bool:
    """Use a blended quality score to gate entry quality."""
    if not QUALITY_SETUP_SCORING:
        return True
    return _setup_quality_score(analysis) >= MIN_SETUP_QUALITY_SCORE


def _volatility_adjusted_notional(bot: Bot, price: float, base_notional: float, atr: float | None) -> float:
    """Reduce position size when volatility is high so the bot doesn't overexpose itself."""
    if not VOLATILITY_SIZING or not price or price <= 0:
        return base_notional

    atr = atr or 0.0
    risk_distance = max(atr * TRAIL_ATR_MULT, price * TRAIL_PCT_FLOOR)
    if risk_distance <= 0:
        return base_notional

    risk_budget = max(price * RISK_PER_TRADE_PCT, 0.01)
    size_factor = min(1.0, max(0.0, risk_budget / risk_distance))
    adjusted = round(base_notional * size_factor, 2)
    max_notional = round((bot.funds_allocated or 0) * MAX_POSITION_PCT, 2)
    if max_notional > 0:
        adjusted = min(adjusted, max_notional)
    return max(0.0, adjusted)


# ────────────────────────────────────────────────────────────────────
# Fully autonomous market selection
# ────────────────────────────────────────────────────────────────────
def _pick_best_setup(db: Session, owner: User, bot: Bot) -> Analysis | None:
    """
    Scan the active markets for the bot's broker, analyze each chart, and pick
    the strongest CONFIRMED dip/reversal BUY setup. Applies the news overlay as
    a sanity veto only. Returns the chosen Analysis (with .symbol set) or None.
    """
    broker = bot.broker or owner.active_broker or "alpaca"
    cfg = MARKET_UNIVERSE.get(broker.lower(), {})
    items = cfg.get("items", [])
    if not items:
        return None

    scan_n = min(AUTO_SCAN_LIMIT, len(items))
    _log(db, owner.id,
         f"[AUTO-SCAN] Bot '{bot.name}' scanning {scan_n}/{len(items)} {broker} markets for the best dip…")

    candidates = []  # (quality_score, strength, analysis)
    scanned = 0
    for item in items[:scan_n]:
        sym = item["symbol"]
        analysis = _safe_analysis(bot, owner, symbol=sym)
        scanned += 1
        if analysis is None:
            continue
        sig = analysis.signal
        logger.info("[AUTO-SCAN] %s -> %s strength=%.2f", sym, sig.action, sig.strength)
        if sig.action == "BUY" and sig.strength >= ENTRY_MIN_STRENGTH:
            quality_score = _setup_quality_score(analysis)
            blocked, reason = _market_collision_blocked(db, owner, bot, sym, quality_score)
            if blocked:
                _log(db, owner.id, f"[AUTO-SCAN] Skipping {sym} — market occupied by another active bot ({reason}).")
                continue
            candidates.append((quality_score, sig.strength, analysis))

    if not candidates:
        _log(db, owner.id,
             f"[AUTO-SCAN] Scanned {scanned} markets — no confirmed dip/reversal met the "
             f"entry bar (strength >= {ENTRY_MIN_STRENGTH}). Standing by.")
        return None

    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)

    # Walk best-first; news overlay can veto a candidate (sanity check only).
    for quality_score, strength, analysis in candidates:
        verdict = _news_sanity(broker, analysis.symbol)
        if verdict.get("veto"):
            _log(db, owner.id,
                 f"[AUTO-SCAN] Skipping {analysis.symbol} (strength {strength:.2f}) — "
                 f"news sanity VETO: {verdict.get('note')}", "WARNING")
            continue
        _log(db, owner.id,
             f"[AUTO-SCAN] Selected {analysis.symbol} (quality {quality_score:.2f}, strength {strength:.2f}, "
             f"news {verdict.get('label')}): {analysis.signal.headline}")
        return analysis

    _log(db, owner.id, "[AUTO-SCAN] All confirmed setups vetoed by news sanity layer. Standing by.", "WARNING")
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

    # Keep the live market-of-record fresh and stream it to any open dashboards.
    _record_quote(db, bot.broker or owner.active_broker or "alpaca", bot.ticker, analysis)

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
    # Critical: branch on in_position ALONE. The old guard also required a
    # truthy avg_entry_price, so a corrupt position (entry price None/0) silently
    # skipped ALL risk management AND fell through to the entry logic below —
    # disabling the stop-loss and risking a double buy. Now an open position is
    # always risk-managed and can never re-enter in the same cycle.
    if bot.in_position:
        if _strategy_name(bot) == "scattershot" and _scattershot_active(bot):
            if is_eod_exit_window(SCATTERSHOT_EOD_WINDOW_MIN):
                closed = _close_scattershot_basket(db, owner, bot, reason="scattershot end-of-day exit")
                result.update({"action": "SELL", "reason": "Scattershot end-of-day exit.", **closed})
                return result
            state = _load_strategy_state(bot)
            tickers = ", ".join(leg.get("ticker", "?") for leg in (state.get("legs") or []))
            db.commit()
            result["reason"] = f"Scattershot basket active ({tickers})."
            return result

        if _requires_same_day_exit(bot):
            _log(db, owner.id,
                 f"[MICRO-TRADER] {bot.ticker} must exit same-day — flattening before the close.",
                 "WARNING")
            closed = _close_position(db, owner, bot, price, reason="micro-trader same-day exit")
            result.update({"action": "SELL", "reason": "Micro-trader same-day exit.", **closed})
            return result

        if not bot.avg_entry_price or bot.avg_entry_price <= 0:
            # We hold shares but have no valid cost basis: flatten immediately to
            # protect capital instead of flying blind.
            _log(db, owner.id,
                 f"[RISK] {bot.ticker} is in position with no valid entry price — "
                 f"flattening immediately to protect capital.", "WARNING")
            closed = _close_position(db, owner, bot, price, reason="invalid entry price (safety flatten)")
            result.update({"action": "SELL", "reason": "Invalid entry price — safety flatten", **closed})
            return result

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
            if _strategy_name(bot) == "swing_trader" and not _swing_hold_met(bot):
                db.commit()
                result["reason"] = (
                    f"Swing hold active ({MIN_SWING_HOLD_DAYS}d min) — take-profit deferred "
                    f"while up {profit_pct:+.2f}%."
                )
                return result
            _log(db, owner.id,
                 f"[TAKE-PROFIT] {bot.ticker} reached target {bot.take_profit_price:.4f} "
                 f"({profit_pct:+.2f}%).")
            closed = _close_position(db, owner, bot, price, reason="take-profit target")
            result.update({"action": "SELL", "reason": "Take-profit target hit", **closed})
            return result

        # 3) Structural peak/breakdown signal (respect manual constraints).
        if sig.action == "SELL" and sig.strength >= ENTRY_MIN_STRENGTH:
            if _strategy_name(bot) == "swing_trader" and not _swing_hold_met(bot):
                db.commit()
                result["reason"] = (
                    f"Swing hold active ({MIN_SWING_HOLD_DAYS}d min) — structural sell deferred."
                )
                return result
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
    if not _passes_conservative_entry_filter(analysis):
        names = ", ".join(p.get("name", "pattern") for p in (analysis.patterns or []) if p.get("bias") == "bullish") or "none"
        _log(db, owner.id,
             f"[ENTRY-FILTER] {bot.ticker} rejected — needs stronger bullish confirmation (trend={(analysis.indicators or {}).get('trend') or 'neutral'}, bullish_patterns={names}).",
             "INFO")
        bot.last_pattern_summary = "Entry skipped by conservative quality filter."
        db.commit()
        result.update({"action": "WAIT",
                       "reason": "Conservative quality filter blocked this setup; waiting for a stronger bullish confirmation."})
        return result

    if not _passes_trend_confirmation_filter(analysis):
        _log(db, owner.id,
             f"[TREND-FILTER] {bot.ticker} rejected — trend confirmation missing.",
             "INFO")
        bot.last_pattern_summary = "Entry skipped by trend confirmation filter."
        db.commit()
        result.update({"action": "WAIT",
                       "reason": "Trend confirmation filter blocked this setup; waiting for a stronger bullish trend."})
        return result

    if not _passes_quality_setup_filter(analysis):
        score = _setup_quality_score(analysis)
        _log(db, owner.id,
             f"[QUALITY-FILTER] {bot.ticker} rejected — setup quality {score:.2f} below threshold {MIN_SETUP_QUALITY_SCORE:.2f}.",
             "INFO")
        bot.last_pattern_summary = "Entry skipped by quality-of-setup filter."
        db.commit()
        result.update({"action": "WAIT",
                       "reason": "Quality-of-setup filter blocked this setup; waiting for a cleaner signal."})
        return result

    if bot.buy_limit and price > bot.buy_limit:
        db.commit()
        result["reason"] = f"Setup confirmed but price {price:.4f} above buy limit {bot.buy_limit}."
        return result
    if bot.first_buy_price and not bot.first_buy_done and price > bot.first_buy_price:
        db.commit()
        result["reason"] = f"Waiting for first-buy trigger at {bot.first_buy_price}."
        return result

    quality_score = _setup_quality_score(analysis)
    blocked, reason = _market_collision_blocked(db, owner, bot, bot.ticker, quality_score)
    if blocked:
        _log(db, owner.id, f"[ENTRY-BLOCK] {bot.ticker} blocked — {reason}")
        bot.last_pattern_summary = f"Entry skipped — another bot already holds {bot.ticker.upper()}."
        db.commit()
        result.update({"action": "WAIT", "reason": reason})
        return result

    notional = round((bot.funds_allocated or 0) * DEPLOY_FRACTION, 2)
    notional = _volatility_adjusted_notional(bot, price, notional, atr)
    if notional <= 0:
        db.commit()
        result["reason"] = "No funds allocated to this bot."
        return result

    # News sanity overlay — secondary check, never the primary driver. The chart
    # dip is confirmed above; here we only stand down on STRONG negative news.
    verdict = _news_sanity(bot.broker or owner.active_broker or "alpaca", bot.ticker)
    if verdict.get("veto"):
        _log(db, owner.id,
             f"[NEWS-VETO] Confirmed dip on {bot.ticker} but news sanity layer vetoed entry "
             f"({verdict.get('note')}).", "WARNING")
        bot.last_pattern_summary = f"Dip confirmed but held — negative news ({verdict.get('label')})."
        db.commit()
        result.update({"action": "WAIT",
                       "reason": f"Chart dip confirmed; entry deferred by news sanity ({verdict.get('label')}).",
                       "news": verdict})
        return result
    result["news"] = verdict

    # Capital availability + (optional) rotation. By default a bot will NEVER
    # liquidate another bot's position — it simply waits. Cross-bot rotation is
    # opt-in via BOT_CAPITAL_ROTATION to prevent identity-confusing behaviour.
    buying_power = _get_buying_power(owner, bot.broker or "alpaca", bot=bot)
    # Fail closed when we cannot read buying power — never enter blind on live/paper.
    if buying_power is None:
        db.commit()
        result.update({
            "action": "WAIT",
            "reason": "Buying power unavailable from broker — entry deferred.",
        })
        return result
    if buying_power < notional:
        if not CAPITAL_ROTATION_ENABLED:
            db.commit()
            result.update({"action": "WAIT",
                           "reason": "Setup confirmed but capital fully deployed; "
                                     "cross-bot rotation disabled (this bot only manages its own position)."})
            return result
        _log(db, owner.id,
             f"[CAPITAL] Setup on {bot.ticker} needs ${notional:.2f} but only "
             f"${buying_power:.2f} free — evaluating capital rotation.", "WARNING")
        freed = _attempt_capital_rotation(db, owner, bot, sig.strength, notional)
        if not freed:
            db.commit()
            result.update({"action": "WAIT",
                           "reason": "Setup confirmed but capital fully deployed; no weaker position to rotate."})
            return result
        buying_power = _get_buying_power(owner, bot.broker or "alpaca", bot=bot)
        if buying_power is None:
            db.commit()
            result.update({
                "action": "WAIT",
                "reason": "Buying power unavailable after rotation — entry deferred.",
            })
            return result
        notional = min(notional, round(buying_power * DEPLOY_FRACTION, 2))

    # Never enter on a missing/zero price: it would record an in-position state
    # with 0 shares and a meaningless cost basis (and divide-by-zero math later).
    if not price or price <= 0:
        db.commit()
        result["reason"] = "Market price unavailable — entry skipped to avoid an invalid (zero-price) fill."
        return result

    # Before placing a trade, verify cash-account strategy constraints required
    # for GFV-safe behavior. Margin accounts bypass the cash-settlement guard.
    allowed, guard = _enforce_low_balance_strategy(db, owner, bot, price, analysis)
    if not allowed:
        bot.last_pattern_summary = f"Entry skipped — cash-account strategy guard blocked this trade ({guard.get('reason')})."
        db.commit()
        result.update({"action": "WAIT", "reason": f"Entry skipped — {guard.get('reason')}.", "guard": guard})
        return result

    # Apply strategy-specific notional caps (one-shot, micro, scattershot).
    if guard.get("notional") is not None and notional > 0:
        try:
            notional = min(notional, float(guard["notional"]))
        except (TypeError, ValueError):
            pass

    # Execute the entry.
    try:
        order = _execute(owner, bot.broker, "buy", bot.ticker, notional=notional, bot=bot)
    except BrokerError as e:
        bot.last_pattern_summary = f"Live/broker order failed: {e}"
        db.commit()
        result.update({"action": "WAIT", "reason": f"Order failed: {e}"})
        return result
    if order.get("simulated") and not _paper(owner, bot):
        result.update({"action": "WAIT", "reason": "Live broker order failed; no position opened."})
        return result
    # Always derive and persist the exact share quantity — never leave a buy
    # with a null qty (the bug that rendered '0' in the History ledger).
    qty = round(notional / price, 8) if price else 0.0
    bot.in_position = True
    bot.avg_entry_price = price
    bot.shares_held = qty
    bot.position_opened_at = datetime.utcnow()
    bot.trade_count = (bot.trade_count or 0) + 1
    bot.first_buy_done = True
    _arm_risk(bot, price, atr, nearest_resistance)
    db.add(Trade(owner_id=owner.id, bot_id=bot.id, bot_uuid=bot.uuid, ticker=bot.ticker,
                 side="buy", qty=qty, notional=notional, price=price,
                 broker=bot.broker, mode=_bot_trading_mode(owner, bot),
                 broker_order_id=order["order_id"], status=order["status"]))
    _log(db, owner.id,
         f"[EXECUTE] BUY ${notional:.2f} ({qty:.6f}) of {bot.ticker} @ {price:.4f} "
         f"({sig.headline}). Stop {bot.stop_price:.4f} | Target {bot.take_profit_price:.4f}.")
    db.commit()
    _emit("trade", {
        "bot_id": bot.id, "bot_uuid": bot.uuid, "bot_name": bot.name,
        "ticker": bot.ticker, "side": "buy", "qty": qty,
        "notional": notional, "price": price, "realized_pnl": None,
        "reason": sig.headline,
    }, user_id=owner.id)
    _emit_portfolio(owner.id)
    result.update({"action": "BUY", "reason": sig.headline, "order": order,
                   "notional": notional, "qty": qty, "stop_price": bot.stop_price,
                   "take_profit_price": bot.take_profit_price})
    return result


def _record_quote(db: Session, broker: str, symbol: str, analysis: Analysis) -> None:
    """Persist + broadcast the freshest price/signal for a symbol."""
    if not symbol or analysis is None:
        return
    try:
        from app.market_store import upsert_quote
        candle_ts = analysis.candles[-1]["time"] if analysis.candles else None
        upsert_quote(db, broker, symbol, analysis.last_price,
                     signal_action=analysis.signal.action,
                     signal_strength=analysis.signal.strength,
                     candle_ts=candle_ts)
        _emit("market_quote", {
            "broker": (broker or "alpaca").lower(), "symbol": symbol.upper(),
            "price": analysis.last_price, "signal_action": analysis.signal.action,
            "signal_strength": analysis.signal.strength,
        })
    except Exception as e:  # pragma: no cover
        logger.debug("quote record failed for %s:%s — %s", broker, symbol, e)


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
def _resolve_exit_price(db: Session, bot: Bot, owner: User) -> float:
    """Best available price to value a manual/forced exit in the ledger:
    freshest stored quote → live analysis → last entry price."""
    broker = bot.broker or owner.active_broker or "alpaca"
    try:
        from app.market_store import get_quote
        q = get_quote(db, broker, bot.ticker or "")
        if q and q.price:
            return float(q.price)
    except Exception:
        pass
    a = _safe_analysis(bot, owner)
    if a and a.last_price:
        return float(a.last_price)
    return float(bot.avg_entry_price or 0.0)


def liquidate_bot(bot_id: int, reason: str = "manual sell") -> dict:
    """
    Sell EVERYTHING a single bot is holding, right now, via the robust cancel-
    then-close path — without touching the bot row itself. Safe to call from the
    Manual Sell endpoint and from delete_bot (which liquidates before removing).
    Opens its own session so it is self-contained.
    """
    db = SessionLocal()
    try:
        bot = db.query(Bot).filter(Bot.id == bot_id).first()
        if not bot:
            return {"action": "ERROR", "reason": "Bot not found."}
        owner = bot.owner
        # Scattershot baskets store multi-leg state and a comma-joined ticker —
        # always unwind via the basket closer, even when in_position/shares_held
        # look like a single-leg position.
        if _strategy_name(bot) == "scattershot" and (
            _scattershot_active(bot) or bot.in_position or "," in (bot.ticker or "")
        ):
            closed = _close_scattershot_basket(db, owner, bot, reason=reason)
            return {"action": closed.get("action") or "SELL", "reason": reason, **closed}
        if not bot.in_position or (bot.shares_held or 0) <= 0:
            return {"action": "FLAT", "reason": "Bot holds no open position to sell."}
        price = _resolve_exit_price(db, bot, owner)
        closed = _close_position(db, owner, bot, price, reason=reason)
        return {"action": "SELL", "reason": reason, "price": price, **closed}
    finally:
        db.close()


_bot_cycle_locks: dict[int, threading.Lock] = {}
_bot_cycle_locks_guard = threading.Lock()


def _acquire_bot_cycle_lock(bot_id: int) -> threading.Lock | None:
    """Non-blocking per-bot lock so scheduler + manual run-cycle cannot overlap."""
    with _bot_cycle_locks_guard:
        lock = _bot_cycle_locks.get(bot_id)
        if lock is None:
            lock = threading.Lock()
            _bot_cycle_locks[bot_id] = lock
    if not lock.acquire(blocking=False):
        return None
    return lock


def run_cycle(bot_id: int) -> dict:
    """
    Run one full decision cycle for a single bot by id. Opens its own DB
    session so it is safe to call from an API route or a background scheduler.
    """
    lock = _acquire_bot_cycle_lock(bot_id)
    if lock is None:
        return {"action": "SKIPPED", "reason": "Cycle already in progress for this bot."}
    db = SessionLocal()
    try:
        bot = db.query(Bot).filter(Bot.id == bot_id).first()
        if not bot:
            return {"action": "ERROR", "reason": "Bot not found."}

        owner = bot.owner

        # ── Market-hours halt ─────────────────────────────────────────────
        # Equities (Alpaca) only trade 09:30–16:00 ET, Mon–Fri. Outside that,
        # suspend the trading loop so no buy/sell orders are placed. Crypto
        # (OKX) is 24/7 and never halts. Paused bots are handled below as usual.
        broker = (bot.broker or owner.active_broker or "alpaca").lower()
        if bot.running and not market_open_for_broker(broker):
            bot.last_analysis_at = datetime.utcnow()
            bot.last_pattern_summary = "SYSTEM HALT: Market offline. Core trading loops suspended."
            db.commit()
            return {"action": "HALTED", "market_closed": True,
                    "reason": "US stock market is closed — trading suspended until the next session."}

        strategy = _strategy_name(bot)
        if strategy == "scattershot":
            return _run_scattershot_cycle(db, bot)

        # Fully autonomous + flat → scan all markets and pick the best dip.
        if bot.auto_select and not bot.in_position:
            if not bot.running:
                return {"action": "SKIPPED", "reason": "Bot is paused."}
            analysis = _pick_best_setup(db, owner, bot)
            if analysis is None:
                bot.last_signal = "HOLD"
                bot.last_analysis_at = datetime.utcnow()
                bot.last_pattern_summary = "Scanning all markets — no confirmed dip yet."
                db.commit()
                return {"action": "WAIT",
                        "reason": "Autonomous scan found no confirmed dip this cycle."}
            bot.ticker = analysis.symbol  # lock onto the chosen asset for this trade
            db.commit()
            return run_bot_cycle(db, bot, analysis)

        analysis = _safe_analysis(bot, owner)
        if analysis is None:
            bot.last_pattern_summary = "Market data unavailable (check broker keys/connectivity)."
            db.commit()
            return {"action": "ERROR",
                    "reason": f"Could not fetch market data for {bot.ticker or 'this bot'}."}
        return run_bot_cycle(db, bot, analysis)
    finally:
        db.close()
        lock.release()


def run_all_active_bots() -> dict:
    """Scan + act on every running bot. Intended for a background scheduler."""
    summary = {"scanned": 0, "actions": []}
    db = SessionLocal()
    try:
        bot_ids = [b.id for b in db.query(Bot.id).filter(Bot.running == True).all()]  # noqa: E712
    finally:
        db.close()

    logger.info("[ENGINE] Scanning %d active bots", len(bot_ids))
    for bot_id in bot_ids:
        summary["scanned"] += 1
        try:
            res = run_cycle(bot_id)  # handles autonomous selection + its own session
            if res.get("action") not in (None, "HOLD", "SKIPPED", "WAIT", "ERROR"):
                summary["actions"].append({"bot_id": bot_id, "action": res["action"]})
        except Exception as e:
            logger.error("Cycle failed for bot %s: %s", bot_id, e)
    return summary
