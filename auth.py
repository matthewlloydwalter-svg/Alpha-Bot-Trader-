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
        # Handle cases where database wrote bytes out directly as literal strings
        if isinstance(hashed_password, str) and hashed_password.startswith("b'") or hashed_password.startswith('b"'):
            hashed_password = hashed_password[2:-1]
            
        if isinstance(hashed_password, str):
            hashed_password = hashed_password.encode('utf-8')
            
        return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password)
    except Exception as e:
        logger.error(f"Password runtime validation matrix evaluation fault: {e}")
        return False

def send_verification_email(to_email: str, code: str) -> bool:
    if not SMTP_USER or not SMTP_PASSWORD:
        logger.error("SMTP Configuration map contains non-routable null values.")
        return False
        
    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = to_email
    msg["Subject"] = f"[{PLATFORM_NAME}] Safe Security Challenge Code"
    msg.attach(MIMEText(f"Your multi-factor security clearance authorization sequence validation code is: {code}", "plain"))
    
    try:
        with smtplib.SMTP(SMTP_HOST, int(SMTP_PORT)) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, to_email, msg.as_string())
        return True
    except Exception as e:
        logger.error(f"Mail delivery subsystem transmission dropped: {e}")
        return False

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
