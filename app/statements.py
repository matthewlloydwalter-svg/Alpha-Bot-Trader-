"""
statements.py — monthly account statements.

Builds a per-user trading summary (total realized P&L, per-bot performance,
active allocations) and emails it via Resend. The scheduler runs
``send_due_statements`` daily; it only sends on/after the 1st of a month and is
idempotent per user (``User.last_statement_sent``) so a process restart cannot
email the same statement twice.
"""

from __future__ import annotations

import logging
from datetime import datetime

from app.database import SessionLocal, User, Bot, Trade
from app import email_service

logger = logging.getLogger("alphabot.statements")

try:
    from app.auth import PLATFORM_NAME
except Exception:  # pragma: no cover — avoid import cycle surprises
    PLATFORM_NAME = "AlphaBotix Trading"


def _prev_month_label(now: datetime) -> str:
    """Human label for the month that just ended (statements cover last month)."""
    year, month = now.year, now.month
    month -= 1
    if month == 0:
        month = 12
        year -= 1
    return datetime(year, month, 1).strftime("%B %Y")


def _previous_month_bounds(now: datetime) -> tuple[datetime, datetime]:
    """Return the previous calendar month's half-open UTC bounds."""
    period_end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if period_end.month == 1:
        period_start = period_end.replace(year=period_end.year - 1, month=12)
    else:
        period_start = period_end.replace(month=period_end.month - 1)
    return period_start, period_end


def _period_trade_totals(trades: list[Trade]) -> tuple[float, int]:
    """Calculate realized P&L using weighted-average cost for each ticker."""
    positions: dict[str, tuple[float, float]] = {}
    realized_pnl = 0.0

    for trade in trades:
        side = (trade.side or "").lower()
        qty = float(trade.qty or 0.0)
        price = float(trade.price or 0.0)
        if qty <= 0 or price <= 0:
            continue

        ticker = (trade.ticker or "").upper()
        held_qty, avg_cost = positions.get(ticker, (0.0, 0.0))
        if side == "buy":
            new_qty = held_qty + qty
            avg_cost = ((held_qty * avg_cost) + (qty * price)) / new_qty
            positions[ticker] = (new_qty, avg_cost)
        elif side == "sell" and held_qty > 0:
            sold_qty = min(qty, held_qty)
            realized_pnl += (price - avg_cost) * sold_qty
            remaining_qty = held_qty - sold_qty
            positions[ticker] = (remaining_qty, avg_cost if remaining_qty > 0 else 0.0)

    return realized_pnl, len(trades)


def build_user_statement(
    user: User,
    bots: list[Bot],
    *,
    db,
    period_start: datetime,
    period_end: datetime,
) -> dict:
    """Assemble a user's statement from trades in the requested period."""
    total_pnl = 0.0
    total_allocated = 0.0
    bot_rows: list[dict] = []
    for b in bots:
        trades = (
            db.query(Trade)
            .filter(
                Trade.bot_id == b.id,
                Trade.created_at >= period_start,
                Trade.created_at < period_end,
            )
            .order_by(Trade.created_at.asc(), Trade.id.asc())
            .all()
        )
        pnl, trade_count = _period_trade_totals(trades)
        allocated = float(b.funds_allocated or 0.0)
        total_pnl += pnl
        if b.running:
            total_allocated += allocated
        bot_rows.append({
            "name": b.name or f"Bot #{b.id}",
            "pnl": pnl,
            "trades": trade_count,
            "allocated": allocated,
            "status": "Running" if b.running else "Paused",
        })
    # Best performers first so the most relevant rows lead the table.
    bot_rows.sort(key=lambda r: r["pnl"], reverse=True)
    return {
        "total_pnl": round(total_pnl, 2),
        "total_allocated": round(total_allocated, 2),
        "bot_rows": bot_rows,
    }


def send_due_statements(*, force: bool = False, now: datetime | None = None) -> dict:
    """
    Send monthly statements to every eligible user.

    Sends only on/after the 1st of the month unless ``force=True``. Idempotent:
    a user already sent a statement this calendar month is skipped.
    """
    now = now or datetime.utcnow()
    if not force and now.day != 1:
        return {"skipped": "not the 1st", "sent": 0}

    period = _prev_month_label(now)
    period_start, period_end = _previous_month_bounds(now)
    sent = 0
    skipped = 0
    failed = 0
    db = SessionLocal()
    try:
        users = db.query(User).filter(User.email.isnot(None)).all()
        for user in users:
            last = user.last_statement_sent
            if not force and last is not None and (last.year, last.month) == (now.year, now.month):
                skipped += 1
                continue
            bots = db.query(Bot).filter(Bot.owner_id == user.id).all()
            # Skip accounts with no bots to avoid empty noise (unless forced).
            if not bots and not force:
                skipped += 1
                continue
            stmt = build_user_statement(
                user,
                bots,
                db=db,
                period_start=period_start,
                period_end=period_end,
            )
            ok = email_service.send_monthly_statement(
                user.email,
                period_label=period,
                total_pnl=stmt["total_pnl"],
                total_allocated=stmt["total_allocated"],
                bot_rows=stmt["bot_rows"],
                display_name=user.name,
                platform_name=PLATFORM_NAME,
            )
            if ok:
                user.last_statement_sent = now
                db.commit()
                sent += 1
            else:
                db.rollback()
                failed += 1
    finally:
        db.close()

    summary = {"period": period, "sent": sent, "skipped": skipped, "failed": failed}
    logger.info("[STATEMENTS] Monthly statement run: %s", summary)
    return summary
