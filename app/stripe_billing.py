"""
Secure server-side Stripe subscription helpers (Python Stripe SDK).

Equivalent to the Stripe Ruby gem flow:
  - Checkout Session created only on the server (mode=subscription)
  - Client may send price_id or lookup_key — never amounts/line_items
  - Billing Portal for self-serve cancel/update
  - Webhook signature verification via STRIPE_WEBHOOK_SECRET
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from app.config import STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, PUBLIC_BASE_URL
from app.plans import (
    PRICE_ID_LOOKUP,
    normalize_plan,
    resolve_plan_from_price_id,
)

logger = logging.getLogger("alphabot.stripe")

# Server-side allowlist — only these Price IDs may be used in Checkout.
ALLOWED_PRICE_IDS = frozenset(PRICE_ID_LOOKUP.keys())

# Optional lookup_key → price_id map (Stripe Dashboard lookup keys).
# Keys are also accepted from the client and resolved server-side.
LOOKUP_KEY_TO_PRICE: dict[str, str] = {
    f"{plan}_{interval}": price_id
    for price_id, (plan, interval) in PRICE_ID_LOOKUP.items()
}


class BillingError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _stripe():
    try:
        import stripe
    except ImportError as exc:
        raise BillingError(
            "Stripe Python package is not installed. Add `stripe` to requirements and redeploy.",
            status_code=503,
        ) from exc
    return stripe


def stripe_configured() -> bool:
    return bool(STRIPE_SECRET_KEY)


def _client():
    if not STRIPE_SECRET_KEY:
        raise BillingError(
            "Stripe is not configured. Set STRIPE_SECRET_KEY on the server.",
            status_code=503,
        )
    stripe = _stripe()
    stripe.api_key = STRIPE_SECRET_KEY
    return stripe


def _abs_url(path: str, public_base_url: Optional[str] = None) -> str:
    base = (public_base_url or PUBLIC_BASE_URL or "").rstrip("/")
    if not base:
        base = "http://127.0.0.1:8000"
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def _epoch_to_naive_utc(epoch: Any) -> Optional[datetime]:
    try:
        sec = int(epoch)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(sec, tz=timezone.utc).replace(tzinfo=None)


def ensure_customer(user) -> str:
    """Return Stripe customer id, creating one if needed."""
    stripe = _client()
    existing = (getattr(user, "stripe_customer_id", None) or "").strip()
    if existing:
        return existing
    customer = stripe.Customer.create(
        email=user.email,
        metadata={"user_id": str(user.id)},
    )
    user.stripe_customer_id = customer["id"]
    return customer["id"]


def resolve_allowed_price_id(
    *,
    price_id: Optional[str] = None,
    lookup_key: Optional[str] = None,
) -> tuple[str, str, str]:
    """
    Resolve and authorize a Price ID from client input.
    Returns (price_id, plan_key, interval).
    Never trusts client-supplied amounts — only allowlisted IDs / lookup keys.
    """
    pid = (price_id or "").strip()
    key = (lookup_key or "").strip()

    if not pid and key:
        # Prefer local map; fall back to Stripe lookup_keys API then re-check allowlist.
        pid = LOOKUP_KEY_TO_PRICE.get(key) or ""
        if not pid:
            stripe = _client()
            prices = stripe.Price.list(lookup_keys=[key], active=True, limit=1)
            data = prices.get("data") or []
            if data:
                pid = data[0].get("id") or ""

    if not pid:
        raise BillingError("Provide a price_id or lookup_key.")

    if pid not in ALLOWED_PRICE_IDS:
        raise BillingError("Unrecognized or unauthorized Stripe price_id.", status_code=400)

    plan, interval = resolve_plan_from_price_id(pid)
    return pid, plan, interval


def create_checkout_session(
    user,
    *,
    price_id: Optional[str] = None,
    lookup_key: Optional[str] = None,
    public_base_url: Optional[str] = None,
) -> dict[str, Any]:
    """
    Create a Stripe Checkout Session (mode=subscription).
    line_items are defined only on the server from an allowlisted price_id.
    """
    stripe = _client()
    pid, plan_key, interval_key = resolve_allowed_price_id(
        price_id=price_id, lookup_key=lookup_key,
    )
    customer_id = ensure_customer(user)

    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": pid, "quantity": 1}],
        success_url=_abs_url(
            "/checkout/success?session_id={CHECKOUT_SESSION_ID}",
            public_base_url,
        ),
        cancel_url=_abs_url("/upgrade-plans?canceled=1", public_base_url),
        client_reference_id=str(user.id),
        metadata={
            "user_id": str(user.id),
            "plan": plan_key,
            "interval": interval_key,
            "price_id": pid,
        },
        subscription_data={
            "metadata": {
                "user_id": str(user.id),
                "plan": plan_key,
                "interval": interval_key,
                "price_id": pid,
            },
        },
        allow_promotion_codes=True,
    )
    url = session.get("url")
    if not url:
        raise BillingError("Stripe did not return a Checkout URL.", status_code=502)
    return {
        "url": url,
        "session_id": session.get("id"),
        "mode": "checkout",
        "plan": plan_key,
        "interval": interval_key,
    }


def create_billing_portal_session(
    user,
    *,
    public_base_url: Optional[str] = None,
) -> dict[str, Any]:
    """Create a Stripe Customer Billing Portal session (manage / cancel)."""
    stripe = _client()
    customer_id = (getattr(user, "stripe_customer_id", None) or "").strip()
    if not customer_id:
        # Create a customer so portal can still open (empty invoices until they subscribe).
        customer_id = ensure_customer(user)
    portal = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=_abs_url("/upgrade-plans", public_base_url),
    )
    url = portal.get("url")
    if not url:
        raise BillingError("Stripe did not return a Billing Portal URL.", status_code=502)
    return {"url": url}


def apply_plan_to_user(
    user,
    plan: str,
    interval: str,
    *,
    status: str = "active",
    customer_id: Optional[str] = None,
    subscription_id: Optional[str] = None,
    current_period_end: Any = None,
) -> None:
    user.subscription_plan = normalize_plan(plan)
    user.subscription_interval = (interval or "month").strip().lower()
    user.subscription_status = status
    if customer_id:
        user.stripe_customer_id = customer_id
    if subscription_id:
        user.stripe_subscription_id = subscription_id
    end = _epoch_to_naive_utc(current_period_end) if current_period_end is not None else None
    if end is not None:
        user.subscription_current_period_end = end
    elif status in {"canceled", "unpaid", "incomplete_expired"}:
        user.subscription_current_period_end = None


def _period_end_from_subscription(subscription: dict) -> Any:
    return subscription.get("current_period_end")


def sync_user_from_checkout_session(user, session: dict, *, stripe_client=None) -> None:
    """Fulfill checkout.session.completed — set active + plan + period end."""
    stripe = stripe_client or _client()
    meta = session.get("metadata") or {}
    plan = meta.get("plan")
    interval = meta.get("interval")
    price_hint = meta.get("price_id") or ""
    if price_hint and price_hint in ALLOWED_PRICE_IDS:
        plan, interval = resolve_plan_from_price_id(price_hint)
    plan = plan or "starter"
    interval = interval or "month"

    period_end = None
    sub_id = session.get("subscription")
    if sub_id:
        try:
            sub = stripe.Subscription.retrieve(sub_id)
            period_end = _period_end_from_subscription(sub)
            # Prefer live price on the subscription item.
            items = (sub.get("items") or {}).get("data") or []
            if items:
                pid = ((items[0].get("price") or {}).get("id")) or ""
                if pid in ALLOWED_PRICE_IDS:
                    plan, interval = resolve_plan_from_price_id(pid)
        except Exception as exc:
            logger.warning("Could not retrieve subscription %s: %s", sub_id, exc)

    apply_plan_to_user(
        user,
        plan,
        interval,
        status="active",
        customer_id=session.get("customer"),
        subscription_id=sub_id,
        current_period_end=period_end,
    )


def sync_user_from_subscription(user, subscription: dict) -> None:
    """Fulfill customer.subscription.updated / deleted."""
    status = subscription.get("status") or "active"
    items = (subscription.get("items") or {}).get("data") or []
    price_id = ""
    if items:
        price = items[0].get("price") or {}
        price_id = price.get("id") or ""
    meta = subscription.get("metadata") or {}
    if price_id and price_id in ALLOWED_PRICE_IDS:
        plan, interval = resolve_plan_from_price_id(price_id)
    else:
        plan = meta.get("plan") or getattr(user, "subscription_plan", None) or "starter"
        interval = meta.get("interval") or getattr(user, "subscription_interval", None) or "month"

    period_end = _period_end_from_subscription(subscription)

    if status in {"canceled", "unpaid", "incomplete_expired"}:
        apply_plan_to_user(
            user,
            "starter",
            "month",
            status=status,
            customer_id=subscription.get("customer"),
            subscription_id=subscription.get("id"),
            current_period_end=None,
        )
    else:
        # Normalize Stripe's "active"/"trialing" into access-granting status.
        access_status = "active" if status in {"active", "trialing"} else status
        apply_plan_to_user(
            user,
            plan,
            interval,
            status=access_status,
            customer_id=subscription.get("customer"),
            subscription_id=subscription.get("id"),
            current_period_end=period_end,
        )


def construct_webhook_event(payload: bytes, sig_header: str):
    stripe = _client()
    if not STRIPE_WEBHOOK_SECRET:
        raise BillingError("STRIPE_WEBHOOK_SECRET is not configured.", status_code=503)
    try:
        return stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as exc:
        raise BillingError(f"Invalid Stripe webhook: {exc}", status_code=400) from exc


def find_user_for_stripe_object(db, data: dict, User):
    """Locate the local user for a Checkout Session or Subscription object."""
    meta = data.get("metadata") or {}
    uid = meta.get("user_id") or data.get("client_reference_id")
    if uid:
        try:
            user = db.query(User).filter(User.id == int(uid)).first()
            if user:
                return user
        except (TypeError, ValueError):
            pass
    sub_id = data.get("id") if str(data.get("object") or "") == "subscription" else data.get("subscription")
    # For checkout sessions, subscription is an id string; for subscription events, id is the sub.
    if data.get("object") == "subscription":
        sub_id = data.get("id")
    if sub_id:
        user = db.query(User).filter(User.stripe_subscription_id == sub_id).first()
        if user:
            return user
    customer = data.get("customer")
    if customer:
        return db.query(User).filter(User.stripe_customer_id == customer).first()
    return None
