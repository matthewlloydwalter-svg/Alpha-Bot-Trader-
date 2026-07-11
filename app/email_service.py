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

# Prefer brand-matched display name; domain must be verified in Resend.
DEFAULT_FROM = "AlphaBotix Trading <updates@alphabotixtrading.com>"
RESEND_API_KEY = (os.getenv("RESEND_API_KEY") or "").strip()
EMAIL_FROM = (os.getenv("EMAIL_FROM") or DEFAULT_FROM).strip()


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

    ``from_addr`` defaults to AlphaBotix Trading <updates@alphabotixtrading.com>.
    Provide at least one of ``html`` or ``text``.
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

    params: dict = {
        "from": (from_addr or EMAIL_FROM).strip(),
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
