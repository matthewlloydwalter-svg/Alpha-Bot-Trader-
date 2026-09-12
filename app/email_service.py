"""
email_service.py — Resend-backed transactional email for AlphaBotix Trading.

Replaces the previous Gmail SMTP (smtplib) path. All outbound mail goes
through Resend using RESEND_API_KEY.
"""

from __future__ import annotations

import logging
import os
from typing import Optional, Sequence, Union

logger = logging.getLogger("alphabot.email")

# Resend rejects a bare display name in the `from` field with HTTP 422
# ("Invalid 'from' field") — it requires a real email address, optionally with a
# display name: "Name <email@domain>". Resend's shared sandbox sender
# (onboarding@resend.dev) works without domain verification, so it is the safe
# default for testing. Swap EMAIL_FROM_ADDRESS to your verified domain sender
# (e.g. "AlphaBotix Trading <noreply@alphabotix.com>") once it's verified.
DEFAULT_SENDER_EMAIL = "onboarding@resend.dev"
DEFAULT_SENDER_NAME = "AlphaBotix Trading"
DEFAULT_FROM = f"{DEFAULT_SENDER_NAME} <{DEFAULT_SENDER_EMAIL}>"
RESEND_API_KEY = (os.getenv("RESEND_API_KEY") or "").strip()


def resolve_from_address() -> str:
    """
    Resolve a Resend-valid `from` value ("Name <email>" or "email").

    Order of preference: EMAIL_FROM_ADDRESS, then legacy EMAIL_FROM, then the
    default sandbox sender. If the configured value is only a display name with
    no email (the cause of the 422), we attach the default sender address so the
    payload is always accepted.
    """
    raw = (
        os.getenv("EMAIL_FROM_ADDRESS")
        or os.getenv("EMAIL_FROM")
        or DEFAULT_FROM
    ).strip()
    if not raw:
        return DEFAULT_FROM
    # Valid: "Name <email@domain>" or a bare "email@domain".
    if "<" in raw and ">" in raw and "@" in raw.split("<", 1)[1]:
        return raw
    if "@" in raw and "<" not in raw and ">" not in raw:
        return raw
    # Only a display name (e.g. "AlphaBotix Trading") — attach a real address.
    name = raw.replace("<", "").replace(">", "").strip() or DEFAULT_SENDER_NAME
    return f"{name} <{DEFAULT_SENDER_EMAIL}>"


# Backwards-compatible module attribute (some code imports EMAIL_FROM directly).
EMAIL_FROM = resolve_from_address()


class EmailError(Exception):
    """Raised with a human-readable reason when an email cannot be sent."""


def _ensure_client():
    """Configure the Resend SDK; raise EmailError if the key is missing."""
    if not RESEND_API_KEY:
        raise EmailError(
            "Email is not configured on the server. Set RESEND_API_KEY "
            "in your environment / Railway variables (from the Resend dashboard)."
        )
    try:
        import resend
    except ImportError as e:
        raise EmailError(
            "The 'resend' package is not installed. Add it to requirements.txt "
            "and redeploy (pip install resend)."
        ) from e
    resend.api_key = RESEND_API_KEY
    return resend


def send_email(
    to: Union[str, Sequence[str]],
    subject: str,
    *,
    html: Optional[str] = None,
    text: Optional[str] = None,
    from_addr: Optional[str] = None,
) -> dict:
    """
    Send an email via Resend.

    ``from_addr`` defaults to ``resolve_from_address()`` (EMAIL_FROM_ADDRESS /
    EMAIL_FROM / the sandbox sender), always coerced to a Resend-valid value so
    a bare display name can't trigger a 422. Provide at least one of ``html`` or
    ``text``.
    """
    resend = _ensure_client()

    if isinstance(to, str):
        recipients = [to.strip()]
    else:
        recipients = [str(addr).strip() for addr in to if str(addr).strip()]
    if not recipients:
        raise EmailError("No recipient address provided.")
    if not (html or text):
        raise EmailError("Email body is empty — provide html and/or text.")

    # Resolve at call time so runtime env changes / an explicit but malformed
    # from_addr can't produce an invalid payload.
    from_value = (from_addr or "").strip() or resolve_from_address()
    if "@" not in from_value:
        # Explicit display-name-only override — attach a real sender address.
        name = from_value.replace("<", "").replace(">", "").strip() or DEFAULT_SENDER_NAME
        from_value = f"{name} <{DEFAULT_SENDER_EMAIL}>"

    params: dict = {
        "from": from_value,
        "to": recipients,
        "subject": subject,
    }
    if html:
        params["html"] = html
    if text:
        params["text"] = text

    try:
        result = resend.Emails.send(params)
        logger.info(
            "Email sent via Resend to %s (id=%s)",
            ", ".join(recipients),
            (result or {}).get("id") if isinstance(result, dict) else getattr(result, "id", None),
        )
        return result if isinstance(result, dict) else {"id": getattr(result, "id", None)}
    except EmailError:
        raise
    except Exception as e:
        # Resend SDK raises resend.exceptions.* — surface a clean message.
        logger.error("Resend delivery failed: %s", e)
        msg = str(e).strip() or e.__class__.__name__
        raise EmailError(f"Resend could not send the email — {msg}") from e


