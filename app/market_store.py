"""
market_store.py — read/write helpers for the ``market_quotes`` table.

This is the database-of-record for live market state. The background poller
(``app/scheduler.py``) writes here continuously; the API and bots read from
here so a value can never silently freeze just because nobody refreshed a page.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.database import MarketQuote

logger = logging.getLogger("alphabot.marketstore")


def upsert_quote(db: Session, broker: str, symbol: str, price: Optional[float],
                 signal_action: Optional[str] = None,
                 signal_strength: Optional[float] = None,
                 candle_ts: Optional[int] = None) -> MarketQuote:
    """Insert or update the single live quote row for (broker, symbol)."""
    broker = (broker or "alpaca").lower()
    symbol = (symbol or "").upper()
    row = (db.query(MarketQuote)
             .filter(MarketQuote.broker == broker, MarketQuote.symbol == symbol)
             .first())
    if row is None:
        row = MarketQuote(broker=broker, symbol=symbol)
        db.add(row)
    if price is not None:
        row.price = float(price)
    if signal_action is not None:
        row.signal_action = signal_action
    if signal_strength is not None:
        row.signal_strength = float(signal_strength)
    if candle_ts is not None:
        row.candle_ts = int(candle_ts)
    row.updated_at = datetime.utcnow()
    db.commit()
    return row


def get_quote(db: Session, broker: str, symbol: str) -> Optional[MarketQuote]:
    return (db.query(MarketQuote)
              .filter(MarketQuote.broker == (broker or "alpaca").lower(),
                      MarketQuote.symbol == (symbol or "").upper())
              .first())


def get_quotes(db: Session, broker: Optional[str] = None) -> list[MarketQuote]:
    q = db.query(MarketQuote)
    if broker:
        q = q.filter(MarketQuote.broker == broker.lower())
    return q.order_by(MarketQuote.symbol.asc()).all()


def quote_to_dict(row: MarketQuote) -> dict:
    return {
        "broker": row.broker,
        "symbol": row.symbol,
        "price": row.price,
        "signal_action": row.signal_action,
        "signal_strength": row.signal_strength,
        "candle_ts": row.candle_ts,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
