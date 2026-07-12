import os
import json
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sse_starlette.sse import EventSourceResponse

load_dotenv()

from app.database import engine, Base, init_db, get_db, SessionLocal, User, Bot, Trade, ActivityLog, MarketQuote
from app.auth import (
    hash_password, verify_password, create_session_token, decode_session_token,
    generate_verification_code, send_verification_email, send_password_reset_email,
    create_password_reset_token, decode_password_reset_token,
    is_user_admin, PLATFORM_NAME, get_current_user, EmailError
)
from app.config import (
    SESSION_COOKIE_SECURE, FRONTEND_ORIGINS, DOCS_ENABLED, APP_ENV, IS_PROD,
    ADMIN_AI_WRITES, FREE_BOT_LIMIT, RESEND_API_KEY,
    STRIPE_SECRET_KEY, PUBLIC_BASE_URL,
)
from app.plans import (
    DEFAULT_PLAN, normalize_plan, plan_display_name, bot_limit_for_plan,
    public_plan_payload, INTERVALS,
)
from app import stripe_billing
from app.stripe_billing import BillingError

from app.brokers import get_account_info, get_spot_price, get_position_snapshot, BrokerError
from app.market_data import get_market_analysis, resolve_chart_preset, CHART_PRESETS
from app.markets_universe import MARKET_UNIVERSE
from app.credentials import resolve_credentials, has_credentials, keys_payload, seal_secret
from app.rate_limit import limit_auth, limit_verification
from app import bot_engine  # Imported bot engine to wire up the run-cycle logic
from app import market_store, ai_assistant
from app.realtime import bus
from app import scheduler as engine_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("alphabot")