def send_verification_email(to_email: str, code: str, platform_name: str = "AlphaBotix Trading") -> bool:
    """Send a 6-digit email verification code via Resend."""
    subject = f"[{platform_name}] Your verification code"
    text = (
        f"Your {platform_name} verification code is: {code}\n\n"
        "If you did not request this, you can ignore this email."
    )
    html = (
        f"<p>Your <strong>{platform_name}</strong> verification code is:</p>"
        f"<p style=\"font-size:24px;letter-spacing:4px;font-weight:700\">{code}</p>"
        f"<p style=\"color:#666;font-size:13px\">If you did not request this, "
        f"you can ignore this email.</p>"
    )
    send_email(to_email, subject, html=html, text=text)
    return True


def send_password_reset_email(to_email: str, code: str, platform_name: str = "AlphaBotix Trading") -> bool:
    """Send a password-reset verification code via the same Resend path."""
    subject = f"[{platform_name}] Password reset code"
    text = (
        f"Your {platform_name} password reset code is: {code}\n\n"
        "Enter this code on the reset form to choose a new password.\n"
        "If you did not request a password reset, you can ignore this email."
    )
    html = (
        f"<p>Your <strong>{platform_name}</strong> password reset code is:</p>"
        f"<p style=\"font-size:24px;letter-spacing:4px;font-weight:700\">{code}</p>"
        f"<p style=\"color:#666;font-size:13px\">Enter this code on the reset form to "
        f"choose a new password. If you did not request a password reset, you can "
        f"ignore this email.</p>"
    )
    send_email(to_email, subject, html=html, text=text)
    return True


# ── Transactional receipts & statements ───────────────────────────────────
# These are "best effort": they must never break the API request that triggers
# them (bot creation, fund changes, auto-pause). Each catches EmailError (and
# anything else) and returns True/False so callers can fire-and-forget.

def _fmt_money(value) -> str:
    """Format a value as US-dollar currency, defaulting invalid values to zero."""
    try:
        return f"${float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def _wrap_html(platform_name: str, heading: str, body_html: str) -> str:
    """Wrap receipt content in a simple, email-client-safe branded shell."""
    return (
        "<div style=\"font-family:system-ui,-apple-system,Segoe UI,Arial,sans-serif;"
        "max-width:560px;margin:0 auto;color:#1a1e28\">"
        "<div style=\"background:#0d0f14;padding:18px 22px;border-radius:10px 10px 0 0\">"
        f"<span style=\"color:#4d9fff;font-size:18px;font-weight:700\">{platform_name}</span>"
        "</div>"
        "<div style=\"border:1px solid #e3e6ee;border-top:none;border-radius:0 0 10px 10px;"
        "padding:22px\">"
        f"<h2 style=\"margin:0 0 14px;font-size:18px\">{heading}</h2>"
        f"{body_html}"
        "<p style=\"color:#8b91a8;font-size:12px;margin-top:22px\">This is an automated "
        f"message from {platform_name}. You are receiving it because you have an active "
        "account and trading bots.</p>"
        "</div></div>"
    )


