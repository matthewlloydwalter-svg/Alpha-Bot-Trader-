"""
bot_engine.py — the trading bot "brain". Asks Claude for a BUY/SELL/HOLD
decision given a bot's current state, then (if the decision clears the
bot's configured limits) places the order through brokers.py and logs
a Trade row.

This module does NOT run on a background scheduler by default — see
the note in main.py's /bots/{id}/run-cycle endpoint for two ways to
trigger it (manual button in your dashboard, or a real scheduler like
APScheduler/Celery once you're ready for that).
"""

import os
import re
import logging
import requests
from sqlalchemy.orm import Session

from database import Bot, Trade, User
from brokers import place_order, BrokerError
from auth import send_verification_email

logger = logging.getLogger("alphabot")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = "claude-sonnet-4-6"


def ask_claude_for_decision(bot: Bot, current_price: float, recent_prices: list[float],
                             news_summary: str) -> tuple[str, str]:
    """Returns (action, reasoning) where action is BUY / SELL / HOLD."""
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set — defaulting bot decision to HOLD")
        return "HOLD", "AI analysis unavailable (no API key configured)."

    limits = []
    if bot.buy_limit:
        limits.append(f"buy only below ${bot.buy_limit}")
    if bot.sell_limit:
        limits.append(f"sell only above ${bot.sell_limit}")
    if bot.min_profit_pct:
        limits.append(f"minimum {bot.min_profit_pct}% profit before selling")

    position_info = (
        f"Currently holding {bot.shares_held} units at avg entry ${bot.avg_entry_price}."
        if bot.in_position else "Not in a position — looking for a dip entry."
    )

    prompt = (
        f"You are an autonomous AI trading bot for {bot.ticker or 'an asset you should pick'}. "
        f"Current price: ${current_price:.4f}. Funds allocated: ${bot.funds_allocated}. "
        f"{position_info} Strategy: buy the dips, sell for profit. "
        f"Recent price trend: {' -> '.join(f'${p:.2f}' for p in recent_prices[-5:])}. "
        f"News context (weight ~25%, do not let it dominate): {news_summary}. "
        f"Constraints: {', '.join(limits) if limits else 'none'}. "
        f"Respond with exactly one word first — BUY, SELL, or HOLD — then a one-sentence reason."
    )

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        text = next((b["text"] for b in data.get("content", []) if b.get("type") == "text"), "HOLD")
        match = re.search(r"\b(BUY|SELL|HOLD)\b", text.upper())
        action = match.group(1) if match else "HOLD"
        return action, text.strip()
    except Exception as e:
        logger.error("Claude API call failed: %s", e)
        return "HOLD", f"AI call failed: {e}"


def run_bot_cycle(db: Session, bot: Bot, current_price: float, recent_prices: list[float],
                   news_summary: str = "") -> dict:
    """
    Runs one decision cycle for a bot: ask Claude, then act on the
    decision if it clears the bot's limits. Returns a summary dict
    your route can return to the frontend.
    """
    if not bot.running:
        return {"action": "SKIPPED", "reason": "Bot is paused."}

    owner: User = bot.owner

    # Respect an unfilled "first buy" price gate before anything else.
    if bot.first_buy_price and not bot.first_buy_done:
        if current_price > bot.first_buy_price:
            return {"action": "WAIT", "reason": f"Waiting for first buy at ${bot.first_buy_price}"}
        bot.first_buy_done = True

    action, reasoning = ask_claude_for_decision(bot, current_price, recent_prices, news_summary)

    result = {"action": action, "reasoning": reasoning, "order": None}

    try:
        if action == "BUY" and not bot.in_position and (not bot.buy_limit or current_price <= bot.buy_limit):
            notional = round(bot.funds_allocated * 0.3, 2)
            order = place_order(
                broker=bot.broker, side="buy", symbol=bot.ticker, notional=notional,
                alpaca_key=owner.alpaca_key, alpaca_secret=owner.alpaca_secret,
                okx_key=owner.okx_key, okx_secret=owner.okx_secret, okx_passphrase=owner.okx_passphrase,
                paper=(owner.trading_mode == "paper"),
            )
            bot.in_position = True
            bot.avg_entry_price = current_price
            bot.shares_held = notional / current_price
            bot.trade_count += 1
            db.add(Trade(owner_id=owner.id, bot_id=bot.id, ticker=bot.ticker, side="buy",
                          notional=notional, price=current_price, broker=bot.broker,
                          mode=owner.trading_mode, broker_order_id=order["order_id"],
                          status=order["status"]))
            result["order"] = order
            send_email(owner.email, f"✅ AlphaBot bought {bot.ticker}",
                       f"Bot '{bot.name}' bought ${notional} of {bot.ticker} at ${current_price:.2f}.")

        elif action == "SELL" and bot.in_position:
            sell_ok = not bot.sell_limit or current_price >= bot.sell_limit
            profit_pct = ((current_price - bot.avg_entry_price) / bot.avg_entry_price * 100) if bot.avg_entry_price else 0
            profit_ok = not bot.min_profit_pct or profit_pct >= bot.min_profit_pct

            if sell_ok and profit_ok:
                order = place_order(
                    broker=bot.broker, side="sell", symbol=bot.ticker, qty=bot.shares_held,
                    alpaca_key=owner.alpaca_key, alpaca_secret=owner.alpaca_secret,
                    okx_key=owner.okx_key, okx_secret=owner.okx_secret, okx_passphrase=owner.okx_passphrase,
                    paper=(owner.trading_mode == "paper"),
                )
                gain = (current_price - bot.avg_entry_price) * bot.shares_held
                bot.realized_pnl += gain
                bot.in_position = False
                bot.trade_count += 1
                db.add(Trade(owner_id=owner.id, bot_id=bot.id, ticker=bot.ticker, side="sell",
                              qty=bot.shares_held, price=current_price, broker=bot.broker,
                              mode=owner.trading_mode, broker_order_id=order["order_id"],
                              status=order["status"]))
                bot.shares_held = 0
                bot.avg_entry_price = None
                result["order"] = order
                result["realized_gain"] = gain
                send_email(owner.email, f"✅ AlphaBot sold {bot.ticker}",
                           f"Bot '{bot.name}' sold {bot.ticker} at ${current_price:.2f}. Gain: ${gain:.2f}.")
            else:
                result["action"] = "HOLD"
                result["reason"] = "Sell signal did not clear limit/profit constraints."

        db.commit()

    except BrokerError as e:
        db.rollback()
        logger.error("Order failed for bot %s: %s", bot.id, e)
        send_email(owner.email, f"❌ AlphaBot order failed for {bot.ticker}", str(e))
        result["error"] = str(e)

    return result
