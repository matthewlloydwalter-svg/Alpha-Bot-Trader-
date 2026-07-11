"""Subscription plan catalog, Stripe Price IDs, and bot-limit mapping."""

from __future__ import annotations

from typing import Any, Optional

# Billing intervals used by the Weekly / Monthly / Annually toggle.
INTERVALS = ("week", "month", "year")

# Display amounts (marketing). Stripe Price IDs determine the actual charge.
PLAN_CATALOG: dict[str, dict[str, Any]] = {
    "starter": {
        "key": "starter",
        "name": "Starter",
        "bots": 1,
        "unlimited": False,
        "bots_label": "1 Bot",
        "featured": False,
        "free": True,
        "display": {
            "week": {"amount": "Free", "period": ""},
            "month": {"amount": "Free", "period": ""},
            "year": {"amount": "Free", "period": ""},
        },
        "price_ids": {"week": None, "month": None, "year": None},
    },
    "growth": {
        "key": "growth",
        "name": "Growth",
        "bots": 5,
        "unlimited": False,
        "bots_label": "Up to 5 Bots",
        "featured": True,
        "free": False,
        "display": {
            "week": {"amount": "$4.75", "period": "/week"},
            "month": {"amount": "$19", "period": "/month"},
            "year": {"amount": "$182", "period": "/year"},
        },
        "price_ids": {
            "week": "price_1Ts4fKRCTdonaQftBbw5XEwg",
            "month": "price_1Ts4fKRCTdonaQftiEgmkm6u",
            "year": "price_1Ts4fKRCTdonaQftGeoZRV9o",
        },
    },
    "pro": {
        "key": "pro",
        "name": "Pro",
        "bots": 10,
        "unlimited": False,
        "bots_label": "Up to 10 Bots",
        "featured": False,
        "free": False,
        "display": {
            "week": {"amount": "$12.25", "period": "/week"},
            "month": {"amount": "$49", "period": "/month"},
            "year": {"amount": "$470", "period": "/year"},
        },
        "price_ids": {
            "week": "price_1Ts4hpRCTdonaQftgq0usfzD",
            "month": "price_1Ts4h0RCTdonaQftjXVoLHnP",
            "year": "price_1Ts4iLRCTdonaQftCf3rJGMd",
        },
    },
    "enterprise": {
        "key": "enterprise",
        "name": "Enterprise",
        "bots": 25,
        "unlimited": False,
        "bots_label": "Up to 25 Bots",
        "featured": False,
        "free": False,
        "display": {
            "week": {"amount": "$24.75", "period": "/week"},
            "month": {"amount": "$99", "period": "/month"},
            "year": {"amount": "$950", "period": "/year"},
        },
        "price_ids": {
            "week": "price_1Ts4mARCTdonaQftJHaPN40m",
            "month": "price_1Ts4lBRCTdonaQft6w1WU5CX",
            "year": "price_1Ts4mARCTdonaQftwhNLMN8A",
        },
    },
}

DEFAULT_PLAN = "starter"

# Reverse map Stripe price → (plan_key, interval)
PRICE_ID_LOOKUP: dict[str, tuple[str, str]] = {}
for _plan_key, _plan in PLAN_CATALOG.items():
    for _interval, _pid in (_plan.get("price_ids") or {}).items():
        if _pid:
            PRICE_ID_LOOKUP[_pid] = (_plan_key, _interval)


def normalize_plan(plan: Optional[str]) -> str:
    key = (plan or DEFAULT_PLAN).strip().lower()
    if key in ("free", "starter"):
        return "starter"
    return key if key in PLAN_CATALOG else DEFAULT_PLAN


def plan_display_name(plan: Optional[str], *, is_admin: bool = False) -> str:
    if is_admin:
        return "Admin (Unlimited)"
    return PLAN_CATALOG[normalize_plan(plan)]["name"]


def bot_limit_for_plan(plan: Optional[str], *, is_admin: bool = False) -> Optional[int]:
    """Max bots for this plan. None = unlimited."""
    if is_admin:
        return None
    meta = PLAN_CATALOG[normalize_plan(plan)]
    if meta.get("unlimited"):
        return None
    return int(meta["bots"])


def price_id_for(plan: str, interval: str) -> Optional[str]:
    plan_key = normalize_plan(plan)
    interval_key = (interval or "month").strip().lower()
    if interval_key not in INTERVALS:
        return None
    return PLAN_CATALOG[plan_key]["price_ids"].get(interval_key)


def resolve_plan_from_price_id(price_id: str) -> tuple[str, str]:
    hit = PRICE_ID_LOOKUP.get(price_id or "")
    if hit:
        return hit
    return DEFAULT_PLAN, "month"


def public_plan_payload() -> list[dict[str, Any]]:
    """JSON-serializable plan list for pricing UIs."""
    out = []
    for key in ("starter", "growth", "pro", "enterprise"):
        p = PLAN_CATALOG[key]
        out.append({
            "key": p["key"],
            "name": p["name"],
            "bots_label": p["bots_label"],
            "featured": p["featured"],
            "free": p["free"],
            "display": p["display"],
            "price_ids": p["price_ids"],
        })
    return out