def _try_send(to_email: str, subject: str, *, html: str, text: str) -> bool:
    """Send, swallowing errors so a failed email never breaks the caller."""
    if not (to_email or "").strip():
        return False
    try:
        send_email(to_email, subject, html=html, text=text)
        return True
    except EmailError as e:
        logger.warning("Receipt email skipped (%s): %s", subject, e)
        return False
    except Exception as e:  # pragma: no cover — never break the request
        logger.error("Receipt email unexpectedly failed (%s): %s", subject, e)
        return False


def send_bot_created_receipt(
    to_email: str,
    *,
    bot_name: str,
    ticker: Optional[str],
    funds: float,
    broker: str,
    mode: str,
    platform_name: str = "AlphaBotix Trading",
) -> bool:
    """Immediate receipt when a user creates a new trading bot."""
    asset = (ticker or "Autonomous (engine-selected)").upper() if ticker else "Autonomous (engine-selected)"
    subject = f"[{platform_name}] Bot created: {bot_name}"
    rows = [
        ("Bot", bot_name),
        ("Asset", asset),
        ("Allocated funds", _fmt_money(funds)),
        ("Broker", (broker or "alpaca").title()),
        ("Account", (mode or "paper").title()),
    ]
    body = "<table style=\"width:100%;border-collapse:collapse;font-size:14px\">" + "".join(
        f"<tr><td style=\"padding:6px 0;color:#8b91a8\">{k}</td>"
        f"<td style=\"padding:6px 0;text-align:right;font-weight:600\">{v}</td></tr>"
        for k, v in rows
    ) + "</table>"
    html = _wrap_html(platform_name, "Your bot is live", body)
    text = (
        f"{platform_name} — Bot created\n\n"
        + "\n".join(f"{k}: {v}" for k, v in rows)
    )
    return _try_send(to_email, subject, html=html, text=text)


def send_funds_change_receipt(
    to_email: str,
    *,
    bot_name: str,
    previous: float,
    new: float,
    platform_name: str = "AlphaBotix Trading",
) -> bool:
    """Immediate receipt when funds are allocated to / de-allocated from a bot."""
    delta = float(new or 0) - float(previous or 0)
    action = "allocated to" if delta >= 0 else "de-allocated from"
    subject = f"[{platform_name}] Funds {'allocated' if delta >= 0 else 'de-allocated'}: {bot_name}"
    rows = [
        ("Bot", bot_name),
        ("Change", f"{'+' if delta >= 0 else '-'}{_fmt_money(abs(delta))}"),
        ("Previous balance", _fmt_money(previous)),
        ("New balance", _fmt_money(new)),
    ]
    body = (
        f"<p style=\"font-size:14px\">{_fmt_money(abs(delta))} was {action} "
        f"<strong>{bot_name}</strong>.</p>"
        "<table style=\"width:100%;border-collapse:collapse;font-size:14px\">"
        + "".join(
            f"<tr><td style=\"padding:6px 0;color:#8b91a8\">{k}</td>"
            f"<td style=\"padding:6px 0;text-align:right;font-weight:600\">{v}</td></tr>"
            for k, v in rows
        )
        + "</table>"
    )
    html = _wrap_html(platform_name, "Allocation updated", body)
    text = (
        f"{platform_name} — Funds {action} {bot_name}\n\n"
        + "\n".join(f"{k}: {v}" for k, v in rows)
    )
    return _try_send(to_email, subject, html=html, text=text)


def send_bot_paused_receipt(
    to_email: str,
    *,
    bot_name: str,
    platform_name: str = "AlphaBotix Trading",
) -> bool:
    """Alert when a bot is auto-paused because its balance reached $0."""
    subject = f"[{platform_name}] Bot paused (insufficient funds): {bot_name}"
    body = (
        f"<p style=\"font-size:14px\"><strong>{bot_name}</strong> has been automatically "
        "paused because its allocated balance reached $0.00 and it can no longer execute "
        "trades.</p>"
        "<p style=\"font-size:14px\">Allocate more funds to this bot from your dashboard to "
        "resume trading.</p>"
    )
    html = _wrap_html(platform_name, "Bot paused — insufficient funds", body)
    text = (
        f"{platform_name} — {bot_name} was auto-paused because its allocated balance "
        "reached $0.00. Allocate more funds from your dashboard to resume trading."
    )
    return _try_send(to_email, subject, html=html, text=text)


