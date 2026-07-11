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
IS_PROD = APP_ENV in {"production", "prod", "railway"} or bool(os.getenv("RAILWAY_ENVIRONMENT"))
_IS_PROD = IS_PROD  # backward-compatible alias

# Admin AI disk writes — off in production unless explicitly enabled.
ADMIN_AI_WRITES = _env_flag("ADMIN_AI_WRITES", "0")

# Free-tier bot cap fallback for non-admins when subscription_plan is starter.
# Plan tiers: Starter 1 | Growth 5 | Pro 10 | Enterprise unlimited. Admins unlimited.
try:
    FREE_BOT_LIMIT = int(os.getenv("FREE_BOT_LIMIT", "1") or 1)
except ValueError:
    FREE_BOT_LIMIT = 1

# Stripe billing (Checkout + webhooks). Required for /upgrade-plans checkout.
STRIPE_SECRET_KEY = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
STRIPE_WEBHOOK_SECRET = (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()
STRIPE_PUBLISHABLE_KEY = (os.getenv("STRIPE_PUBLISHABLE_KEY") or "").strip()
# Absolute public site origin used in Stripe success/cancel URLs (no trailing slash).
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or os.getenv("FRONTEND_ORIGIN") or "").strip().rstrip(",")
if "," in PUBLIC_BASE_URL:
    PUBLIC_BASE_URL = PUBLIC_BASE_URL.split(",")[0].strip()

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
