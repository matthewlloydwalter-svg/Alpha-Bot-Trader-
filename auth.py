import os
import random
import smtplib
import logging
import bcrypt
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from jose import jwt, JWTError
from config import SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD

logger = logging.getLogger("AlphaBot Trading")

PLATFORM_NAME = os.getenv("PLATFORM_NAME", "AlphaBot Trading")
ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}

def send_verification_email(to_email: str, code: str) -> bool:
    if not SMTP_USER or not SMTP_PASSWORD:
        logger.error("SMTP credentials missing.")
        return False
        
    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = to_email
    msg["Subject"] = "Your Verification Code"
    msg.attach(MIMEText(f"Your code is: {code}", "plain"))
    
    try:
        with smtplib.SMTP(SMTP_HOST, int(SMTP_PORT)) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, to_email, msg.as_string())
        return True
    except Exception as e:
        logger.error(f"Email send failed: {e}")
        return False

def is_user_admin(email: str) -> bool:
    return email.strip().lower() in ADMIN_EMAILS
    
def send_email(to_email: str, subject: str, body: str):
    smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", 587))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")

    if not smtp_user or not smtp_password:
        logging.error("SMTP credentials missing. Cannot send email.")
        return

    msg = MIMEMultipart()
    msg["From"] = smtp_user
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "html"))

    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()  # Secure the connection
            server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, to_email, msg.as_string())
            logging.info(f"Email sent successfully to {to_email}")
    except Exception as e:
        logging.error(f"Failed to send email: {e}")

logger = logging.getLogger("AlphaBot Trading")

# ── password hashing (Direct bcrypt) ────────────────────────────
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode('utf-8'), hashed.encode('utf-8'))

# ── session tokens (JWT) ────────────────────────────────────────
JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 24 * 7

if not JWT_SECRET:
    raise RuntimeError("JWT_SECRET is not set.")

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
    if not SMTP_USERNAME or not SMTP_PASSWORD:
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
        return True
    except Exception as e:
        logger.error(f"Email send failed: {e}")
        return False
