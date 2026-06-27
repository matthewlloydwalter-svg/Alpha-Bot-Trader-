"""
credentials.py — single source of truth for resolving which broker API keys
to use for a given user + broker + trading mode.

Users can store separate Paper and Live key sets per exchange. This module
resolves the right set for the active mode, transparently falling back to the
legacy single-set columns so accounts created before the paper/live split keep
working without re-entering anything.
"""

from __future__ import annotations

import os

from app.crypto import encrypt, decrypt


def _first(*vals):
    """Return the first non-empty value (treats ''/None as empty)."""
    for v in vals:
        if v:
            return v
    return None


# ════════════════════════════════════════════════════════════════════
# MULTI-TENANT KEY SEPARATION
# --------------------------------------------------------------------
# 1. resolve_data_credentials(broker)        -> GLOBAL market-data keys ONLY.
#    Used exclusively by the background poller / chart endpoints to read prices
#    and bars. NEVER used to place orders or read a user's balance.
#
# 2. resolve_trading_credentials(user, ...)  -> the specific USER's keys, pulled
#    dynamically from Postgres. The ONLY credentials allowed for order placement
#    and balance/portfolio checks.
# ════════════════════════════════════════════════════════════════════

def resolve_data_credentials(broker: str) -> dict:
    """
    GLOBAL, read-only market-data credentials. Alpaca data requires keys, so we
    use the platform-wide env keys (ALPACA_DATA_KEY/SECRET). OKX market data is
    public and needs none.

    These keys are scoped to data and must never reach an order/balance call.
    """
    broker = (broker or "alpaca").lower()
    if broker == "alpaca":
        return {
            "alpaca_key": os.getenv("ALPACA_DATA_KEY") or os.getenv("ALPACA_API_KEY"),
            "alpaca_secret": os.getenv("ALPACA_DATA_SECRET") or os.getenv("ALPACA_SECRET_KEY"),
        }
    # OKX (and anything else): public candle access — no keys.
    return {}


def has_data_credentials(broker: str) -> bool:
    creds = resolve_data_credentials(broker)
    if (broker or "alpaca").lower() == "alpaca":
        return bool(creds.get("alpaca_key") and creds.get("alpaca_secret"))
    return True  # OKX public data always available


def resolve_trading_credentials(user, broker: str, paper: bool) -> dict:
    """
    Return the USER's broker credentials for placing orders / reading balance.
    Pulled dynamically from Postgres — never from global env keys.

    Alpaca resolution precedence:
      1. Dedicated, encrypted alpaca_trading_key/secret (explicit trading store).
      2. Per-mode columns (paper vs live) — mode-correct for Alpaca's separate
         paper/live key pairs.
      3. Legacy single-set columns (backward compatibility).
    """
    broker = (broker or "alpaca").lower()
    if broker == "alpaca":
        dedicated_key = decrypt(getattr(user, "alpaca_trading_key", None))
        dedicated_secret = decrypt(getattr(user, "alpaca_trading_secret", None))
        if paper:
            return {
                "alpaca_key": _first(user.alpaca_key_paper, dedicated_key, user.alpaca_key),
                "alpaca_secret": _first(user.alpaca_secret_paper, dedicated_secret, user.alpaca_secret),
            }
        return {
            "alpaca_key": _first(user.alpaca_key_live, dedicated_key, user.alpaca_key),
            "alpaca_secret": _first(user.alpaca_secret_live, dedicated_secret, user.alpaca_secret),
        }
    elif broker == "okx":
        if paper:
            return {
                "okx_key": _first(user.okx_key_paper, user.okx_key),
                "okx_secret": _first(user.okx_secret_paper, user.okx_secret),
                "okx_passphrase": _first(user.okx_pass_paper, user.okx_pass),
            }
        return {
            "okx_key": _first(user.okx_key_live, user.okx_key),
            "okx_secret": _first(user.okx_secret_live, user.okx_secret),
            "okx_passphrase": _first(user.okx_pass_live, user.okx_pass),
        }
    return {}


# Backward-compatible alias. Historically "resolve_credentials" returned the
# user's keys; that role is now explicitly the TRADING resolver.
def resolve_credentials(user, broker: str, paper: bool) -> dict:
    return resolve_trading_credentials(user, broker, paper)


def store_alpaca_trading_credentials(user, api_key: str, secret_key: str) -> None:
    """Persist the user's dedicated Alpaca trading keys, encrypted at rest."""
    user.alpaca_trading_key = encrypt((api_key or "").strip() or None)
    user.alpaca_trading_secret = encrypt((secret_key or "").strip() or None)


def has_credentials(user, broker: str, paper: bool) -> bool:
    creds = resolve_trading_credentials(user, broker, paper)
    if broker == "alpaca":
        return bool(creds.get("alpaca_key") and creds.get("alpaca_secret"))
    if broker == "okx":
        return bool(creds.get("okx_key") and creds.get("okx_secret") and creds.get("okx_passphrase"))
    return False


def keys_payload(user) -> dict:
    """
    Build the structure the Account UI uses to auto-populate the key boxes.
    Legacy single-set keys are surfaced under the 'paper' slot so they remain
    visible/editable after the migration.
    """
    return {
        "alpaca": {
            "paper": {
                "api_key": _first(user.alpaca_key_paper, user.alpaca_key) or "",
                "secret_key": _first(user.alpaca_secret_paper, user.alpaca_secret) or "",
            },
            "live": {
                "api_key": user.alpaca_key_live or "",
                "secret_key": user.alpaca_secret_live or "",
            },
        },
        "okx": {
            "paper": {
                "api_key": _first(user.okx_key_paper, user.okx_key) or "",
                "secret_key": _first(user.okx_secret_paper, user.okx_secret) or "",
                "passphrase": _first(user.okx_pass_paper, user.okx_pass) or "",
            },
            "live": {
                "api_key": user.okx_key_live or "",
                "secret_key": user.okx_secret_live or "",
                "passphrase": user.okx_pass_live or "",
            },
        },
    }
