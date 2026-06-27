import os
import random
import smtplib
import logging
import bcrypt
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from jose import jwt, JWTError
from app.config import SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD

logger = logging.getLogger("AlphaBot Trading")

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session
from app.database import User, get_db

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

PLATFORM_NAME = os.getenv("PLATFORM_NAME", "AlphaBot Trading")
ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}

JWT_SECRET = os.getenv("JWT_SECRET", "super-secure-fallback-secret-key-12345!")
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


class EmailError(Exception):
    """Raised with a human-readable reason when an email cannot be sent."""
    pass


def send_verification_email(to_email: str, code: str) -> bool:
    """
    Send the verification code. Raises EmailError with a SPECIFIC reason on
    failure so the API can surface what actually went wrong (the old code
    swallowed everything into one opaque message).

    Gmail notes:
      - Use an *App Password* (16 chars), not your normal password, with 2FA on.
      - Host smtp.gmail.com, port 587 (STARTTLS) or 465 (implicit SSL).
    """
    if not SMTP_USER or not SMTP_PASSWORD:
        raise EmailError(
            "Email is not configured on the server. Set SMTP_HOST, SMTP_PORT, "
            "SMTP_USER and SMTP_PASSWORD (Gmail App Password) in your environment/Railway variables."
        )

    host = SMTP_HOST or "smtp.gmail.com"
    try:
        port = int(SMTP_PORT) if SMTP_PORT else 587
    except (TypeError, ValueError):
        port = 587

    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = to_email
    msg["Subject"] = f"[{PLATFORM_NAME}] Your verification code"
    msg.attach(MIMEText(
        f"Your {PLATFORM_NAME} verification code is: {code}\n\n"
        "If you did not request this, you can ignore this email.",
        "plain",
    ))

    try:
        if port == 465:
            # Implicit TLS.
            with smtplib.SMTP_SSL(host, port, timeout=20) as server:
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_USER, [to_email], msg.as_string())
        else:
            # STARTTLS (587 and most others).
            with smtplib.SMTP(host, port, timeout=20) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_USER, [to_email], msg.as_string())
        logger.info("Verification email sent to %s via %s:%s", to_email, host, port)
        return True
    except smtplib.SMTPAuthenticationError as e:
        logger.error("SMTP auth failed: %s", e)
        raise EmailError(
            "SMTP authentication failed. For Gmail you must use a 16-character App Password "
            "(Google Account → Security → App passwords) with 2-Step Verification enabled — "
            "your normal Gmail password will not work."
        )
    except (smtplib.SMTPException, OSError) as e:
        logger.error("Mail delivery failed (%s:%s): %s", host, port, e)
        raise EmailError(f"Could not reach the mail server {host}:{port} — {e}")

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
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None

def generate_verification_code() -> str:
    return str(random.randint(100000, 999999))
