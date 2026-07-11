"""
credentials.py — resolve / store broker API keys (with optional at-rest encryption).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os

logger = logging.getLogger("alphabot.credentials")

_ENC_PREFIX = "enc:v1:"
_fernet = None


def _get_fernet():
    """Lazy Fernet from KEY_ENCRYPTION_SECRET or JWT_SECRET."""
    global _fernet
    if _fernet is not None:
        return _fernet
    secret = (os.getenv("KEY_ENCRYPTION_SECRET") or os.getenv("JWT_SECRET") or "").strip()
    if not secret or len(secret) < 16:
        _fernet = False  # disabled
        return _fernet
    try:
        from cryptography.fernet import Fernet
        digest = hashlib.sha256(secret.encode("utf-8")).digest()
        key = base64.urlsafe_b64encode(digest)
        _fernet = Fernet(key)
    except Exception as e:
        logger.warning("Key encryption unavailable: %s", e)
        _fernet = False
    return _fernet


def seal_secret(value: str | None) -> str | None:
    """Encrypt a secret for DB storage. No-ops if encryption is disabled or empty."""
    if not value:
        return value
    if value.startswith(_ENC_PREFIX):
        return value
    f = _get_fernet()
    if not f:
        return value
    try:
        return _ENC_PREFIX + f.encrypt(value.encode("utf-8")).decode("utf-8")
    except Exception as e:
        logger.error("seal_secret failed: %s", e)
        return value


def unseal_secret(value: str | None) -> str | None:
    """Decrypt a sealed secret (or return plaintext legacy values as-is)."""
    if not value:
        return value
    if not value.startswith(_ENC_PREFIX):
        return value
    f = _get_fernet()
    if not f:
        logger.error("Encrypted key present but KEY_ENCRYPTION_SECRET/JWT_SECRET unavailable")
        return None
    try:
        return f.decrypt(value[len(_ENC_PREFIX):].encode("utf-8")).decode("utf-8")
    except Exception as e:
        logger.error("unseal_secret failed: %s", e)
        return None


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
    """
    broker = (broker or "alpaca").lower()
    if broker == "alpaca":
        if paper:
            return {
                "alpaca_key": unseal_secret(_first(user.alpaca_key_paper, user.alpaca_key)),
                "alpaca_secret": unseal_secret(_first(user.alpaca_secret_paper, user.alpaca_secret)),
            }
        return {
            "alpaca_key": unseal_secret(_first(user.alpaca_key_live, user.alpaca_key)),
            "alpaca_secret": unseal_secret(_first(user.alpaca_secret_live, user.alpaca_secret)),
        }
    elif broker == "okx":
        if paper:
            return {
                "okx_key": unseal_secret(_first(user.okx_key_paper, user.okx_key)),
                "okx_secret": unseal_secret(_first(user.okx_secret_paper, user.okx_secret)),
                "okx_passphrase": unseal_secret(_first(user.okx_pass_paper, user.okx_pass)),
            }
        return {
            "okx_key": unseal_secret(_first(user.okx_key_live, user.okx_key)),
            "okx_secret": unseal_secret(_first(user.okx_secret_live, user.okx_secret)),
            "okx_passphrase": unseal_secret(_first(user.okx_pass_live, user.okx_pass)),
        }
    return {}


def has_credentials(user, broker: str, paper: bool) -> bool:
    creds = resolve_credentials(user, broker, paper)
    if broker == "alpaca":
        return bool(creds.get("alpaca_key") and creds.get("alpaca_secret"))
    if broker == "okx":
        return bool(creds.get("okx_key") and creds.get("okx_secret") and creds.get("okx_passphrase"))
    return False


def mask_secret(value: str | None) -> str:
    """Return a masked form for UI/API display (never echo full secrets)."""
    plain = unseal_secret(value) if value else ""
    if not plain:
        return ""
    if len(plain) <= 8:
        return "••••••••"
    return plain[:4] + "••••••••" + plain[-4:]


def keys_payload(user, *, mask: bool = True) -> dict:
    """
    Build the structure the Account UI uses for key boxes.
    By default secrets are masked; pass mask=False only for trusted admin tooling.
    """
    def _show(v):
        if not v:
            return ""
        return mask_secret(v) if mask else (unseal_secret(v) or "")

    return {
        "alpaca": {
            "paper": {
                "api_key": _show(_first(user.alpaca_key_paper, user.alpaca_key)),
                "secret_key": _show(_first(user.alpaca_secret_paper, user.alpaca_secret)),
                "configured": bool(_first(user.alpaca_key_paper, user.alpaca_key)),
            },
            "live": {
                "api_key": _show(_first(user.alpaca_key_live, user.alpaca_key)),
                "secret_key": _show(_first(user.alpaca_secret_live, user.alpaca_secret)),
                "configured": bool(_first(user.alpaca_key_live)),
            },
        },
        "okx": {
            "paper": {
                "api_key": _show(_first(user.okx_key_paper, user.okx_key)),
                "secret_key": _show(_first(user.okx_secret_paper, user.okx_secret)),
                "passphrase": _show(_first(user.okx_pass_paper, user.okx_pass)),
                "configured": bool(_first(user.okx_key_paper, user.okx_key)),
            },
            "live": {
                "api_key": _show(_first(user.okx_key_live, user.okx_key)),
                "secret_key": _show(_first(user.okx_secret_live, user.okx_secret)),
                "passphrase": _show(_first(user.okx_pass_live, user.okx_pass)),
                "configured": bool(_first(user.okx_key_live)),
            },
        },
    }
