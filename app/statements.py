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

from app.database import SessionLocal, User, Bot
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


def build_user_statement(user: User, bots: list[Bot]) -> dict:
    """Assemble the statement payload for a single user from their bots."""
    total_pnl = 0.0
    total_allocated = 0.0
    bot_rows: list[dict] = []
    for b in bots:
        pnl = float(b.realized_pnl or 0.0)
        allocated = float(b.funds_allocated or 0.0)
        total_pnl += pnl
        if b.running:
            total_allocated += allocated
        bot_rows.append({
            "name": b.name or f"Bot #{b.id}",
            "pnl": pnl,
            "trades": int(b.trade_count or 0),
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
            stmt = build_user_statement(user, bots)
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