def _assert_production_config() -> None:
    """Fail closed on missing launch-critical secrets in production."""
    if not IS_PROD:
        return
    # Match app.credentials: KEY_ENCRYPTION_SECRET if set, otherwise JWT_SECRET.
    key_secret = (
        (os.getenv("KEY_ENCRYPTION_SECRET") or "").strip()
        or (os.getenv("JWT_SECRET") or "").strip()
    )
    if len(key_secret) < 24:
        raise RuntimeError(
            "Broker key encryption requires KEY_ENCRYPTION_SECRET or JWT_SECRET "
            "of at least 24 characters in production. Set KEY_ENCRYPTION_SECRET "
            "(preferred) or ensure JWT_SECRET is long enough."
        )
    if not (os.getenv("KEY_ENCRYPTION_SECRET") or "").strip():
        logger.warning(
            "[STARTUP] KEY_ENCRYPTION_SECRET unset — using JWT_SECRET for "
            "broker key encryption. Prefer a dedicated KEY_ENCRYPTION_SECRET."
        )
    if not RESEND_API_KEY:
        raise RuntimeError(
            "RESEND_API_KEY must be set in production for email verification "
            "and password reset."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Capture the running event loop so background worker threads can stream
    # events to SSE subscribers, then start the always-on background engine.
    _assert_production_config()
    try:
        bus.bind_loop(asyncio.get_running_loop())
    except RuntimeError:
        pass
    engine_scheduler.start_scheduler()
    logger.info(
        "[STARTUP] %s engine core online (env=%s, free_bot_limit=%s, stripe_env=%s).",
        PLATFORM_NAME, APP_ENV, FREE_BOT_LIMIT or "unlimited",
        __import__("app.config", fromlist=["STRIPE_ENVIRONMENT"]).STRIPE_ENVIRONMENT,
    )
    try:
        yield
    finally:
        engine_scheduler.shutdown_scheduler()


_docs_url = "/docs" if DOCS_ENABLED else None
_redoc_url = "/redoc" if DOCS_ENABLED else None
app = FastAPI(
    title=f"{PLATFORM_NAME} Engine Core",
    lifespan=lifespan,
    docs_url=_docs_url,
    redoc_url=_redoc_url,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if IS_PROD:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

if not os.path.exists(STATIC_DIR):
    os.makedirs(STATIC_DIR)
if not os.path.exists(TEMPLATES_DIR):
    os.makedirs(TEMPLATES_DIR)

# Bust browser caches whenever static assets change (critical on Railway so
# redeploys don't leave users on a stale app.js / background image while HTML
# already updated).
def _asset_version() -> str:
    """Max mtime across static JS/CSS/images so image-only uploads bust caches."""
    roots = [
        os.path.join(STATIC_DIR, "js"),
        os.path.join(STATIC_DIR, "css"),
        os.path.join(STATIC_DIR, "images"),
    ]
    mtimes: list[int] = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                try:
                    mtimes.append(int(os.path.getmtime(os.path.join(dirpath, name))))
                except OSError:
                    pass
    # Also consider the top-level files historically versioned.
    for rel in ("js/app.js", "css/style.css"):
        try:
            mtimes.append(int(os.path.getmtime(os.path.join(STATIC_DIR, rel))))
        except OSError:
            pass
    return str(max(mtimes) if mtimes else int(datetime.now(timezone.utc).timestamp()))


ASSET_VERSION = _asset_version()

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

init_db()

# Models
class AuthModel(BaseModel):
    email: str
    password: str
    agreed_to_tos: bool = False
    confirm_password: Optional[str] = None


class VerificationChallengeModel(BaseModel):
    code: str


class PasswordResetRequestModel(BaseModel):
    email: str


class PasswordResetVerifyModel(BaseModel):
    email: str
    code: str


class PasswordResetConfirmModel(BaseModel):
    reset_token: str
    password: str
    confirm_password: Optional[str] = None

class AlpacaKeysModel(BaseModel):
    api_key: str
    secret_key: str
    mode: Optional[str] = "paper"   # "paper" or "live"

class OKXKeysModel(BaseModel):
    api_key: str
    secret_key: str
    passphrase: str
    mode: Optional[str] = "paper"   # "paper" or "live"

def _keep_or_seal(new_val: str, old_val: str | None) -> str | None:
    """Keep existing secret if the client sent a blank/masked value; else seal."""
    v = (new_val or "").strip()
    if (not v) or ("•" in v) or v.lower().startswith("(saved"):
        return old_val
    return seal_secret(v)


def _cookie_kwargs(request: Request | None = None) -> dict:
    secure = SESSION_COOKIE_SECURE
    if request is not None:
        forwarded_proto = (request.headers.get("x-forwarded-proto") or "").lower()
        if forwarded_proto == "https":
            secure = True
    return {"httponly": True, "max_age": 86400, "samesite": "lax", "secure": secure}


def get_current_user_from_cookie(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get("session_token")
    if not token:
        raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")
    payload = decode_session_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")
    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")
    try:
        token_sv = int(payload.get("sv", 0))
    except (TypeError, ValueError):
        token_sv = 0
    if token_sv != int(user.session_version or 0):
        raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")
    return user


def _issue_session(response: Response, request: Request, user: User) -> None:
    token = create_session_token(user.id, user.email, session_version=int(user.session_version or 0))
    response.set_cookie(key="session_token", value=token, **_cookie_kwargs(request=request))


def _bump_session_version(user: User) -> None:
    user.session_version = int(user.session_version or 0) + 1


def _user_bot_limit(user: User) -> Optional[int]:
    """
    Max bots this user may create. None = unlimited.
    Admins are always unlimited. Paid plans map via subscription_plan;
    Starter uses FREE_BOT_LIMIT (default 1).
    """
    if user.is_admin or is_user_admin(user.email or ""):
        return None
    plan = normalize_plan(getattr(user, "subscription_plan", None))
    if plan != "starter":
        return bot_limit_for_plan(plan, is_admin=False)
    # Starter / free: honor FREE_BOT_LIMIT (0 would mean unlimited — prefer 1).
    if FREE_BOT_LIMIT <= 0:
        return 1
    return FREE_BOT_LIMIT


def _enforce_bot_create_limit(user: User, db: Session) -> None:
    limit = _user_bot_limit(user)
    if limit is None:
        return
    count = db.query(Bot).filter(Bot.owner_id == user.id).count()
    if count >= limit:
        plan_name = plan_display_name(getattr(user, "subscription_plan", None))
        raise HTTPException(
            status_code=403,
            detail=(
                f"{plan_name} accounts can run up to {limit} bot"
                f"{'s' if limit != 1 else ''}. Upgrade your plan to create more."
            ),
        )


def _enforce_running_bot_limit(user: User, db: Session) -> int:
    """
    After plan downgrades (or before starting a bot), ensure running bots
    do not exceed the account's plan limit. Pauses newest excess bots first.
    Returns how many bots were paused.
    """
    limit = _user_bot_limit(user)
    if limit is None:
        return 0
    running = (
        db.query(Bot)
        .filter(Bot.owner_id == user.id, Bot.running == True)  # noqa: E712
        .order_by(Bot.id.desc())
        .all()
    )
    paused = 0
    for b in running[limit:]:
        b.running = False
        b.last_pattern_summary = (
            f"Paused automatically — plan limit is {limit} running bot"
            f"{'s' if limit != 1 else ''}."
        )
        paused += 1
    return paused


def _user_plan_payload(user: User) -> dict:
    is_admin = bool(user.is_admin or is_user_admin(user.email or ""))
    plan = normalize_plan(getattr(user, "subscription_plan", None))
    end = getattr(user, "subscription_current_period_end", None)
    from app.support_routing import support_payload_for_user
    support = support_payload_for_user(user)
    return {
        "subscription_plan": plan,
        "subscription_plan_name": plan_display_name(plan, is_admin=is_admin),
        "plan_level": support["plan_level"],
        "subscription_interval": getattr(user, "subscription_interval", None),
        "subscription_status": getattr(user, "subscription_status", None),
        "subscription_current_period_end": end.isoformat() + "Z" if end else None,
        "can_upgrade": (not is_admin) and plan != "enterprise",
        "has_stripe_customer": bool(getattr(user, "stripe_customer_id", None)),
        "support_priority": support["support_priority"],
        "support_mailto": support["support_mailto"],
        "stripe_environment": __import__("app.config", fromlist=["STRIPE_ENVIRONMENT"]).STRIPE_ENVIRONMENT,
    }


def _request_public_base(request: Request) -> str:
    configured = (PUBLIC_BASE_URL or "").strip()
    if configured:
        return configured.rstrip("/")
    # Prefer proxy headers on Railway / reverse proxies.
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    if host:
        return f"{proto}://{host}".rstrip("/")
    return str(request.base_url).rstrip("/")

def _html_page(request: Request, template_name: str = "index.html", **extra):
    """Render an HTML template with cache-busting asset version always set."""
    ctx = {
        "request": request,
        "PLATFORM_NAME": PLATFORM_NAME,
        "ASSET_VERSION": _asset_version(),
        **extra,
    }
    resp = templates.TemplateResponse(template_name, ctx)
    if template_name == "index.html":
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
    return resp


@app.get("/health")
def health():
    """Lightweight liveness probe for Railway / uptime monitors."""
    return {
        "status": "ok",
        "env": APP_ENV,
        "engine": os.getenv("ENGINE_ENABLED", "1"),
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/ads.txt", include_in_schema=False)
def get_ads_txt():
    """Google AdSense authorization file — must be reachable at /ads.txt."""
    ads_path = os.path.join(BASE_DIR, "ads.txt")
    if not os.path.isfile(ads_path):
        # Fallback so a missing deploy artifact never 404s AdSense crawlers.
        return Response(
            content="google.com, pub-2688407250698963, DIRECT, f08c47fec0942fa0\n",
            media_type="text/plain",
        )
    return FileResponse(ads_path, media_type="text/plain")


@app.get("/", response_class=HTMLResponse)
def landing_pane(request: Request):
    """Public marketing / business identity page (Stripe-accessible, no login wall)."""
    import json as _json
    return _html_page(
        request,
        "landing.html",
        YEAR=datetime.now(timezone.utc).year,
        PLANS=public_plan_payload(),
        PLANS_JSON=_json.dumps(public_plan_payload()),
    )


# Dashboard SPA sections (URL segment → served by index.html shell).
# "stocks" / "crypto" are accepted aliases for the Markets tab ("markets").
DASHBOARD_SECTIONS = frozenset({
    "portfolio",
    "markets",
    "stocks",
    "crypto",
    "bots",
    "news",
    "history",
    "assets",
    "account",
})


@app.get("/login", response_class=HTMLResponse)
@app.get("/signup", response_class=HTMLResponse)
def auth_pane(request: Request):
    """Auth screens only — separate URLs from the public landing page."""
    return _html_page(request, "index.html")


@app.get("/app", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_root(request: Request):
    """Canonical app entry: redirect bare /dashboard and legacy /app to Portfolio."""
    return RedirectResponse(url="/dashboard/portfolio", status_code=307)


@app.get("/dashboard/{section}", response_class=HTMLResponse)
def dashboard_section(section: str, request: Request):
    """Private trading app shell — one URL per former SPA tab (AdSense-eligible)."""
    key = (section or "").strip().lower()
    if key not in DASHBOARD_SECTIONS:
        return RedirectResponse(url="/dashboard/portfolio", status_code=307)
    # Normalize aliases to the canonical Markets path.
    if key in ("stocks", "crypto"):
        return RedirectResponse(url="/dashboard/markets", status_code=307)
    return _html_page(request, "index.html")


@app.get("/upgrade-plans", response_class=HTMLResponse)
def upgrade_plans_pane(request: Request, db: Session = Depends(get_db)):
    """Logged-in subscription upgrade page with Stripe Checkout CTAs."""
    import json as _json
    try:
        user = get_current_user_from_cookie(request, db)
    except Exception:
        return RedirectResponse(url="/login?next=/upgrade-plans", status_code=303)
    plans = public_plan_payload()
    return _html_page(
        request,
        "upgrade_plans.html",
        YEAR=datetime.now(timezone.utc).year,
        PLANS=plans,
        PLANS_JSON=_json.dumps(plans),
        CURRENT_PLAN=normalize_plan(getattr(user, "subscription_plan", None)),
        CURRENT_PLAN_NAME=plan_display_name(
            getattr(user, "subscription_plan", None),
            is_admin=bool(user.is_admin or is_user_admin(user.email or "")),
        ),
        CAN_UPGRADE=_user_plan_payload(user)["can_upgrade"],
        HAS_STRIPE_CUSTOMER=bool(getattr(user, "stripe_customer_id", None)),
        USER_EMAIL=user.email,
        STRIPE_CONFIGURED=bool(STRIPE_SECRET_KEY),
    )


@app.get("/checkout/success", response_class=HTMLResponse)
def checkout_success_pane(request: Request, db: Session = Depends(get_db), session_id: str = ""):
    """
    Post-Checkout landing. Confirms the session server-side (when possible),
    then shows status and links back into the app.
    """
    sid = (session_id or request.query_params.get("session_id") or "").strip()
    try:
        user = get_current_user_from_cookie(request, db)
    except Exception:
        # Preserve Checkout context so login can return here and confirm the session.
        q = f"/checkout/success?session_id={sid}" if sid else "/checkout/success"
        from urllib.parse import quote
        return RedirectResponse(url=f"/login?next={quote(q, safe='')}", status_code=303)

    confirmed = False
    error = ""
    if sid and STRIPE_SECRET_KEY:
        try:
            import stripe
            stripe.api_key = STRIPE_SECRET_KEY
            session = stripe.checkout.Session.retrieve(sid)
            meta = session.get("metadata") or {}
            owns = (
                str(session.get("client_reference_id") or "") == str(user.id)
                or str(meta.get("user_id") or "") == str(user.id)
            )
            if not owns:
                error = "This Checkout session does not belong to your account."
            elif session.get("payment_status") in {"paid", "no_payment_required"} or session.get("status") == "complete":
                stripe_billing.sync_user_from_checkout_session(user, session)
                db.add(user)
                db.commit()
                db.refresh(user)
                confirmed = True
            else:
                error = "Payment is still processing. Your plan will update when Stripe confirms."
        except Exception as exc:
            logger.warning("checkout success confirm failed: %s", exc)
            error = "Could not confirm Checkout yet. If you were charged, access will unlock via webhook shortly."

    return _html_page(
        request,
        "checkout_success.html",
        YEAR=datetime.now(timezone.utc).year,
        CONFIRMED=confirmed,
        ERROR=error,
        PLAN_NAME=plan_display_name(
            getattr(user, "subscription_plan", None),
            is_admin=bool(user.is_admin or is_user_admin(user.email or "")),
        ),
        USER_EMAIL=user.email,
    )


@app.get("/api/plans")
def api_plans():
    """Public plan catalog for pricing UIs."""
    from app.config import STRIPE_ENVIRONMENT
    return {
        "plans": public_plan_payload(),
        "intervals": list(INTERVALS),
        "stripe_environment": STRIPE_ENVIRONMENT,
    }


@app.get("/api/support/lookup")
def api_support_lookup(request: Request, email: str = "", db: Session = Depends(get_db)):
    """
    Look up plan_level + support routing for an email (admin/support tooling).
    Returns plan_level and destination inbox for automated ticket routing.
    """
    _require_admin(request, db)
    from app.support_routing import (
        get_plan_level_by_email, support_destination_email,
        get_support_priority_for_plan, support_mailto_for_plan,
    )
    level = get_plan_level_by_email(email, db)
    return {
        "email": (email or "").strip().lower(),
        "plan_level": level,
        "support_priority": get_support_priority_for_plan(level),
        "support_destination": support_destination_email(level),
        "support_mailto": support_mailto_for_plan(level),
    }


@app.get("/api/support/priority/{user_id}")
def api_support_priority(user_id: int, request: Request, db: Session = Depends(get_db)):
    _require_admin(request, db)
    from app.support_routing import get_support_priority
    return {"user_id": user_id, "support_priority": get_support_priority(user_id, db)}


class CheckoutModel(BaseModel):
    """Client may send only a price_id or lookup_key — never amounts."""
    price_id: Optional[str] = None
    lookup_key: Optional[str] = None
    # Backward-compatible fields: server maps them to an allowlisted price_id.
    plan: Optional[str] = None
    interval: Optional[str] = None


@app.post("/billing/checkout")
def billing_checkout(
    body: CheckoutModel,
    request: Request,
    u: User = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """
    Create a Stripe Checkout Session (mode=subscription).
    Server builds line_items from an allowlisted price_id / lookup_key.
    Returns {"url": "..."} — frontend redirects the browser to that URL.
    """
    if u.is_admin or is_user_admin(u.email or ""):
        raise HTTPException(status_code=400, detail="Admin accounts already have unlimited access.")

    status = (getattr(u, "subscription_status", None) or "").lower()
    if getattr(u, "stripe_subscription_id", None) and status in {"active", "trialing", "past_due"}:
        raise HTTPException(
            status_code=400,
            detail="You already have an active subscription. Use Manage billing to change or cancel your plan.",
        )

    price_id = (body.price_id or "").strip() or None
    lookup_key = (body.lookup_key or "").strip() or None
    if not price_id and not lookup_key and body.plan:
        # Legacy UI: map plan+interval → allowlisted price_id on the server.
        from app.plans import price_id_for
        interval = (body.interval or "month").strip().lower()
        if interval not in INTERVALS:
            raise HTTPException(status_code=400, detail="Interval must be week, month, or year.")
        price_id = price_id_for(normalize_plan(body.plan), interval)
        if not price_id:
            raise HTTPException(status_code=400, detail="Starter is free — no checkout required.")

    try:
        result = stripe_billing.create_checkout_session(
            u,
            price_id=price_id,
            lookup_key=lookup_key,
            public_base_url=_request_public_base(request),
        )
        db.add(u)
        db.commit()
        return result
    except BillingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except Exception as exc:
        logger.exception("Stripe checkout failed")
        raise HTTPException(status_code=502, detail=f"Stripe checkout failed: {exc}")


@app.post("/billing/portal")
def billing_portal(
    request: Request,
    u: User = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """Create a Stripe Customer Billing Portal session and return its URL."""
    try:
        result = stripe_billing.create_billing_portal_session(
            u,
            public_base_url=_request_public_base(request),
        )
        db.add(u)
        db.commit()
        return result
    except BillingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except Exception as exc:
        logger.exception("Stripe billing portal failed")
        raise HTTPException(status_code=502, detail=f"Stripe portal failed: {exc}")


@app.get("/billing/checkout/confirm")
def billing_confirm_checkout(
    session_id: str = "",
    u: User = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """Optional post-Checkout sync when webhooks are delayed."""
    sid = (session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="session_id required")
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Stripe is not configured")
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
        session = stripe.checkout.Session.retrieve(sid)
    except ImportError:
        raise HTTPException(status_code=503, detail="Stripe package not installed")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not load Checkout session: {exc}")
    meta = session.get("metadata") or {}
    owns = (
        str(session.get("client_reference_id") or "") == str(u.id)
        or str(meta.get("user_id") or "") == str(u.id)
    )
    if not owns:
        raise HTTPException(status_code=403, detail="Checkout session does not belong to this account.")
    if session.get("payment_status") in {"paid", "no_payment_required"} or session.get("status") == "complete":
        stripe_billing.sync_user_from_checkout_session(u, session)
        db.add(u)
        db.commit()
    return {"ok": True, **_user_plan_payload(u)}


async def _handle_stripe_webhook(request: Request, db: Session):
    """Shared Stripe webhook handler (signature-verified)."""
    payload = await request.body()
    sig = request.headers.get("stripe-signature") or ""
    try:
        event = stripe_billing.construct_webhook_event(payload, sig)
    except BillingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    etype = event.get("type")
    data = (event.get("data") or {}).get("object") or {}
    user = stripe_billing.find_user_for_stripe_object(db, data, User)

    # Paid Checkout / subscription events must resolve a user or Stripe will
    # stop retrying after a 2xx — leave unpaid plan upgrades silent.
    critical = etype in {
        "checkout.session.completed",
        "customer.subscription.updated",
        "customer.subscription.created",
        "customer.subscription.deleted",
    }
    if critical and user is None:
        logger.error(
            "Stripe webhook %s could not resolve a local user (event=%s)",
            etype, event.get("id"),
        )
        raise HTTPException(
            status_code=500,
            detail="Webhook user not found — Stripe should retry after account sync.",
        )

    if etype == "checkout.session.completed" and user:
        stripe_billing.sync_user_from_checkout_session(user, data)
    elif etype in {
        "customer.subscription.updated",
        "customer.subscription.created",
        "customer.subscription.deleted",
    } and user:
        stripe_billing.sync_user_from_subscription(user, data)

    if user is not None:
        _enforce_running_bot_limit(user, db)
        db.add(user)
        db.commit()
    return {"received": True}


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """Stripe webhook endpoint (preferred path)."""
    return await _handle_stripe_webhook(request, db)


@app.post("/billing/webhook")
async def billing_webhook(request: Request, db: Session = Depends(get_db)):
    """Alias of /webhooks/stripe for existing Stripe dashboard configs."""
    return await _handle_stripe_webhook(request, db)


@app.get("/terms", response_class=HTMLResponse)
def terms_pane(request: Request):
    return templates.TemplateResponse(
        "legal.html",
        {
            "request": request,
            "PLATFORM_NAME": PLATFORM_NAME,
            "ASSET_VERSION": _asset_version(),
            "PAGE": "terms",
            "PAGE_TITLE": "Terms of Service",
            "YEAR": datetime.now(timezone.utc).year,
        },
    )


@app.get("/privacy", response_class=HTMLResponse)
def privacy_pane(request: Request):
    return templates.TemplateResponse(
        "legal.html",
        {
            "request": request,
            "PLATFORM_NAME": PLATFORM_NAME,
            "ASSET_VERSION": _asset_version(),
            "PAGE": "privacy",
            "PAGE_TITLE": "Privacy Policy",
            "YEAR": datetime.now(timezone.utc).year,
        },
    )


@app.get("/admin", response_class=HTMLResponse)
def admin_pane(request: Request, db: Session = Depends(get_db)):
    try:
        u = get_current_user_from_cookie(request, db)
        if not u.is_admin:
            return RedirectResponse(url="/dashboard/portfolio")
        return templates.TemplateResponse("admin.html", {"request": request, "PLATFORM_NAME": PLATFORM_NAME, "ASSET_VERSION": _asset_version()})
    except Exception:
        return RedirectResponse(url="/login?next=/admin", status_code=303)

@app.post("/auth/signup")
def register_endpoint(body: AuthModel, response: Response, request: Request, db: Session = Depends(get_db)):
    limit_auth(request)
    normalized_email = body.email.strip().lower()
    if not normalized_email or "@" not in normalized_email:
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    if not body.agreed_to_tos:
        raise HTTPException(status_code=400, detail="You must agree to the Terms of Service and Privacy Policy.")
    if len(body.password or "") < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    if body.confirm_password is not None and body.confirm_password != body.password:
        raise HTTPException(status_code=400, detail="Passwords do not match.")
    existing = db.query(User).filter(User.email == normalized_email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered in system databases.")
    
    new_user = User(
        email=normalized_email,
        hashed_password=hash_password(body.password),
        is_admin=is_user_admin(normalized_email),
        email_verified=False,
        subscription_plan=DEFAULT_PLAN,
        plan_level="Starter",
    )
    db.add(new_user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Email already registered in system databases.")
    db.refresh(new_user)
    
    _issue_session(response, request, new_user)
    payload = {
        "id": new_user.id,
        "email": new_user.email,
        "is_admin": new_user.is_admin,
        "email_verified": new_user.email_verified,
        **_user_plan_payload(new_user),
        "bot_count": 0,
        "bot_limit": _user_bot_limit(new_user),
    }
    return payload

@app.post("/auth/login")
def login_endpoint(body: AuthModel, response: Response, request: Request, db: Session = Depends(get_db)):
    limit_auth(request)
    normalized_email = body.email.strip().lower()
    user = db.query(User).filter(User.email == normalized_email).first()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Invalid credential combination supplied.")
        
    _issue_session(response, request, user)

    return {
        "id": user.id,
        "email": user.email,
        "is_admin": user.is_admin,
        "email_verified": user.email_verified,
        "trading_mode": user.trading_mode or "paper",
        "active_broker": user.active_broker or "alpaca",
        "total_deposited": user.total_deposited or 0.0,
        "total_withdrawn": user.total_withdrawn or 0.0,
        **_user_plan_payload(user),
        "bot_count": db.query(Bot).filter(Bot.owner_id == user.id).count(),
        "bot_limit": _user_bot_limit(user),
    }

@app.get("/auth/me")
def current_user_endpoint(u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    limit = _user_bot_limit(u)
    bot_count = db.query(Bot).filter(Bot.owner_id == u.id).count()
    return {
        "id": u.id,
        "email": u.email,
        "is_admin": u.is_admin,
        "email_verified": u.email_verified,
        "trading_mode": u.trading_mode or "paper",
        "active_broker": u.active_broker or "alpaca",
        "total_deposited": u.total_deposited or 0.0,
        "total_withdrawn": u.total_withdrawn or 0.0,
        "bot_count": bot_count,
        "bot_limit": limit,  # null = unlimited
        **_user_plan_payload(u),
    }

@app.post("/auth/logout")
def logout_endpoint(response: Response, request: Request):
    response.delete_cookie("session_token", samesite="lax", secure=_cookie_kwargs(request=request)["secure"])
    return {"success": True}

@app.post("/auth/trigger-verification")
def trigger_verification(request: Request, u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    limit_verification(request)
    code = generate_verification_code()
    u.verification_code = code
    db.commit()
    try:
        send_verification_email(u.email, code)
    except EmailError as e:
        logger.warning("Verification email failed for %s: %s", u.email, e)
        # Return 200 with email_not_configured so the frontend can show a
        # helpful inline message rather than a red error toast.
        # smtp_not_configured kept as a legacy alias for older frontends.
        return {
            "success": False,
            "email_not_configured": True,
            "smtp_not_configured": True,
            "detail": str(e),
        }
    except Exception as e:
        logger.error("Unexpected email error for %s: %s", u.email, e)
        raise HTTPException(status_code=500, detail=f"Unexpected mail error: {e}")
    return {"success": True}

@app.post("/auth/confirm-verification")
def confirm_verification(body: VerificationChallengeModel, request: Request, u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    limit_verification(request)
    stored = (u.verification_code or "").strip()
    # Password-reset codes are namespaced so they cannot confirm email verify.
    if not stored or stored.startswith("rp:") or stored != body.code.strip():
        raise HTTPException(status_code=400, detail="Invalid or expired verification code.")
    u.email_verified = True
    u.verification_code = None
    db.commit()
    return {"success": True}


@app.post("/auth/password-reset/request")
def password_reset_request(body: PasswordResetRequestModel, request: Request, db: Session = Depends(get_db)):
    """
    Start forgot-password: email a 6-digit code via the same Resend path used
    for account verification. Always returns a generic success payload so we
    do not reveal whether the email is registered.
    """
    limit_verification(request)
    normalized_email = (body.email or "").strip().lower()
    if not normalized_email or "@" not in normalized_email:
        raise HTTPException(status_code=400, detail="Enter a valid email address.")

    user = db.query(User).filter(User.email == normalized_email).first()
    if user:
        code = generate_verification_code()
        # Namespace so an in-flight email-verify code is not overwritten ambiguously
        # and reset codes cannot be reused on /auth/confirm-verification.
        user.verification_code = f"rp:{code}"
        db.commit()
        try:
            send_password_reset_email(user.email, code)
        except EmailError as e:
            logger.warning("Password reset email failed for %s: %s", user.email, e)
            return {
                "success": False,
                "email_not_configured": True,
                "detail": str(e),
            }
        except Exception as e:
            logger.error("Unexpected password-reset email error for %s: %s", user.email, e)
            raise HTTPException(status_code=500, detail=f"Unexpected mail error: {e}")

    return {
        "success": True,
        "detail": "If an account exists for that email, a verification code has been sent.",
    }


@app.post("/auth/password-reset/verify")
def password_reset_verify(body: PasswordResetVerifyModel, request: Request, db: Session = Depends(get_db)):
    """Confirm the emailed code and issue a short-lived password-reset token."""
    limit_verification(request)
    normalized_email = (body.email or "").strip().lower()
    code = (body.code or "").strip()
    if not normalized_email or not code:
        raise HTTPException(status_code=400, detail="Enter your email and verification code.")

    user = db.query(User).filter(User.email == normalized_email).first()
    expected = f"rp:{code}"
    if not user or not user.verification_code or user.verification_code != expected:
        raise HTTPException(status_code=400, detail="Invalid verification code.")

    # Consume the one-time code; the reset token authorizes the password change.
    user.verification_code = None
    # Proving inbox access also satisfies email verification.
    user.email_verified = True
    db.commit()
    reset_token = create_password_reset_token(user.id, user.email)
    return {"success": True, "reset_token": reset_token}


@app.post("/auth/password-reset/confirm")
def password_reset_confirm(body: PasswordResetConfirmModel, request: Request, db: Session = Depends(get_db)):
    """Set a new password after a successful code verification."""
    limit_auth(request)
    payload = decode_password_reset_token(body.reset_token or "")
    if not payload:
        raise HTTPException(status_code=400, detail="Reset session expired. Request a new verification code.")
    try:
        user_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Reset session expired. Request a new verification code.")

    if len(body.password or "") < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    if body.confirm_password is not None and body.confirm_password != body.password:
        raise HTTPException(status_code=400, detail="Passwords do not match.")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=400, detail="Account not found.")
    token_email = (payload.get("email") or "").strip().lower()
    if token_email and token_email != (user.email or "").strip().lower():
        raise HTTPException(status_code=400, detail="Reset session expired. Request a new verification code.")

    user.hashed_password = hash_password(body.password)
    user.verification_code = None
    _bump_session_version(user)
    db.commit()
    logger.info("[AUTH] Password reset completed for user %s (sessions invalidated)", user.id)
    return {"success": True, "detail": "Password updated. You can sign in with your new password."}

@app.post("/broker/alpaca/keys")
def save_alpaca_keys(body: AlpacaKeysModel, u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    mode = (body.mode or "paper").lower()
    if mode not in ("paper", "live"):
        raise HTTPException(status_code=400, detail="mode must be 'paper' or 'live'.")
    if mode == "paper":
        key = _keep_or_seal(body.api_key, u.alpaca_key_paper or u.alpaca_key)
        secret = _keep_or_seal(body.secret_key, u.alpaca_secret_paper or u.alpaca_secret)
        if not key or not secret:
            raise HTTPException(status_code=400, detail="Enter both Alpaca paper API key and secret.")
        u.alpaca_key_paper, u.alpaca_secret_paper = key, secret
        u.alpaca_key, u.alpaca_secret = key, secret
    else:
        key = _keep_or_seal(body.api_key, u.alpaca_key_live)
        secret = _keep_or_seal(body.secret_key, u.alpaca_secret_live)
        if not key or not secret:
            raise HTTPException(status_code=400, detail="Enter both Alpaca live API key and secret.")
        u.alpaca_key_live, u.alpaca_secret_live = key, secret
    db.commit()
    logger.info("[KEYS] Saved Alpaca %s keys for user %s", mode, u.id)
    return {"success": True, "mode": mode}

@app.post("/broker/okx/keys")
def save_okx_keys(body: OKXKeysModel, u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    mode = (body.mode or "paper").lower()
    if mode not in ("paper", "live"):
        raise HTTPException(status_code=400, detail="mode must be 'paper' or 'live'.")
    if mode == "paper":
        key = _keep_or_seal(body.api_key, u.okx_key_paper or u.okx_key)
        secret = _keep_or_seal(body.secret_key, u.okx_secret_paper or u.okx_secret)
        passphrase = _keep_or_seal(body.passphrase, u.okx_pass_paper or u.okx_pass)
        if not key or not secret or not passphrase:
            raise HTTPException(status_code=400, detail="Enter all OKX paper fields.")
        u.okx_key_paper, u.okx_secret_paper, u.okx_pass_paper = key, secret, passphrase
        u.okx_key, u.okx_secret, u.okx_pass = key, secret, passphrase
    else:
        key = _keep_or_seal(body.api_key, u.okx_key_live)
        secret = _keep_or_seal(body.secret_key, u.okx_secret_live)
        passphrase = _keep_or_seal(body.passphrase, u.okx_pass_live)
        if not key or not secret or not passphrase:
            raise HTTPException(status_code=400, detail="Enter all OKX live fields.")
        u.okx_key_live, u.okx_secret_live, u.okx_pass_live = key, secret, passphrase
    db.commit()
    logger.info("[KEYS] Saved OKX %s keys for user %s", mode, u.id)
    return {"success": True, "mode": mode}

@app.get("/broker/keys")
def get_broker_keys(u: User = Depends(get_current_user_from_cookie)):
    """
    Return masked key status for the Account UI. Full secrets are never echoed back.
    """
    return keys_payload(u, mask=True)

@app.post("/broker/trading-mode")
async def set_trading_mode(request: Request, u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    data = await request.json()
    mode = data.get("mode", "paper")
    if mode not in ["paper", "live"]:
        raise HTTPException(status_code=400, detail="Invalid mode. Use 'paper' or 'live'.")
    if mode == "live":
        if not u.email_verified:
            raise HTTPException(
                status_code=403,
                detail="Verify your email before enabling Live trading (Account → Send Verification Code).",
            )
        broker = (u.active_broker or "alpaca").lower()
        if not has_credentials(u, broker, paper=False):
            raise HTTPException(
                status_code=403,
                detail=f"Save valid Live {broker.upper()} API keys in Account before enabling Live trading.",
            )
    previous = (u.trading_mode or "paper").lower()
    u.trading_mode = mode

    # Switching the account UI to Paper must not leave Live bots running in the
    # background. Pause any running bots assigned to the mode we are leaving.
    paused_ids: list[int] = []
    if previous != mode:
        leaving = previous
        running_bots = (
            db.query(Bot)
            .filter(
                Bot.owner_id == u.id,
                Bot.running == True,  # noqa: E712
            )
            .all()
        )
        for b in running_bots:
            bot_mode = (b.mode or previous).lower()
            if bot_mode == leaving:
                b.running = False
                paused_ids.append(b.id)
                b.last_pattern_summary = (
                    f"Paused automatically — account switched from {leaving} to {mode}."
                )

    db.commit()
    return {
        "trading_mode": u.trading_mode,
        "paused_bots": paused_ids,
        "paused_count": len(paused_ids),
    }

@app.post("/broker/switch")
async def switch_broker(request: Request, u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    data = await request.json()
    broker = data.get("broker", "alpaca")
    if broker not in ["alpaca", "okx"]:
        raise HTTPException(status_code=400, detail="Invalid broker. Use 'alpaca' or 'okx'.")
    u.active_broker = broker
    db.commit()
    return {"active_broker": u.active_broker}

@app.get("/broker/account")
def get_broker_account(u: User = Depends(get_current_user_from_cookie)):
    broker = u.active_broker or "alpaca"
    paper  = (u.trading_mode or "paper") == "paper"
    mode_label = "Paper" if paper else "Live"
    creds = resolve_credentials(u, broker, paper)
    try:
        if broker == "alpaca":
            if not creds.get("alpaca_key"):
                raise HTTPException(status_code=400, detail=f"No Alpaca {mode_label} API keys configured. Add them in Account → Alpaca API Keys.")
            return get_account_info(broker="alpaca", paper=paper, **creds)
        elif broker == "okx":
            if not creds.get("okx_key"):
                raise HTTPException(status_code=400, detail=f"No OKX {mode_label} API keys configured. Add them in Account → OKX API Keys.")
            return get_account_info(broker="okx", paper=paper, **creds)
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("broker account lookup failed for user %s: %s", u.id, e)
        detail = "Could not reach the broker account. Check your API keys and try again."
        if not IS_PROD:
            detail = f"{detail} ({e})"
        raise HTTPException(status_code=400, detail=detail)

@app.get("/broker/trades-ledger")
def get_trades_ledger(u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    # Query database and map trade results with full quantity + bot attribution.
    records = db.query(Trade).filter(Trade.owner_id == u.id).order_by(Trade.created_at.desc()).all()
    bot_names = {b.id: b.name for b in db.query(Bot).filter(Bot.owner_id == u.id).all()}

    def _qty(r):
        # Never surface a misleading 0: reconstruct from notional/price if needed.
        if r.qty:
            return float(r.qty)
        if r.notional and r.price:
            return float(r.notional) / float(r.price)
        return 0.0

    return [
        {
            "id": r.id,
            "ticker": r.ticker,
            "side": r.side,
            "qty": round(_qty(r), 8),
            "notional": r.notional,
            "price": r.price,
            "mode": r.mode,
            "broker": r.broker,
            "bot_id": r.bot_id,
            "bot_uuid": r.bot_uuid,
            "bot_name": bot_names.get(r.bot_id, "Manual / Unlinked" if r.bot_id is None else f"Bot #{r.bot_id}"),
            "created_at": r.created_at.isoformat()
        } for r in records
    ]

@app.get("/admin/stats")
def admin_stats(request: Request, db: Session = Depends(get_db)):
    u = get_current_user_from_cookie(request, db)
    if not u.is_admin: raise HTTPException(status_code=403)
    return {
        "total_users": db.query(User).count(),
        "verified_users": db.query(User).filter(User.email_verified == True).count(),
        "total_deposited": sum(float(x.total_deposited or 0) for x in db.query(User).all()),
        "total_bots": db.query(Bot).count(),
        "total_trades": db.query(Trade).count()
    }

@app.get("/admin/users")
def admin_users_list(request: Request, db: Session = Depends(get_db)):
    u = get_current_user_from_cookie(request, db)
    if not u.is_admin: raise HTTPException(status_code=403)
    users = db.query(User).all()
    return [{"email": x.email, "is_admin": x.is_admin, "email_verified": x.email_verified} for x in users]


@app.get("/system/logs")
def get_system_logs(u: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    Returns the last 50 operational logs for the logged-in user.
    Serialized to plain dicts so FastAPI doesn't choke on raw ORM rows.
    """
    rows = db.query(ActivityLog)\
             .filter(ActivityLog.user_id == u.id)\
             .order_by(ActivityLog.created_at.desc())\
             .limit(50)\
             .all()
    return [
        {
            "id": r.id,
            "message": r.message,
            "level": r.level,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows
    ]

@app.get("/bots")
def get_bots(u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    bots = db.query(Bot).filter(Bot.owner_id == u.id).all()
    rows = []
    for b in bots:
        broker = (b.broker or u.active_broker or "alpaca").lower()
        # Prefer the bot's own mode; fall back to the account trading mode.
        paper = ((b.mode or u.trading_mode or "paper").lower() == "paper")
        entry_price = b.avg_entry_price
        current_price = None
        unrealized_pl = None

        # Prefer the always-on market store quote (no broker round-trip).
        if b.in_position and b.ticker:
            q = market_store.get_quote(db, broker, b.ticker)
            if q and q.price:
                current_price = float(q.price)

        # Optional live broker snapshot when keys are present and store has no mark.
        if b.in_position and b.ticker and current_price is None and has_credentials(u, broker, paper):
            try:
                creds = resolve_credentials(u, broker, paper)
                position_snapshot = get_position_snapshot(
                    broker=broker,
                    symbol=b.ticker,
                    paper=paper,
                    **creds,
                )
                if position_snapshot:
                    current_price = position_snapshot.get("current_price")
                    unrealized_pl = position_snapshot.get("unrealized_pl")
            except Exception as e:
                logger.debug("bot position snapshot failed for %s: %s", b.ticker, e)

        if current_price is None and entry_price is not None:
            current_price = entry_price
        if unrealized_pl is None and current_price is not None and entry_price is not None and (b.shares_held or 0) > 0:
            unrealized_pl = (current_price - entry_price) * (b.shares_held or 0)

        # Prefer the bot's armed risk levels; only fall back to env-derived display
        # targets when the bot has not armed stops yet (e.g. flat / pre-entry).
        display_stop_price = b.stop_price
        display_take_profit_price = b.take_profit_price
        if display_stop_price is None or display_take_profit_price is None:
            calc_stop, calc_tp = bot_engine._get_display_risk_targets(entry_price)
            if display_stop_price is None:
                display_stop_price = calc_stop
            if display_take_profit_price is None:
                display_take_profit_price = calc_tp

        strategy_name = (b.low_balance_strategy or "standard").lower()
        state = bot_engine._load_strategy_state(b)
        scattershot_legs = (state.get("legs") or []) if strategy_name == "scattershot" else []
        swing_hold_days = None
        if strategy_name == "swing_trader" and b.in_position and b.position_opened_at:
            swing_hold_days = max(
                1,
                int((datetime.utcnow() - b.position_opened_at).total_seconds() // 86400) + 1,
            )

        rows.append({
            "id": b.id,
            "name": b.name,
            "ticker": b.ticker,
            "auto_select": b.auto_select,
            "low_balance_strategy": b.low_balance_strategy or "standard",
            "low_balance_strategy_label": bot_engine._strategy_label(b.low_balance_strategy),
            "low_balance_strategy_tooltip": bot_engine._strategy_tooltip(b.low_balance_strategy),
            "strategy_cooldown_until": b.strategy_cooldown_until.isoformat() if b.strategy_cooldown_until else None,
            "scattershot_legs": scattershot_legs,
            "position_opened_at": b.position_opened_at.isoformat() if b.position_opened_at else None,
            "swing_hold_days": swing_hold_days,
            "broker": b.broker,
            "mode": b.mode or "paper",
            "timeframe": b.timeframe,
            "funds_allocated": b.funds_allocated,
            "is_auto": b.is_auto,
            "running": b.running,
            "trade_count": b.trade_count,
            "in_position": b.in_position,
            "shares_held": b.shares_held,
            "avg_entry_price": b.avg_entry_price,
            "entry_price": entry_price,
            "current_price": current_price,
            "unrealized_pl": unrealized_pl,
            "stop_price": display_stop_price,
            "take_profit_price": display_take_profit_price,
            "display_stop_price": display_stop_price,
            "display_take_profit_price": display_take_profit_price,
            "realized_pnl": b.realized_pnl,
            "last_signal": b.last_signal,
            "last_pattern_summary": b.last_pattern_summary,
            "last_analysis_at": b.last_analysis_at.isoformat() if b.last_analysis_at else None,
        })
    return rows

@app.get("/api/news")
def get_news():
    """Server-side RSS proxy — avoids browser CORS restrictions."""
    import requests as req
    import xml.etree.ElementTree as ET

    feeds = [
        "https://feeds.marketwatch.com/marketwatch/topstories/",
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "https://finance.yahoo.com/news/rssindex",
    ]

    for url in feeds:
        try:
            resp = req.get(url, timeout=7, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                continue
            root = ET.fromstring(resp.content)
            items = []
            from app.news_analysis import classify_headline_sentiment
            for item in root.findall(".//item")[:30]:
                title    = (item.findtext("title") or "").strip()
                link     = (item.findtext("link")  or "").strip()
                pub_date = (item.findtext("pubDate") or "").strip()
                if title and link:
                    items.append({"title": title, "link": link, "pubDate": pub_date,
                                  "sentiment": classify_headline_sentiment(title)})
            if items:
                return items
        except Exception as e:
            logger.warning(f"News feed failed ({url}): {e}")
            continue

    return []

@app.get("/api/markets/{exchange}")
def list_markets(exchange: str):
    """Return the tradable universe for an exchange (drives the Markets tab)."""
    ex = exchange.lower()
    if ex not in MARKET_UNIVERSE:
        raise HTTPException(status_code=404, detail=f"Unknown exchange '{exchange}'.")
    cfg = MARKET_UNIVERSE[ex]
    return {
        "exchange": ex,
        "asset_class": cfg["asset_class"],
        "quote": cfg["quote"],
        "count": len(cfg["items"]),
        "items": cfg["items"],
    }


@app.get("/api/markets/{exchange}/{symbol}/dashboard")
def market_dashboard(exchange: str, symbol: str, timeframe: str = "1h", limit: int = 200,
                     preset: Optional[str] = None,
                     u: User = Depends(get_current_user_from_cookie),
                     db: Session = Depends(get_db)):
    """
    The heart of the clickable Market Dashboard.

    Fetches historical candles for the asset, runs them through the shared
    pattern-analysis brain (indicators + structural patterns + a normalized
    signal) and returns a fully-prepared payload the UI can render directly.
    The bots consume this exact same analysis via market_data.

    Optional ``preset`` (1D / 1M / 3M) maps to Alpaca-compatible timeframe +
    a dynamically calculated UTC start_date:
      1D → 1Min bars from now−1 day
      1M → 30Min bars from now−30 days
      3M → 1Hour bars from now−90 days
    """
    ex = exchange.lower()
    if ex not in MARKET_UNIVERSE:
        raise HTTPException(status_code=404, detail=f"Unknown exchange '{exchange}'.")

    cfg = MARKET_UNIVERSE[ex]
    meta = next((i for i in cfg["items"] if i["symbol"].upper() == symbol.upper()), None)
    asset_name = meta["name"] if meta else symbol.upper()
    display = meta["display"] if meta else symbol.upper()

    start = None
    resolved_preset = None
    if preset:
        try:
            resolved = resolve_chart_preset(preset)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        timeframe = resolved["timeframe"]
        limit = resolved["limit"]
        start = resolved["start"]
        resolved_preset = resolved["preset"]
        logger.info(
            "[DASHBOARD] preset=%s → tf=%s (%s) start=%s limit=%d",
            resolved_preset, timeframe, resolved["alpaca_timeframe"],
            start.isoformat(), limit,
        )
    else:
        limit = max(50, min(int(limit), 500))

    # Preset windows need more bars than the legacy dropdown (1-min × 1 day).
    if resolved_preset:
        limit = max(50, min(int(limit), 5000))

    paper = (u.trading_mode or "paper") == "paper"
    creds = resolve_credentials(u, ex, paper)

    # Equity charts need Alpaca credentials. Fall back to platform data keys
    # (same pattern as the market poller) so Markets works before a user saves keys.
    if ex == "alpaca" and not (creds.get("alpaca_key") and creds.get("alpaca_secret")):
        env_key = os.getenv("ALPACA_DATA_KEY") or os.getenv("ALPACA_API_KEY")
        env_secret = os.getenv("ALPACA_DATA_SECRET") or os.getenv("ALPACA_SECRET_KEY")
        if env_key and env_secret:
            creds = {**creds, "alpaca_key": env_key, "alpaca_secret": env_secret}
        else:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Add your Alpaca API keys in Account to view equity market charts "
                    "(or ask the platform admin to set ALPACA_DATA_KEY / ALPACA_DATA_SECRET)."
                ),
            )

    try:
        analysis = get_market_analysis(
            broker=ex, symbol=symbol, timeframe=timeframe, limit=limit,
            start=start, preset=resolved_preset,
            alpaca_key=creds.get("alpaca_key"), alpaca_secret=creds.get("alpaca_secret"),
            okx_key=creds.get("okx_key"), okx_secret=creds.get("okx_secret"),
            okx_passphrase=creds.get("okx_passphrase"),
            paper=paper,
            # Preset charts refresh every 15s and must recompute end=now; skip the
            # short in-process cache so soft refreshes cannot serve a stale window.
            use_cache=not bool(resolved_preset),
        )
    except BrokerError as e:
        logger.warning("Dashboard data unavailable for %s:%s — %s", ex, symbol, e)
        raise HTTPException(status_code=502, detail=str(e))

    # Keep the live market-of-record fresh from this on-demand fetch too.
    try:
        candle_ts = analysis.candles[-1]["time"] if analysis.candles else None
        market_store.upsert_quote(db, ex, symbol, analysis.last_price,
                                  signal_action=analysis.signal.action,
                                  signal_strength=analysis.signal.strength,
                                  candle_ts=candle_ts)
        bus.publish("market_quote", {
            "broker": ex, "symbol": symbol.upper(), "price": analysis.last_price,
            "signal_action": analysis.signal.action,
            "signal_strength": analysis.signal.strength,
        })
    except Exception:
        pass

    # Find the user's bot(s) trading this asset to surface "Internal Bot Status".
    bot_status = _collect_bot_status(u.id, ex, symbol)

    payload = analysis.to_dict()
    # Surface freshness so the UI can show when the last candle actually is
    # (helps distinguish "refreshing every 15s" from "last trade was at market close").
    last_candle_ts = None
    if analysis.candles:
        last_candle_ts = analysis.candles[-1].get("time") or analysis.candles[-1].get("ts")
    data_as_of = None
    if last_candle_ts:
        try:
            data_as_of = datetime.fromtimestamp(int(last_candle_ts), timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            data_as_of = None
    payload.update({
        "asset_name": asset_name,
        "display_symbol": display,
        "asset_class": cfg["asset_class"],
        "quote": cfg["quote"],
        "bot_status": bot_status,
        "preset": resolved_preset,
        "chart_presets": list(CHART_PRESETS.keys()),
        "start_date": start.isoformat() if start else None,
        "data_as_of": data_as_of,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })
    return payload


def _collect_bot_status(user_id: int, exchange: str, symbol: str) -> dict:
    """Summarize what the internal bots think/are doing for this asset."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        bots = db.query(Bot).filter(
            Bot.owner_id == user_id,
            Bot.ticker.ilike(symbol),
        ).all()
        if not bots:
            return {
                "has_bot": False,
                "headline": "No bot deployed on this asset yet.",
                "detail": "Create a bot on this ticker to let the engine scan it autonomously.",
                "bots": [],
            }
        return {
            "has_bot": True,
            "headline": f"{len(bots)} bot(s) monitoring {symbol.upper()}",
            "bots": [{
                "id": b.id, "name": b.name, "running": b.running,
                "in_position": b.in_position, "signal": b.last_signal,
                "summary": b.last_pattern_summary or "Awaiting first scan.",
                "stop_price": b.stop_price, "take_profit_price": b.take_profit_price,
                "avg_entry_price": b.avg_entry_price, "trade_count": b.trade_count,
                "analyzed_at": b.last_analysis_at.isoformat() if b.last_analysis_at else None,
            } for b in bots],
        }
    finally:
        db.close()


@app.post("/bots/scan-all")
def scan_all_bots(u: User = Depends(get_current_user_from_cookie)):
    """Run one decision cycle for every running bot owned by this user."""
    db = SessionLocal()
    try:
        bots = db.query(Bot).filter(Bot.owner_id == u.id, Bot.running == True).all()  # noqa: E712
    finally:
        db.close()
    results = []
    for b in bots:
        try:
            results.append({"bot_id": b.id, "ticker": b.ticker, **bot_engine.run_cycle(b.id)})
        except Exception as e:
            results.append({"bot_id": b.id, "ticker": b.ticker, "action": "ERROR", "reason": str(e)})
    return {"scanned": len(results), "results": results}


def _bot_performance_highlights(db: Session, bots: list) -> Optional[dict]:
    """
    Rank the user's bots by current net profit (realized P/L + live unrealized
    mark-to-market) and return the most- and least-profitable ones with ROI.
    Returns None when the user has no bots. Only ever considers the bots passed
    in (which the caller has already scoped to the authenticated user).
    """
    stats = []
    for b in bots:
        # Include stopped bots that still hold open positions so capital/P&L
        # remains visible after a manual halt.
        if not b.running and not b.in_position:
            continue
        realized = float(b.realized_pnl or 0.0)
        unrealized = 0.0
        if b.in_position and b.avg_entry_price and b.shares_held:
            q = market_store.get_quote(db, b.broker or "alpaca", b.ticker or "")
            mark = q.price if (q and q.price) else b.avg_entry_price
            unrealized = (float(mark) - float(b.avg_entry_price)) * float(b.shares_held)
        net = round(realized + unrealized, 2)
        funds = float(b.funds_allocated or 0.0)
        roi = round((net / funds * 100.0), 2) if funds > 0 else 0.0
        stats.append({
            "bot_id": b.id, "name": b.name, "net_profit": net, "roi": roi,
            "funds_allocated": round(funds, 2), "in_position": bool(b.in_position),
            "trade_count": int(b.trade_count or 0),
        })
    if not stats:
        return None
    best = max(stats, key=lambda s: s["net_profit"])
    worst = min(stats, key=lambda s: s["net_profit"])
    return {"most_profitable": best, "least_profitable": worst, "bot_count": len(stats)}


@app.get("/api/market-status")
def market_status_endpoint():
    """US equities session status (open/closed + next open in UTC epoch).
    Public — the frontend renders next_open in the user's local timezone."""
    from app.market_hours import market_status
    return market_status()


@app.get("/api/portfolio/performance")
def portfolio_performance(mode: Optional[str] = None, u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    """
    Build the bot-performance dataset for the Portfolio graph:
      - funds_allocated : capital currently deployed in open bot positions
      - net_position    : cumulative realized P/L generated by the bots
      - series          : cumulative P/L line over time (UTC epoch seconds)
      - markers         : per-trade dots (buy=orange, profitable sell=green,
                          losing sell=red) with the details for the click tooltip
    The realized P/L per sell is reconstructed by replaying the trade ledger
    per bot (cost-basis accounting) since trades don't store gain directly.
    """
    # ── Paper vs Live separation ──────────────────────────────────────────
    # Everything below is scoped to ONE trading mode. Defaults to the account's
    # current trading_mode but can be requested explicitly via ?mode=paper|live.
    active_mode = (mode or u.trading_mode or "paper").lower()
    if active_mode not in ("paper", "live"):
        active_mode = "paper"
    # Open positions are only actually "held" in the account the user is live in,
    # so they only count toward the mode matching the current account state.
    include_open = active_mode == (u.trading_mode or "paper").lower()

    bots = db.query(Bot).filter(Bot.owner_id == u.id).all()
    bot_names = {b.id: b.name for b in bots}

    # funds_allocated = capital currently deployed in open bot positions (this mode)
    mode_bots = [
        b for b in bots
        if (b.mode or u.trading_mode or "paper").lower() == active_mode
    ]
    funds_allocated = sum(
        float(b.funds_allocated or 0.0) for b in mode_bots if b.in_position
    ) if include_open else 0.0

    trades = (db.query(Trade)
                .filter(Trade.owner_id == u.id, Trade.mode == active_mode)
                .order_by(Trade.created_at.asc(), Trade.id.asc())
                .all())

    def epoch_utc(dt) -> int:
        # created_at is stored as naive UTC (datetime.utcnow) — pin it to UTC.
        return int(dt.replace(tzinfo=timezone.utc).timestamp()) if dt else 0

    positions: dict = {}   # key -> {qty, cost}
    cumulative = 0.0
    series_map: dict = {}  # epoch -> cumulative (dedup so the line is strictly ascending)
    markers = []

    for t in trades:
        key = t.bot_id if t.bot_id is not None else f"manual:{t.ticker}"
        pos = positions.setdefault(key, {"qty": 0.0, "cost": 0.0})
        price = float(t.price or 0)
        side = (t.side or "").lower()
        ts = epoch_utc(t.created_at)
        dt_iso = t.created_at.replace(tzinfo=timezone.utc).isoformat() if t.created_at else None
        name = bot_names.get(t.bot_id, "Manual / Unlinked")

        if side == "buy":
            qty = float(t.qty) if t.qty else ((float(t.notional) / price) if (t.notional and price) else 0.0)
            amount = float(t.notional) if t.notional else qty * price
            pos["qty"] += qty
            pos["cost"] += amount
            markers.append({
                "time": ts, "datetime_utc": dt_iso, "type": "buy",
                "bot_id": t.bot_id, "bot_name": name,
                "ticker": t.ticker, "side": "buy", "amount": round(amount, 2),
                "price": price, "qty": round(qty, 8), "pnl": None,
            })
        elif side == "sell":
            qty = float(t.qty) if t.qty else pos["qty"]
            avg = (pos["cost"] / pos["qty"]) if pos["qty"] > 0 else price
            realized = (price - avg) * qty
            cumulative += realized
            pos["qty"] = max(0.0, pos["qty"] - qty)
            pos["cost"] = max(0.0, pos["cost"] - avg * qty)
            markers.append({
                "time": ts, "datetime_utc": dt_iso,
                "type": "sell_profit" if realized >= 0 else "sell_loss",
                "bot_id": t.bot_id, "bot_name": name, "ticker": t.ticker, "side": "sell",
                "amount": round(qty * price, 2), "cost_basis": round(avg * qty, 2),
                "price": price, "qty": round(qty, 8), "pnl": round(realized, 2),
            })

        series_map[ts] = round(cumulative, 2)

    # Bind every marker to the exact line value at its timestamp so the
    # scatter dots sit ON the portfolio line (chronologically AND vertically),
    # instead of floating at an arbitrary offset.
    for m in markers:
        m["value"] = series_map.get(m["time"], round(cumulative, 2))

    # Unrealized P/L: mark each open bot position against the live stored quote.
    # Only counts for the currently-live mode (see include_open above).
    unrealized = 0.0
    if include_open:
        for b in mode_bots:
            if b.in_position and b.avg_entry_price and b.shares_held:
                # Scattershot may store comma-joined tickers; skip unusable marks.
                ticker = (b.ticker or "").split(",")[0].strip()
                q = market_store.get_quote(db, b.broker or "alpaca", ticker) if ticker else None
                mark = float(q.price) if (q and q.price) else float(b.avg_entry_price)
                unrealized += (mark - float(b.avg_entry_price)) * float(b.shares_held)

    live_value = round(cumulative + unrealized, 2)
    series = [{"time": ts, "value": v} for ts, v in sorted(series_map.items())]
    if series:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        if now_ts > series[-1]["time"]:
            series.append({"time": now_ts, "value": live_value})
        else:
            series[-1] = {"time": series[-1]["time"], "value": live_value}
    markers.sort(key=lambda m: m["time"])

    return {
        "mode": active_mode,
        "funds_allocated": round(funds_allocated, 2),
        "net_position": round(cumulative, 2),
        "unrealized": round(unrealized, 2),
        "live_value": live_value,
        "trade_count": len(trades),
        "series": series,
        "markers": markers,
        "highlights": _bot_performance_highlights(db, bots),
    }


# Funding is sourced strictly from the live broker account now — manual
# deposit/withdrawal tracking has been removed. This is the single, exact
# message shown when a Box allocation cannot be backed by verified broker cash.
INSUFFICIENT_FUNDS_MSG = (
    "Insufficient funds in broker's account. Please make a deposit via your "
    "broker Alpaca or OKX before recording an allocation."
)


def _broker_available_funds(
    user: User,
    broker: Optional[str] = None,
    mode: Optional[str] = None,
) -> Optional[float]:
    """
    Verified, allocatable cash for a broker + paper/live mode.

    Defaults to the user's active broker / trading mode when omitted.
    Returns ``None`` when cash cannot be verified (no/invalid keys, etc.) —
    callers treat ``None`` as "skip the allocation guardrail".
    """
    broker = (broker or user.active_broker or "alpaca").lower()
    paper = ((mode or user.trading_mode or "paper").lower() == "paper")
    creds = resolve_credentials(user, broker, paper)
    try:
        info = get_account_info(broker=broker, paper=paper, **creds)
    except Exception:
        return None
    if not isinstance(info, dict) or info.get("error"):
        return None

    if broker == "alpaca":
        # "What is actually present" = real cash, not margin buying power.
        raw = info.get("cash")
        if raw is None:
            raw = info.get("buying_power")
        try:
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    # OKX: sum the USD-equivalent stablecoin balances (the tradable quote).
    balances = info.get("balances", {}) or {}
    total, found = 0.0, False
    for k in ("USDT", "USD", "USDC"):
        v = balances.get(k)
        if v is None:
            continue
        try:
            total += float(v)
            found = True
        except (TypeError, ValueError):
            continue
    return total if found else None


def _validate_cash_account_strategy_allocation(user: User, broker: str, strategy: str, funds: float) -> None:
    """Reject allocations that would violate GFV-safe low-balance strategy limits."""
    if strategy == "scattershot" and (broker or "alpaca").lower() != "alpaca":
        raise HTTPException(status_code=400, detail="Scattershot is available for Alpaca equities only.")
    if (broker or "alpaca").lower() != "alpaca":
        return
    try:
        acct_ctx = bot_engine._get_alpaca_account_context(user, broker)
    except Exception:
        # No keys / broker unreachable — skip GFV checks (common for paper / new accounts).
        return
    if not acct_ctx or acct_ctx.get("account_type") != "cash":
        return
    non_marginable = acct_ctx.get("non_marginable_buying_power")
    if strategy == "one_shot_daily":
        if non_marginable is None:
            raise HTTPException(status_code=400, detail="Unable to verify Alpaca non-marginable buying power for cash account.")
        if funds > (non_marginable + 1e-6):
            raise HTTPException(status_code=400, detail=(
                f"Cash Alpaca account non-marginable buying power (${non_marginable:.2f}) insufficient "
                f"for One-Shot Daily allocation of ${funds:.2f}. Reduce allocation or switch account type."))
    if strategy in ("micro_trader", "scattershot"):
        min_needed = 5.0 if strategy == "scattershot" else 1.0
        if non_marginable is None or non_marginable < min_needed - 1e-6:
            raise HTTPException(status_code=400, detail=(
                f"Cash Alpaca account non-marginable buying power insufficient for "
                f"{'$5 scattershot basket' if strategy == 'scattershot' else '$1 micro trades'}; "
                "add funds or use a margin account."))


@app.post("/bots")
async def create_bot(request: Request, u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    _enforce_bot_create_limit(u, db)
    data = await request.json()

    def _num(key):
        v = data.get(key)
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    is_auto = bool(data.get("is_auto", True))
    ticker_raw = (data.get("ticker") or "").strip().upper() or None
    strategy = ((data.get("low_balance_strategy") or "standard").strip().lower() or "standard")
    allowed_strategies = {"standard", "one_shot_daily", "micro_trader", "swing_trader", "scattershot"}
    if strategy not in allowed_strategies:
        strategy = "standard"
    # Fully autonomous = autonomous mode with NO fixed ticker → engine picks the asset.
    auto_select = bool(is_auto and not ticker_raw)

    try:
        funds = float(data.get("funds_allocated", 0.0) or 0.0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Allocate a valid funds amount greater than zero.")
    if funds <= 0:
        raise HTTPException(status_code=400, detail="Allocate a funds amount greater than zero.")
    if not auto_select and not ticker_raw:
        raise HTTPException(status_code=400, detail="Manual bots require a ticker symbol.")

    # ── Allocation guardrail ──────────────────────────────────────────────
    # When broker keys are configured, verify the requested funds don't exceed
    # the live broker balance minus what's already allocated to other bots.
    # When keys aren't set yet (common in paper mode / new accounts), skip the
    # check and let the user create bots freely — paper trading doesn't risk
    # real money so this is safe.
    broker_selected = (data.get("broker") or (u.active_broker or "alpaca")).lower()
    mode_now = (u.trading_mode or "paper").lower()
    available = _broker_available_funds(u, broker=broker_selected, mode=mode_now)
    if available is not None:
        already_allocated = sum(
            float(b.funds_allocated or 0.0)
            for b in db.query(Bot).filter(Bot.owner_id == u.id).all()
            if (b.mode or mode_now).lower() == mode_now
            and (b.broker or u.active_broker or "alpaca").lower() == broker_selected
        )
        if funds > (available - already_allocated) + 1e-6:
            raise HTTPException(status_code=400, detail=INSUFFICIENT_FUNDS_MSG)

    _validate_cash_account_strategy_allocation(u, broker_selected, strategy, funds)

    new_bot = Bot(
        owner_id=u.id,
        name=data.get("name") or (f"Autonomous {('OKX' if broker_selected == 'okx' else 'Alpaca')} Bot" if auto_select else "Unnamed Bot"),
        ticker=ticker_raw,  # None for fully autonomous
        broker=broker_selected,
        mode=(u.trading_mode or "paper"),   # assign the bot to the current account
        timeframe=data.get("timeframe") or "1h",
        funds_allocated=funds,
        is_auto=is_auto, # Added is_auto assignment to stop silent drops
        auto_select=auto_select,
        low_balance_strategy=strategy,
        buy_limit=_num("buy_limit"),
        sell_limit=_num("sell_limit"),
        min_profit_pct=_num("min_profit_pct"),
        first_buy_price=_num("first_buy_price"),
        running=True,   # start scanning immediately — user can pause any time
        trade_count=0
    )

    db.add(new_bot)
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.exception("bot create failed for user %s", u.id)
        raise HTTPException(status_code=500, detail=f"Could not create bot: {e}")
    db.refresh(new_bot)
    logger.info("[BOT CREATED] id=%s ticker=%s auto_select=%s broker=%s tf=%s funds=%s",
                new_bot.id, new_bot.ticker, new_bot.auto_select, new_bot.broker,
                new_bot.timeframe, new_bot.funds_allocated)
    return {"status": "bot created", "bot_id": new_bot.id}

@app.post("/bots/{bot_id}/toggle")
async def toggle_bot(bot_id: int, u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    bot = db.query(Bot).filter(Bot.id == bot_id, Bot.owner_id == u.id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found or unauthorized.")

    # Starting a bot must respect plan running limits (create limit alone is not enough
    # after a downgrade when older bots still exist).
    if not bot.running:
        limit = _user_bot_limit(u)
        if limit is not None:
            running_count = (
                db.query(Bot)
                .filter(Bot.owner_id == u.id, Bot.running == True)  # noqa: E712
                .count()
            )
            if running_count >= limit:
                plan_name = plan_display_name(getattr(u, "subscription_plan", None))
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"{plan_name} accounts can run up to {limit} bot"
                        f"{'s' if limit != 1 else ''} at once. Pause another bot or upgrade."
                    ),
                )

    bot.running = not bot.running
    db.commit()
    return {"status": f"bot {bot_id} toggled", "running": bot.running}

@app.delete("/bots/{bot_id}")
async def delete_bot(bot_id: int, u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    bot = db.query(Bot).filter(Bot.id == bot_id, Bot.owner_id == u.id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found or unauthorized.")

    # Sell out FIRST, then delete. The robust path cancels the symbol's open
    # orders (which reserve shares), waits for them to clear, then liquidates the
    # whole real position — so we never orphan live shares at the broker when the
    # bot is removed. With real keys, a liquidation failure aborts the delete
    # (fail safe) rather than silently dropping the bot while shares remain.
    liquidation = None
    if bot.in_position and (bot.shares_held or 0) > 0:
        try:
            liquidation = bot_engine.liquidate_bot(bot.id, reason="bot deleted")
        except Exception as e:
            logger.error("Liquidation before delete failed for bot %s: %s", bot_id, e)
            raise HTTPException(
                status_code=502,
                detail=f"Could not sell this bot's holdings before deletion: {e}. "
                       f"The bot was NOT deleted so its shares are not orphaned — please retry.")
        db.expire_all()  # liquidate_bot committed the close in its own session

    # Preserve the trade ledger when a bot is removed: every Trade (including the
    # liquidation sell just recorded) keeps its immutable bot_uuid for
    # attribution, but the bot_id foreign key must be cleared first or deleting
    # the bot row violates the trades_bot_id_fkey constraint (Postgres) -> 500.
    db.query(Trade).filter(Trade.bot_id == bot_id).update(
        {Trade.bot_id: None}, synchronize_session=False
    )
    db.query(Bot).filter(Bot.id == bot_id, Bot.owner_id == u.id).delete(synchronize_session=False)
    db.commit()
    return {"status": f"bot {bot_id} deleted", "liquidation": liquidation}

@app.post("/bots/{bot_id}/liquidate")
async def liquidate_bot_endpoint(bot_id: int, u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    """
    Manual Sell: liquidate everything a single bot is holding right now, without
    deleting the bot. Cancels the symbol's open orders, waits for them to clear,
    then closes the full position. Strictly scoped to the authenticated user's
    own bot. The bot's running/paused state is left unchanged.
    """
    bot = db.query(Bot).filter(Bot.id == bot_id, Bot.owner_id == u.id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found or unauthorized.")
    if not bot.in_position or (bot.shares_held or 0) <= 0:
        return {"status": "flat", "detail": "This bot is not holding any position to sell."}
    try:
        result = bot_engine.liquidate_bot(bot.id, reason="manual sell")
    except Exception as e:
        logger.error("Manual sell failed for bot %s: %s", bot_id, e)
        raise HTTPException(status_code=502, detail=f"Could not sell this bot's holdings: {e}")
    return {"status": "liquidated", "bot_id": bot_id, "details": result}

@app.post("/bots/{bot_id}/funds")
async def update_bot_funds(bot_id: int, request: Request, u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    """
    Dynamically adjust a bot's allocated capital. The new amount is validated
    against the user's live broker balance (reserving capital already committed
    to the user's *other* bots) so total allocations can never exceed the real
    balance. ``funds_allocated`` is read live every trading cycle, so this takes
    effect on the bot's next decision immediately (deployment = funds × fraction).
    Strictly scoped to the authenticated user's own bot.
    """
    bot = db.query(Bot).filter(Bot.id == bot_id, Bot.owner_id == u.id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found or unauthorized.")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body.")
    try:
        new_funds = round(float(data.get("funds_allocated")), 2)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Provide a valid funds amount.")
    if new_funds <= 0:
        raise HTTPException(status_code=400, detail="Allocated funds must be greater than zero.")

    # Same guardrail as bot creation: only enforce when keys are present.
    bot_broker = (bot.broker or u.active_broker or "alpaca").lower()
    bot_mode = (bot.mode or u.trading_mode or "paper").lower()
    available = _broker_available_funds(u, broker=bot_broker, mode=bot_mode)
    if available is not None:
        others_allocated = sum(
            float(b.funds_allocated or 0.0)
            for b in db.query(Bot).filter(Bot.owner_id == u.id, Bot.id != bot_id).all()
            if (b.mode or u.trading_mode or "paper").lower() == bot_mode
            and (b.broker or u.active_broker or "alpaca").lower() == bot_broker
        )
        if new_funds > (available - others_allocated) + 1e-6:
            raise HTTPException(status_code=400, detail=INSUFFICIENT_FUNDS_MSG)

    strategy = (bot.low_balance_strategy or "standard").lower()
    _validate_cash_account_strategy_allocation(u, bot_broker, strategy, new_funds)

    previous = float(bot.funds_allocated or 0.0)
    bot.funds_allocated = new_funds
    db.commit()
    logger.info("[FUNDS] Bot %s funds_allocated %.2f -> %.2f (user %s)", bot_id, previous, new_funds, u.id)
    # Nudge any open dashboards to refresh the portfolio view for this user only.
    try:
        bus.publish("portfolio_update", {"user_id": u.id}, user_id=u.id)
    except Exception:
        pass
    return {"status": "funds updated", "bot_id": bot_id,
            "funds_allocated": bot.funds_allocated, "previous": round(previous, 2)}

@app.post("/bots/{bot_id}/run-cycle")
async def run_bot_cycle_endpoint(bot_id: int, u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    bot = db.query(Bot).filter(Bot.id == bot_id, Bot.owner_id == u.id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found or unauthorized.")
        
    try:
        result = bot_engine.run_cycle(bot.id)
        return {"status": "cycle executed", "details": result}
    except Exception as e:
        logger.error("run-cycle failed for bot %s: %s", bot_id, e)
        raise HTTPException(status_code=500, detail=str(e))


# ── Live data streaming (Server-Sent Events) ─────────────────────────
@app.get("/stream/updates")
async def stream_updates(request: Request):
    """
    Server-Sent Events feed that streams market-quote, trade and portfolio
    events from the always-on background engine to the browser. Replaces manual
    refresh: the frontend subscribes once and reacts to pushes.

    Market quotes are broadcast to everyone; trade/portfolio events are scoped
    to the authenticated user.
    """
    # Use a short-lived session only to authenticate. A Depends(get_db) session
    # would stay open for the entire stream lifetime (minutes/hours), pinning a
    # pooled DB connection per open tab and eventually exhausting the pool.
    db = SessionLocal()
    try:
        user = get_current_user_from_cookie(request, db)
        user_id = user.id
    except HTTPException:
        user_id = None  # allow anonymous market-quote stream
    finally:
        db.close()

    queue = bus.subscribe(user_id=user_id)

    async def event_generator():
        # Initial hello so the client flips to "live" immediately.
        yield {"event": "hello", "data": '{"ok": true}'}
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=20.0)
                    yield {"event": payload.get("type", "message"),
                           "data": json.dumps(payload.get("data", {}))}
                except asyncio.TimeoutError:
                    # Heartbeat keeps proxies (Railway) from closing the stream.
                    yield {"event": "ping", "data": "{}"}
        finally:
            bus.unsubscribe(queue)

    return EventSourceResponse(event_generator())


@app.get("/api/market/quotes")
def market_quotes(broker: Optional[str] = None, u: User = Depends(get_current_user_from_cookie),
                  db: Session = Depends(get_db)):
    """Latest stored quotes from the background poller (database-of-record)."""
    rows = market_store.get_quotes(db, broker)
    return [market_store.quote_to_dict(r) for r in rows]


# ── Admin AI assistant (secure, human-in-the-loop) ───────────────────
class AIAuditModel(BaseModel):
    prompt: str
    paths: Optional[list[str]] = None


class AIProposalModel(BaseModel):
    proposal_id: str


def _require_admin(request: Request, db: Session) -> User:
    u = get_current_user_from_cookie(request, db)
    if not u.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    return u


@app.get("/admin/ai/status")
def ai_status(request: Request, db: Session = Depends(get_db)):
    _require_admin(request, db)
    return ai_assistant.provider_status()


@app.get("/admin/ai/files")
def ai_files(request: Request, db: Session = Depends(get_db)):
    _require_admin(request, db)
    return {"files": ai_assistant.list_repo_files()}


@app.get("/admin/ai/file")
def ai_file(path: str, request: Request, db: Session = Depends(get_db)):
    _require_admin(request, db)
    try:
        return {"path": path, "content": ai_assistant.read_file(path)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/admin/ai/audit")
def ai_audit(body: AIAuditModel, request: Request, db: Session = Depends(get_db)):
    """
    Run an audit/edit request. Returns findings + a unified-diff PREVIEW of any
    proposed changes plus a proposal_id. NOTHING is written until /admin/ai/approve.
    """
    _require_admin(request, db)
    if not (body.prompt or "").strip():
        raise HTTPException(status_code=400, detail="Prompt is required.")
    try:
        return ai_assistant.audit(body.prompt.strip(), body.paths)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("AI audit failed: %s", e)
        raise HTTPException(status_code=500, detail=f"AI audit failed: {e}")


@app.post("/admin/ai/approve")
def ai_approve(body: AIProposalModel, request: Request, db: Session = Depends(get_db)):
    """APPROVE — explicitly apply a previously-previewed proposal to disk."""
    if IS_PROD and not ADMIN_AI_WRITES:
        raise HTTPException(
            status_code=403,
            detail="AI code writes are disabled in production. Set ADMIN_AI_WRITES=1 only for controlled maintenance.",
        )
    u = _require_admin(request, db)
    try:
        result = ai_assistant.apply_proposal(body.proposal_id)
        logger.warning("[AI] Admin %s APPROVED proposal %s -> wrote %s",
                       u.email, body.proposal_id, result.get("written"))
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/admin/ai/deny")
def ai_deny(body: AIProposalModel, request: Request, db: Session = Depends(get_db)):
    """DENY — discard a pending proposal without writing anything."""
    _require_admin(request, db)
    return ai_assistant.deny_proposal(body.proposal_id)
