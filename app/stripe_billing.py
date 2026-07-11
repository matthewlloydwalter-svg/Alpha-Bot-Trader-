"""Stripe Checkout + webhook helpers for subscription upgrades."""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.config import STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, PUBLIC_BASE_URL
from app.plans import normalize_plan, price_id_for, resolve_plan_from_price_id

logger = logging.getLogger("alphabot.stripe")


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


def _absolute(path: str) -> str:
    base = (PUBLIC_BASE_URL or "").rstrip("/")
    if not base:
        base = "http://127.0.0.1:8000"
    if not path.startswith("/"):
        path = "/" + path
    return base + path


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


def start_checkout_or_upgrade(user, plan: str, interval: str, *, public_base_url: Optional[str] = None) -> dict[str, Any]:
    """
    Start Stripe Checkout for a new subscription, or modify an existing one.
    Returns {"url": "..."} for the browser to navigate to.
    """
    stripe = _client()
    plan_key = normalize_plan(plan)
    interval_key = (interval or "month").strip().lower()
    if plan_key == "starter":
        raise BillingError("Starter is free — no checkout required.")
    price_id = price_id_for(plan_key, interval_key)
    if not price_id:
        raise BillingError("Unknown plan or billing interval.")

    global_base = (public_base_url or "").rstrip("/")
    if global_base:
        def abs_url(path: str) -> str:
            if not path.startswith("/"):
                path = "/" + path
            return global_base + path
    else:
        abs_url = _absolute

    customer_id = ensure_customer(user)
    sub_id = (getattr(user, "stripe_subscription_id", None) or "").strip()

    if sub_id and (getattr(user, "subscription_status", None) or "") in {
        "active", "trialing", "past_due",
    }:
        try:
            sub = stripe.Subscription.retrieve(sub_id)
            items = (sub.get("items") or {}).get("data") or []
            if items:
                item_id = items[0]["id"]
                stripe.Subscription.modify(
                    sub_id,
                    items=[{"id": item_id, "price": price_id}],
                    proration_behavior="create_prorations",
                    metadata={
                        "user_id": str(user.id),
                        "plan": plan_key,
                        "interval": interval_key,
                    },
                )
                user.subscription_plan = plan_key
                user.subscription_interval = interval_key
                user.subscription_status = "active"
                return {
                    "url": abs_url("/upgrade-plans?upgraded=1"),
                    "mode": "updated",
                }
        except Exception as exc:
            logger.warning("Subscription modify failed, falling back to Checkout: %s", exc)

    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=abs_url("/upgrade-plans?success=1&session_id={CHECKOUT_SESSION_ID}"),
        cancel_url=abs_url("/upgrade-plans?canceled=1"),
        client_reference_id=str(user.id),
        metadata={
            "user_id": str(user.id),
            "plan": plan_key,
            "interval": interval_key,
        },
        subscription_data={
            "metadata": {
                "user_id": str(user.id),
                "plan": plan_key,
                "interval": interval_key,
            },
        },
        allow_promotion_codes=True,
    )
    return {"url": session["url"], "mode": "checkout"}


def apply_plan_to_user(user, plan: str, interval: str, *, status: str = "active",
                       customer_id: Optional[str] = None,
                       subscription_id: Optional[str] = None) -> None:
    user.subscription_plan = normalize_plan(plan)
    user.subscription_interval = (interval or "month").strip().lower()
    user.subscription_status = status
    if customer_id:
        user.stripe_customer_id = customer_id
    if subscription_id:
        user.stripe_subscription_id = subscription_id


def sync_user_from_checkout_session(user, session: dict) -> None:
    meta = session.get("metadata") or {}
    plan = meta.get("plan") or "starter"
    interval = meta.get("interval") or "month"
    apply_plan_to_user(
        user,
        plan,
        interval,
        status="active",
        customer_id=session.get("customer"),
        subscription_id=session.get("subscription"),
    )


def sync_user_from_subscription(user, subscription: dict) -> None:
    status = subscription.get("status") or "active"
    items = (subscription.get("items") or {}).get("data") or []
    price_id = ""
    if items:
        price = items[0].get("price") or {}
        price_id = price.get("id") or ""
    meta = subscription.get("metadata") or {}
    if price_id:
        plan, interval = resolve_plan_from_price_id(price_id)
    else:
        plan = meta.get("plan") or getattr(user, "subscription_plan", None) or "starter"
        interval = meta.get("interval") or getattr(user, "subscription_interval", None) or "month"

    if status in {"canceled", "unpaid", "incomplete_expired"}:
        apply_plan_to_user(
            user,
            "starter",
            "month",
            status=status,
            customer_id=subscription.get("customer"),
            subscription_id=subscription.get("id"),
        )
    else:
        apply_plan_to_user(
            user,
            plan,
            interval,
            status=status,
            customer_id=subscription.get("customer"),
            subscription_id=subscription.get("id"),
        )


def construct_webhook_event(payload: bytes, sig_header: str):
    stripe = _client()
    if not STRIPE_WEBHOOK_SECRET:
        raise BillingError("STRIPE_WEBHOOK_SECRET is not configured.", status_code=503)
    try:
        return stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as exc:
        raise BillingError(f"Invalid Stripe webhook: {exc}", status_code=400) from exc
