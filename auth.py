"""
auth.py — password hashing, session tokens, and email sending.

Kept separate from main.py so main.py stays readable as the "routes"
file. Nothing in here is exotic; it's the same handful of patterns
every small FastAPI app with login uses.
"""

import os
import random
import smtplib
import logging
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from passlib.context import CryptContext
from jose import jwt, JWTError

logger = logging.getLogger("alphabot")

# ── password hashing ────────────────────────────────────────────
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ── session tokens (JWT) ────────────────────────────────────────
JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 24 * 7  # 7 days

if not JWT_SECRET:
    raise RuntimeError(
        "JWT_SECRET is not set in your environment. Generate one with: "
        "python -c \"import secrets; print(secrets.token_hex(32))\" "
        "and add it to your .env file (or Railway environment variables)."
    )


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
        return payload
    except JWTError:
        return None


# ── verification codes ──────────────────────────────────────────
def generate_verification_code() -> str:
    return str(random.randint(100000, 999999))


# ── email sending ───────────────────────────────────────────────
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
ALERT_FROM_EMAIL = os.getenv("ALERT_FROM_EMAIL", SMTP_USERNAME)


def send_email(to_email: str, subject: str, body: str) -> bool:
    """
    Sends a plaintext email via smtplib. Returns True/False rather than
    raising, so a flaky mail server never crashes a trade or a signup.
    """
    if not SMTP_USERNAME or not SMTP_PASSWORD:
        logger.warning("SMTP not configured — skipping email: %s -> %s", subject, to_email)
        return False

    msg = MIMEMultipart()
    msg["From"] = ALERT_FROM_EMAIL
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(ALERT_FROM_EMAIL, to_email, msg.as_string())
        logger.info("Email sent: %s -> %s", subject, to_email)
        return True
    except Exception as e:
        logger.error("Email send failed: %s", e)
        return False
