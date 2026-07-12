"""Subscription plan catalog, Stripe Price IDs (test/live), and bot-limit mapping."""

from __future__ import annotations

from typing import Any, Optional

from app.config import STRIPE_ENVIRONMENT

# Billing intervals used by the Weekly / Monthly / Annually toggle.
INTERVALS = ("week", "month", "year")

DEFAULT_PLAN = "starter"

# Marketing display amounts (same in test/live — Stripe Price IDs control billing).
_PLAN_BASE: dict[str, dict[str, Any]] = {
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
            "week": {"amount": "$5", "period": "/week"},
            "month": {"amount": "$20", "period": "/month"},
            "year": {"amount": "$192", "period": "/year"},
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
            "week": {"amount": "$12.50", "period": "/week"},
            "month": {"amount": "$50", "period": "/month"},
            "year": {"amount": "$480", "period": "/year"},
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
            "week": {"amount": "$25", "period": "/week"},
            "month": {"amount": "$100", "period": "/month"},
            "year": {"amount": "$960", "period": "/year"},
        },
    },
}

# Stripe Price IDs by environment. Switch with STRIPE_ENVIRONMENT=test|live.
STRIPE_PRICE_IDS: dict[str, dict[str, dict[str, Optional[str]]]] = {
    "test": {
        "starter": {"week": None, "month": None, "year": None},
        "growth": {
            "week": "price_1Ts4fKRCTdonaQftBbw5XEwg",
            "month": "price_1Ts4fKRCTdonaQftiEgmkm6u",
            "year": "price_1Ts4fKRCTdonaQftGeoZRV9o",
        },
        "pro": {
            "week": "price_1Ts4hpRCTdonaQftgq0usfzD",
            "month": "price_1Ts4h0RCTdonaQftjXVoLHnP",
            "year": "price_1Ts4iLRCTdonaQftCf3rJGMd",
        },
        "enterprise": {
            "week": "price_1Ts4mARCTdonaQftJHaPN40m",
            "month": "price_1Ts4lBRCTdonaQft6w1WU5CX",
            "year": "price_1Ts4mARCTdonaQftwhNLMN8A",
        },
    },
    "live": {
        "starter": {"week": None, "month": None, "year": None},
        "growth": {
            "week": "price_1Ts64fRCTdonaQftjxynDW0P",
            "month": "price_1Ts64fRCTdonaQftrEwQRGWd",
            "year": "price_1Ts64fRCTdonaQfte0yHBo6t",
        },
        "pro": {
            "week": "price_1Ts64jRCTdonaQftwABgNQ4z",
            "month": "price_1Ts64jRCTdonaQfte2PahkYA",
            "year": "price_1Ts64jRCTdonaQft4aZowsTV",
        },
        "enterprise": {
            "week": "price_1Ts64mRCTdonaQft7Fnp3oTy",
            "month": "price_1Ts64mRCTdonaQftdLU962xS",
            "year": "price_1Ts64mRCTdonaQft6d2a0nKF",
        },
    },
}


def _active_env() -> str:
    env = (STRIPE_ENVIRONMENT or "test").strip().lower()
    return env if env in STRIPE_PRICE_IDS else "test"


def active_price_ids() -> dict[str, dict[str, Optional[str]]]:
    return STRIPE_PRICE_IDS[_active_env()]


def _build_catalog() -> dict[str, dict[str, Any]]:
    prices = active_price_ids()
    catalog: dict[str, dict[str, Any]] = {}
    for key, base in _PLAN_BASE.items():
        catalog[key] = {**base, "price_ids": dict(prices.get(key) or {})}
    return catalog


# Rebuilt when imported; call rebuild_plan_catalog() after env changes in tests.
PLAN_CATALOG: dict[str, dict[str, Any]] = {}
PRICE_ID_LOOKUP: dict[str, tuple[str, str]] = {}


def rebuild_plan_catalog() -> None:
    """Refresh PLAN_CATALOG / PRICE_ID_LOOKUP from STRIPE_ENVIRONMENT."""
    global PLAN_CATALOG, PRICE_ID_LOOKUP
    PLAN_CATALOG = _build_catalog()
    lookup: dict[str, tuple[str, str]] = {}
    for plan_key, plan in PLAN_CATALOG.items():
        for interval, pid in (plan.get("price_ids") or {}).items():
            if pid:
                lookup[pid] = (plan_key, interval)
    # Also index the other environment so webhook fulfillments still resolve
    # historical test/live prices after a mode switch.
    for env_key, env_prices in STRIPE_PRICE_IDS.items():
        for plan_key, intervals in env_prices.items():
            for interval, pid in intervals.items():
                if pid and pid not in lookup:
                    lookup[pid] = (plan_key, interval)
    PRICE_ID_LOOKUP = lookup


rebuild_plan_catalog()


def normalize_plan(plan: Optional[str]) -> str:
    key = (plan or DEFAULT_PLAN).strip().lower()
    if key in ("free", "starter"):
        return "starter"
    return key if key in _PLAN_BASE else DEFAULT_PLAN


def plan_level_label(plan: Optional[str], *, is_admin: bool = False) -> str:
    """Canonical support/CRM label: Starter | Growth | Pro | Enterprise."""
    if is_admin:
        return "Enterprise"
    return _PLAN_BASE[normalize_plan(plan)]["name"]


def plan_display_name(plan: Optional[str], *, is_admin: bool = False) -> str:
    if is_admin:
        return "Admin (Unlimited)"
    return _PLAN_BASE[normalize_plan(plan)]["name"]


def bot_limit_for_plan(plan: Optional[str], *, is_admin: bool = False) -> Optional[int]:
    """Max bots for this plan. None = unlimited."""
    if is_admin:
        return None
    meta = _PLAN_BASE[normalize_plan(plan)]
    if meta.get("unlimited"):
        return None
    return int(meta["bots"])


def price_id_for(plan: str, interval: str) -> Optional[str]:
    plan_key = normalize_plan(plan)
    interval_key = (interval or "month").strip().lower()
    if interval_key not in INTERVALS:
        return None
    return active_price_ids().get(plan_key, {}).get(interval_key)


def resolve_plan_from_price_id(price_id: str) -> tuple[str, str]:
    hit = PRICE_ID_LOOKUP.get(price_id or "")
    if hit:
        return hit
    return DEFAULT_PLAN, "month"


def public_plan_payload() -> list[dict[str, Any]]:
    """JSON-serializable plan list for pricing UIs (active environment prices)."""
    rebuild_plan_catalog()
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
            "stripe_environment": _active_env(),
        })
    return out
