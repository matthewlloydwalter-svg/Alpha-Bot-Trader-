import os


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


# Resend transactional email (replaces Google SMTP).
RESEND_API_KEY = (os.getenv("RESEND_API_KEY") or "").strip()
EMAIL_FROM = (
    os.getenv("EMAIL_FROM") or "AlphaBotix Trading <updates@alphabotixtrading.com>"
).strip()

SESSION_COOKIE_SECURE = _env_flag("SESSION_COOKIE_SECURE", "0")

APP_ENV = (os.getenv("ENV") or os.getenv("APP_ENV") or "development").strip().lower()
_IS_PROD = APP_ENV in {"production", "prod", "railway"} or bool(os.getenv("RAILWAY_ENVIRONMENT"))

# Comma-separated browser origins allowed for credentialed CORS.
_raw_origins = (os.getenv("FRONTEND_ORIGIN") or "").strip()
if _raw_origins:
    FRONTEND_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()]
elif _IS_PROD:
    # Fail closed-ish: same-origin browser apps don't need CORS; keep empty list
    # so cross-site credentialed requests are denied unless FRONTEND_ORIGIN is set.
    FRONTEND_ORIGINS = []
else:
    FRONTEND_ORIGINS = [
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    ]

# Interactive API docs — off by default in production.
if os.getenv("DOCS_ENABLED") is not None:
    DOCS_ENABLED = _env_flag("DOCS_ENABLED", "0")
else:
    DOCS_ENABLED = not _IS_PROD
