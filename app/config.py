import os


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


# Resend transactional email (replaces Google SMTP).
RESEND_API_KEY = (os.getenv("RESEND_API_KEY") or "").strip()
EMAIL_FROM = (
    os.getenv("EMAIL_FROM") or "AlphaBotix Trading <updates@alphabotixtrading.com>"
).strip()

SESSION_COOKIE_SECURE = _env_flag("SESSION_COOKIE_SECURE", "0")
