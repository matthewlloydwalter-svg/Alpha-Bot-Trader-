import os
from datetime import date


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


# ────────────────────────────────────────────────────────────────────
# TODO: REVERT THIS AFTER 60 DAYS TO RE-ENABLE LOGIN WALL
# Temporary Google AdSense review bypass (added 2026-07-13, target undo 2026-09-11).
#
# HOW TO REVERT (after AdSense approval):
#   1. Set env ADSENSE_AUTH_BYPASS=0  (instant), OR
#   2. Tell the agent "revert AdSense bypass", OR
#   3. Search codebase for: TODO: REVERT THIS AFTER 60 DAYS TO RE-ENABLE LOGIN WALL
#
# Behavior while enabled:
#   - Login/signup pages are disabled (redirect to dashboard)
#   - Every HTTP visitor is the mock guest_trader profile ONLY
#   - Real user accounts (you + others) are NEVER returned to the UI/API
#   - /admin stays locked (guest is not admin)
#   - Background trading is UNAFFECTED: APScheduler still evaluates every
#     Bot with running=True for ALL real owners using their stored broker keys
#   - Auto-disables after ADSENSE_AUTH_BYPASS_DEADLINE even if flag left on
# ────────────────────────────────────────────────────────────────────
ADSENSE_AUTH_BYPASS_DEADLINE = date(2026, 9, 11)
ADSENSE_GUEST_EMAIL = "guest_trader@adsense-review.invalid"
ADSENSE_GUEST_NAME = "guest_trader"
ADSENSE_GUEST_BALANCE = 10000.00
_ADSENSE_AUTH_BYPASS_FLAG = _env_flag("ADSENSE_AUTH_BYPASS", "1")  # temporary default ON
ADSENSE_AUTH_BYPASS = bool(
    _ADSENSE_AUTH_BYPASS_FLAG and date.today() <= ADSENSE_AUTH_BYPASS_DEADLINE
)


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
# Plan tiers: Starter 1 | Growth 5 | Pro 10 | Enterprise 25. Admins unlimited.
try:
    FREE_BOT_LIMIT = int(os.getenv("FREE_BOT_LIMIT", "1") or 1)
except ValueError:
    FREE_BOT_LIMIT = 1

# Stripe billing (Checkout + webhooks). Required for /upgrade-plans checkout.
# Prefer STRIPE_API_KEY; STRIPE_SECRET_KEY is accepted as a backward-compatible alias.
STRIPE_API_KEY = (
    (os.getenv("STRIPE_API_KEY") or "").strip()
    or (os.getenv("STRIPE_SECRET_KEY") or "").strip()
)
STRIPE_SECRET_KEY = STRIPE_API_KEY  # alias used throughout the codebase
STRIPE_WEBHOOK_SECRET = (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()
STRIPE_PUBLISHABLE_KEY = (os.getenv("STRIPE_PUBLISHABLE_KEY") or "").strip()
# test | live — selects which Stripe Price ID dictionary is active for Checkout.
_raw_stripe_env = (os.getenv("STRIPE_ENVIRONMENT") or "").strip().lower()
if _raw_stripe_env in {"live", "prod", "production"}:
    STRIPE_ENVIRONMENT = "live"
elif _raw_stripe_env in {"test", "sandbox"}:
    STRIPE_ENVIRONMENT = "test"
else:
    # Default: live keys → live prices; otherwise test.
    STRIPE_ENVIRONMENT = "live" if STRIPE_API_KEY.startswith("sk_live_") else "test"
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
