"""
credentials.py — single source of truth for resolving which broker API keys
to use for a given user + broker + trading mode.

Users can store separate Paper and Live key sets per exchange. This module
resolves the right set for the active mode, transparently falling back to the
legacy single-set columns so accounts created before the paper/live split keep
working without re-entering anything.
"""

from __future__ import annotations


def _first(*vals):
    """Return the first non-empty value (treats ''/None as empty)."""
    for v in vals:
        if v:
            return v
    return None


def resolve_credentials(user, broker: str, paper: bool) -> dict:
    """
    Return the broker credentials for the given mode.

    For Alpaca: {"alpaca_key", "alpaca_secret"}
    For OKX:    {"okx_key", "okx_secret", "okx_passphrase"}

    Falls back to legacy columns (alpaca_key/okx_key/...) when a mode-specific
    value is missing — this is what makes a user who only ever entered "paper"
    keys in the old UI keep working in paper mode.
    """
    broker = (broker or "alpaca").lower()
    if broker == "alpaca":
        if paper:
            return {
                "alpaca_key": _first(user.alpaca_key_paper, user.alpaca_key),
                "alpaca_secret": _first(user.alpaca_secret_paper, user.alpaca_secret),
            }
        return {
            "alpaca_key": _first(user.alpaca_key_live, user.alpaca_key),
            "alpaca_secret": _first(user.alpaca_secret_live, user.alpaca_secret),
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


def has_credentials(user, broker: str, paper: bool) -> bool:
    creds = resolve_credentials(user, broker, paper)
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
