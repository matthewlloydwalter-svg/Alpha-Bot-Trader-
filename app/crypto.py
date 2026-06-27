"""
crypto.py — at-rest encryption for sensitive per-user secrets (trading keys).

On a public, multi-tenant platform user broker keys must never sit in the
database as plaintext. When ``APP_ENCRYPTION_KEY`` (any passphrase) is set in
the environment we derive a stable Fernet key from it and transparently encrypt
the dedicated trading-key columns. Ciphertext is tagged with an ``enc::`` prefix
so reads can tell encrypted values from legacy plaintext and migrate safely.

If no key is configured the functions degrade to plaintext pass-through (with a
one-time warning) so local/dev still works — production should always set
``APP_ENCRYPTION_KEY``.
"""

from __future__ import annotations

import os
import base64
import hashlib
import logging

logger = logging.getLogger("alphabot.crypto")

_PREFIX = "enc::"
_warned = False


def _fernet():
    global _warned
    secret = os.getenv("APP_ENCRYPTION_KEY") or os.getenv("ENCRYPTION_KEY")
    if not secret:
        if not _warned:
            logger.warning(
                "APP_ENCRYPTION_KEY is not set — trading keys will be stored as "
                "plaintext. Set it in your Railway variables for at-rest encryption."
            )
            _warned = True
        return None
    try:
        from cryptography.fernet import Fernet
    except Exception as e:  # pragma: no cover
        logger.warning("cryptography unavailable (%s) — storing plaintext.", e)
        return None
    # Derive a valid 32-byte urlsafe-base64 Fernet key from any passphrase.
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    from cryptography.fernet import Fernet as _F
    return _F(key)


def encryption_enabled() -> bool:
    return _fernet() is not None


def encrypt(plaintext: str | None) -> str | None:
    if not plaintext:
        return plaintext
    if plaintext.startswith(_PREFIX):
        return plaintext  # already encrypted
    f = _fernet()
    if f is None:
        return plaintext  # plaintext fallback
    return _PREFIX + f.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(value: str | None) -> str | None:
    if not value:
        return value
    if not value.startswith(_PREFIX):
        return value  # legacy plaintext
    f = _fernet()
    if f is None:
        logger.error("Encrypted value present but APP_ENCRYPTION_KEY missing — cannot decrypt.")
        return None
    try:
        from cryptography.fernet import InvalidToken
        return f.decrypt(value[len(_PREFIX):].encode("utf-8")).decode("utf-8")
    except Exception as e:  # InvalidToken or others
        logger.error("Failed to decrypt a stored secret: %s", e)
        return None