def send_monthly_statement(
    to_email: str,
    *,
    period_label: str,
    total_pnl: float,
    total_allocated: float,
    bot_rows: Sequence[dict],
    display_name: Optional[str] = None,
    platform_name: str = "AlphaBotix Trading",
) -> bool:
    """
    Monthly summary statement: total PnL, per-bot performance, active allocations.

    ``bot_rows`` items: {"name", "pnl", "trades", "allocated", "status"}.
    """
    subject = f"[{platform_name}] Your {period_label} statement"
    greeting = f"Hi {display_name}," if display_name else "Hi,"
    pnl_color = "#00a06a" if float(total_pnl or 0) >= 0 else "#d64562"

    if bot_rows:
        header = (
            "<tr style=\"font-size:12px;color:#8b91a8;text-align:left\">"
            "<th style=\"padding:6px 4px\">Bot</th>"
            "<th style=\"padding:6px 4px;text-align:right\">P&amp;L</th>"
            "<th style=\"padding:6px 4px;text-align:right\">Trades</th>"
            "<th style=\"padding:6px 4px;text-align:right\">Allocated</th>"
            "<th style=\"padding:6px 4px;text-align:right\">Status</th></tr>"
        )
        body_rows = ""
        for r in bot_rows:
            pnl = float(r.get("pnl", 0) or 0)
            c = "#00a06a" if pnl >= 0 else "#d64562"
            body_rows += (
                "<tr style=\"font-size:13px;border-top:1px solid #eef0f5\">"
                f"<td style=\"padding:8px 4px;font-weight:600\">{r.get('name','')}</td>"
                f"<td style=\"padding:8px 4px;text-align:right;color:{c}\">"
                f"{'+' if pnl >= 0 else ''}{_fmt_money(pnl)}</td>"
                f"<td style=\"padding:8px 4px;text-align:right\">{int(r.get('trades',0) or 0)}</td>"
                f"<td style=\"padding:8px 4px;text-align:right\">{_fmt_money(r.get('allocated',0))}</td>"
                f"<td style=\"padding:8px 4px;text-align:right\">{r.get('status','')}</td></tr>"
            )
        bot_table = (
            "<table style=\"width:100%;border-collapse:collapse;margin-top:8px\">"
            + header + body_rows + "</table>"
        )
    else:
        bot_table = "<p style=\"font-size:14px;color:#8b91a8\">No bots were active this period.</p>"

    body = (
        f"<p style=\"font-size:14px\">{greeting}</p>"
        f"<p style=\"font-size:14px\">Here is your {period_label} trading summary.</p>"
        "<div style=\"display:flex;gap:12px;margin:16px 0\">"
        "<div style=\"flex:1;background:#f5f7fb;border-radius:8px;padding:12px\">"
        "<div style=\"font-size:12px;color:#8b91a8\">Total P&amp;L</div>"
        f"<div style=\"font-size:20px;font-weight:700;color:{pnl_color}\">"
        f"{'+' if float(total_pnl or 0) >= 0 else ''}{_fmt_money(total_pnl)}</div></div>"
        "<div style=\"flex:1;background:#f5f7fb;border-radius:8px;padding:12px\">"
        "<div style=\"font-size:12px;color:#8b91a8\">Active allocations</div>"
        f"<div style=\"font-size:20px;font-weight:700\">{_fmt_money(total_allocated)}</div></div>"
        "</div>"
        "<h3 style=\"font-size:15px;margin:16px 0 4px\">Bot performance</h3>"
        + bot_table
    )
    html = _wrap_html(platform_name, f"{period_label} statement", body)

    text_lines = [
        f"{platform_name} — {period_label} statement",
        "",
        f"Total P&L: {_fmt_money(total_pnl)}",
        f"Active allocations: {_fmt_money(total_allocated)}",
        "",
        "Bot performance:",
    ]
    for r in bot_rows:
        text_lines.append(
            f"  - {r.get('name','')}: P&L {_fmt_money(r.get('pnl',0))}, "
            f"{int(r.get('trades',0) or 0)} trades, "
            f"allocated {_fmt_money(r.get('allocated',0))}, {r.get('status','')}"
        )
    text = "\n".join(text_lines)
    return _try_send(to_email, subject, html=html, text=text)
