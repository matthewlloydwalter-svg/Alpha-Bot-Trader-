"""Subscription-tier support routing for customer support / mailto aliases."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Optional

from app.plans import normalize_plan, plan_level_label

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# Forwarding aliases users email (Gmail filter → Support/… labels). Override via env.
_SUPPORT_DOMAIN = (
    os.getenv("SUPPORT_MAIL_DOMAIN") or "alphabotixtrading.com"
).strip().lstrip("@")

SUPPORT_MAILTO = {
    "enterprise": os.getenv("SUPPORT_MAILTO_ENTERPRISE")
    or f"enterprise@{_SUPPORT_DOMAIN}",
    "pro": os.getenv("SUPPORT_MAILTO_PRO") or f"pro@{_SUPPORT_DOMAIN}",
    "growth": os.getenv("SUPPORT_MAILTO_GROWTH") or f"growth@{_SUPPORT_DOMAIN}",
    "starter": os.getenv("SUPPORT_MAILTO_DEFAULT") or f"support@{_SUPPORT_DOMAIN}",
}

# Internal destination inboxes (provider routing / team aliases).
SUPPORT_INBOX = {
    "enterprise": os.getenv("SUPPORT_INBOX_ENTERPRISE")
    or f"enterprise@{_SUPPORT_DOMAIN}",
    "growth": os.getenv("SUPPORT_INBOX_STANDARD")
    or f"growth@{_SUPPORT_DOMAIN}",
    "pro": os.getenv("SUPPORT_INBOX_PRO") or f"pro@{_SUPPORT_DOMAIN}",
    "starter": os.getenv("SUPPORT_INBOX_GENERAL")
    or f"support@{_SUPPORT_DOMAIN}",
}

SUPPORT_PRIORITY = {
    "enterprise": "Direct Priority",
    "growth": "Priority Email",
    "pro": "Priority Email",
    "starter": "Basic",
}


def plan_key_from_level(plan_level: Optional[str]) -> str:
    return normalize_plan(plan_level)


def get_plan_level_for_user(user) -> str:
    """Return display plan_level (Starter/Growth/Pro/Enterprise)."""
    if not user:
        return "Starter"
    stored = (getattr(user, "plan_level", None) or "").strip()
    if stored:
        # Normalize casing via catalog.
        return plan_level_label(stored)
    return plan_level_label(getattr(user, "subscription_plan", None))


def get_support_priority_for_plan(plan_level: Optional[str]) -> str:
    key = plan_key_from_level(plan_level)
    return SUPPORT_PRIORITY.get(key, "Basic")


def get_support_priority(user_id: int, db: "Session") -> str:
    """
    Support tier for CRM routing:
      Starter → Basic
      Growth / Pro → Priority Email
      Enterprise → Direct Priority
    """
    from app.database import User

    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user:
        return "Basic"
    if getattr(user, "is_admin", False):
        return "Direct Priority"
    return get_support_priority_for_plan(get_plan_level_for_user(user))


def get_plan_level_by_email(email_address: str, db: "Session") -> str:
    """Look up a user's plan_level by email (case-insensitive)."""
    from app.database import User

    email = (email_address or "").strip().lower()
    if not email or "@" not in email:
        return "Starter"
    user = db.query(User).filter(User.email == email).first()
    if not user:
        return "Starter"
    if getattr(user, "is_admin", False):
        return "Enterprise"
    return get_plan_level_for_user(user)


def support_destination_email(plan_level: Optional[str]) -> str:
    """
    Internal inbox for automated ticket routing:
      Enterprise → vip-support@alphabotix.com
      Growth/Pro → standard-support@alphabotix.com
      Otherwise → general-support@alphabotix.com
    """
    key = plan_key_from_level(plan_level)
    return SUPPORT_INBOX.get(key, SUPPORT_INBOX["starter"])


def support_mailto_for_plan(plan_level: Optional[str]) -> str:
    """
    Public mailto alias (Account → Contact Support):
      Enterprise → enterprise@alphabotservices.com
      Pro        → pro@alphabotservices.com
      Growth     → growth@alphabotservices.com
      Starter    → support@alphabotservices.com
    """
    key = plan_key_from_level(plan_level)
    return SUPPORT_MAILTO.get(key, SUPPORT_MAILTO["starter"])


def support_payload_for_user(user) -> dict:
    level = get_plan_level_for_user(user)
    if user and getattr(user, "is_admin", False):
        level = "Enterprise"
    return {
        "plan_level": level,
        "support_priority": get_support_priority_for_plan(level if not (user and user.is_admin) else "enterprise"),
        "support_mailto": support_mailto_for_plan("enterprise" if (user and user.is_admin) else level),
        "support_destination": support_destination_email(
            "enterprise" if (user and user.is_admin) else level
        ),
    }
