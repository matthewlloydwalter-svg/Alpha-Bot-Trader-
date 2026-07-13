import os
import random
import logging
import bcrypt
from datetime import datetime, timedelta
from jose import jwt, JWTError

logger = logging.getLogger("AlphaBotix Trading")

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from app.database import User, get_db
from app.email_service import (
    EmailError,
    send_verification_email as _send_verification_email,
    send_password_reset_email as _send_password_reset_email,
)
from app.config import (
    ADSENSE_AUTH_BYPASS,
    ADSENSE_GUEST_EMAIL,
    ADSENSE_GUEST_NAME,
    ADSENSE_GUEST_BALANCE,
)

# Re-export so existing `from app.auth import EmailError, send_verification_email` keeps working.
__all__ = [
    "EmailError",
    "send_verification_email",
    "send_password_reset_email",
    "get_current_user",
    "get_or_create_adsense_guest_user",
    "is_adsense_guest_user",
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


def is_adsense_guest_user(user: User | None) -> bool:
    """True only for the dedicated AdSense mock profile (never a real account)."""
    if not user or not getattr(user, "email", None):
        return False
    return str(user.email).strip().lower() == ADSENSE_GUEST_EMAIL.lower()


def get_or_create_adsense_guest_user(db: Session) -> User:
    """
    TODO: REVERT THIS AFTER 60 DAYS TO RE-ENABLE LOGIN WALL

    Return a safe mock GUEST user for AdSense crawlers. Always keyed by
    ADSENSE_GUEST_EMAIL — never selects / returns a real customer profile.
    """
    email = ADSENSE_GUEST_EMAIL.strip().lower()
    user = db.query(User).filter(User.email == email).first()
    if user:
        # Harden on every lookup: guest must never become admin or carry live keys.
        dirty = False
        if user.is_admin:
            user.is_admin = False
            dirty = True
        if not user.email_verified:
            user.email_verified = True
            dirty = True
        if (user.name or "") != ADSENSE_GUEST_NAME:
            user.name = ADSENSE_GUEST_NAME
            dirty = True
        if float(user.total_deposited or 0) != float(ADSENSE_GUEST_BALANCE):
            user.total_deposited = float(ADSENSE_GUEST_BALANCE)
            dirty = True
        # Strip any broker secrets so reviewers never see real credentials.
        for col in (
            "alpaca_key", "alpaca_secret", "okx_key", "okx_secret", "okx_pass",
            "alpaca_key_paper", "alpaca_secret_paper", "alpaca_key_live", "alpaca_secret_live",
            "okx_key_paper", "okx_secret_paper", "okx_pass_paper",
            "okx_key_live", "okx_secret_live", "okx_pass_live",
        ):
            if getattr(user, col, None):
                setattr(user, col, None)
                dirty = True
        if dirty:
            db.add(user)
            db.commit()
            db.refresh(user)
        return user

    # Unusable random password — guest is session-bypass only, not a login target.
    import secrets as _secrets
    guest = User(
        name=ADSENSE_GUEST_NAME,
        email=email,
        hashed_password=hash_password(_secrets.token_urlsafe(32)),
        is_admin=False,
        email_verified=True,
        trading_mode="paper",
        active_broker="alpaca",
        total_deposited=float(ADSENSE_GUEST_BALANCE),
        total_withdrawn=0.0,
        subscription_plan="growth",
        plan_level="Growth",
    )
    try:
        db.add(guest)
        db.commit()
        db.refresh(guest)
        return guest
    except IntegrityError:
        db.rollback()
        existing = db.query(User).filter(User.email == email).first()
        if existing:
            return existing
        raise


def _resolve_session_user(request: Request, db: Session, *, missing_detail: str) -> User:
    """Shared cookie → User resolution used by both auth dependency entry points."""
    token = request.cookies.get("session_token")
    if not token:
        raise HTTPException(status_code=401, detail=missing_detail)

    payload = decode_session_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail=missing_detail)

    # We use .get("sub") because that's the key we used in create_session_token.
    # A tampered/malformed token may carry a missing/non-numeric sub — treat that
    # as unauthenticated (401) rather than letting int() raise an unhandled 500.
    try:
        user_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail=missing_detail)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail=missing_detail)
    try:
        token_sv = int(payload.get("sv", 0))
    except (TypeError, ValueError):
        token_sv = 0
    if token_sv != int(user.session_version or 0):
        raise HTTPException(status_code=401, detail=missing_detail)
    return user


def get_current_user(request: Request, db: Session = Depends(get_db)):
    # TODO: REVERT THIS AFTER 60 DAYS TO RE-ENABLE LOGIN WALL
    # AdSense review: prefer a real session when present; otherwise serve mock guest.
    if ADSENSE_AUTH_BYPASS:
        try:
            return _resolve_session_user(request, db, missing_detail="Not authenticated")
        except HTTPException:
            return get_or_create_adsense_guest_user(db)

    return _resolve_session_user(request, db, missing_detail="Not authenticated")

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

def create_session_token(user_id: int, email: str, session_version: int = 0) -> str:
    payload = {
        "sub": str(user_id),
        "email": email,
        "sv": int(session_version or 0),
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
