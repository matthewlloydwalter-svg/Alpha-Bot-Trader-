import os


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = os.getenv("SMTP_PORT")
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SMTP_FROM = os.getenv("SMTP_FROM") or SMTP_USER
SMTP_USE_SSL = _env_flag("SMTP_USE_SSL", "0")
SMTP_USE_TLS = _env_flag("SMTP_USE_TLS", "1")
SESSION_COOKIE_SECURE = _env_flag("SESSION_COOKIE_SECURE", "0")
