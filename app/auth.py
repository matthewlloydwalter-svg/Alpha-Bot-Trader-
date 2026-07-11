import os
import random
import logging
import bcrypt
from datetime import datetime, timedelta
from jose import jwt, JWTError

logger = logging.getLogger("AlphaBotix Trading")

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session
from app.database import User, get_db
from app.email_service import (
    EmailError,
    send_verification_email as _send_verification_email,
    send_password_reset_email as _send_password_reset_email,
)

# Re-export so existing `from app.auth import EmailError, send_verification_email` keeps working.
__all__ = [
    "EmailError",
    "send_verification_email",
    "send_password_reset_email",
    "get_current_user",
    "hash_password",
    "verify_password",
    "is_user_admin",
    "create_session_token",
    "decode_session_token",
    "create_password_reset_token",
    "decode_password_reset_token",
    "generate_verification_code",
    "PLATFORM_NAME",
    "ADMIN_EMAILS",
    "JWT_SECRET",
]


def get_current_user(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("session_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    payload = decode_session_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    # We use .get("sub") because that's the key we used in create_session_token.
    # A tampered/malformed token may carry a missing/non-numeric sub — treat that
    # as unauthenticated (401) rather than letting int() raise an unhandled 500.
    try:
        user_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user

PLATFORM_NAME = os.getenv("PLATFORM_NAME", "AlphaBotix Trading")
ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}

_raw_jwt = (os.getenv("JWT_SECRET") or "").strip()
_WEAK_JWT_DEFAULTS = {
    "",
    "super-secure-fallback-secret-key-12345!",
    "dev-local-secret-key-change-me",
    "change-me",
    "secret",
}
_ENV = (os.getenv("ENV") or os.getenv("APP_ENV") or "development").strip().lower()
_ALLOW_WEAK_JWT = (os.getenv("ALLOW_WEAK_JWT") or "").strip().lower() in {"1", "true", "yes", "on"}

if _raw_jwt in _WEAK_JWT_DEFAULTS or len(_raw_jwt) < 24:
    if _ENV in {"production", "prod", "railway"} or (
        not _ALLOW_WEAK_JWT and _ENV not in {"development", "dev", "test", "local"}
    ):
        raise RuntimeError(
            "JWT_SECRET is missing or too weak for production. "
            "Set a unique secret of at least 24 characters in the environment."
        )
    # Local/dev only: keep a deterministic fallback so the app can boot.
    JWT_SECRET = _raw_jwt or "super-secure-fallback-secret-key-12345!"
    import logging as _logging
    _logging.getLogger("AlphaBotix Trading").warning(
        "JWT_SECRET is weak or unset — acceptable for local dev only."
    )
else:
    JWT_SECRET = _raw_jwt
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 24

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Defensive check ensures text, explicit bytes, or stringified raw byte arrays evaluate cleanly."""
    if not hashed_password:
        return False
    try:
        # Handle cases where database wrote bytes out directly as literal strings.
        # NOTE: parentheses matter — the original lacked them, so the b'/b" check
        # mis-evaluated due to `and`/`or` precedence.
        if isinstance(hashed_password, str) and (
            hashed_password.startswith("b'") or hashed_password.startswith('b"')
        ):
            hashed_password = hashed_password[2:-1]

        if isinstance(hashed_password, str):
            hashed_password = hashed_password.encode('utf-8')

        return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password)
    except Exception as e:
        logger.error(f"Password runtime validation matrix evaluation fault: {e}")
        return False


def send_verification_email(to_email: str, code: str) -> bool:
    """Send the verification code via Resend (see app.email_service)."""
    return _send_verification_email(to_email, code, platform_name=PLATFORM_NAME)


def send_password_reset_email(to_email: str, code: str) -> bool:
    """Send a password-reset code via the same Resend configuration."""
    return _send_password_reset_email(to_email, code, platform_name=PLATFORM_NAME)

def is_user_admin(email: str) -> bool:
    return email.strip().lower() in ADMIN_EMAILS

def create_session_token(user_id: int, email: str) -> str:
    payload = {
        "sub": str(user_id),
        "email": email,
        "exp": datetime.utcnow() + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def decode_session_token(token: str):
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None
    # Password-reset JWTs share the signing key but must never authenticate a session.
    if isinstance(payload, dict) and payload.get("purpose") == "password_reset":
        return None
    return payload


PASSWORD_RESET_EXPIRE_MINUTES = 15


def create_password_reset_token(user_id: int, email: str) -> str:
    """Short-lived token issued only after a correct email verification code."""
    payload = {
        "sub": str(user_id),
        "email": email,
        "purpose": "password_reset",
        "exp": datetime.utcnow() + timedelta(minutes=PASSWORD_RESET_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_password_reset_token(token: str):
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None
    if not payload or payload.get("purpose") != "password_reset":
        return None
    return payload

def generate_verification_code() -> str:
    return str(random.randint(100000, 999999))
