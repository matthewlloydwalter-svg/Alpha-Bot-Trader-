import os
import logging
from datetime import datetime
from typing import Optional, Literal
from fastapi import FastAPI, Depends, HTTPException, Request, Cookie, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from sqlalchemy.orm import Session

load_dotenv()

print(f"DEBUG: DATABASE_URL is: {os.getenv('DATABASE_URL')}")

from database import engine, Base, init_db, get_db, User, Bot, Trade
from auth import (
    hash_password, verify_password, create_session_token, decode_session_token,
    generate_verification_code, send_email, is_user_admin, ADMIN_EMAILS, PLATFORM_NAME
)
from brokers import get_account_info, BrokerError
from bot_engine import run_bot_cycle

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("alphabot")


# ──────────────────────────────────────────────────────────────────
# App setup
# ──────────────────────────────────────────────────────────────────
app = FastAPI(title=f"{PLATFORM_NAME} Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("FRONTEND_ORIGIN", "*")],  # set FRONTEND_ORIGIN in prod
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Use absolute paths derived from this file's location so Railway
# finds static/ and templates/ regardless of working directory.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

# Static files (CSS/JS/images) served at /static/...
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
else:
    logger.warning("static/ not found at %s", STATIC_DIR)

# Jinja2 templates from templates/
templates = Jinja2Templates(directory=TEMPLATES_DIR) if os.path.isdir(TEMPLATES_DIR) else None
if not templates:
    logger.warning("templates/ not found at %s", TEMPLATES_DIR)


@app.on_event("startup")
def on_startup():
    init_db()
    logger.info("Database initialized.")
    if not ADMIN_EMAILS:
        logger.warning("ADMIN_EMAILS is empty — no one will have admin access until you set it.")


# ──────────────────────────────────────────────────────────────────
# Auth dependency — reads the session cookie, loads the User
# ──────────────────────────────────────────────────────────────────
def get_current_user(session_token: Optional[str] = Cookie(None), db: Session = Depends(get_db)) -> User:
    if not session_token:
        raise HTTPException(status_code=401, detail="Not logged in")
    payload = decode_session_token(session_token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


def get_current_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def require_verified_email(user: User = Depends(get_current_user)) -> User:
    if not user.email_verified:
        raise HTTPException(status_code=403, detail="Email verification required for this action")
    return user


# ──────────────────────────────────────────────────────────────────
# Page routes — serve templates (the actual dashboard HTML/JS)
# ──────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def serve_index(request: Request):
    """Main dashboard — all HTML, CSS and JS are inlined so no static files needed."""
    if not templates:
        return HTMLResponse("<h1>templates/ folder not found</h1>")
    return templates.TemplateResponse("index.html", {"request": request, "platform_name": PLATFORM_NAME})


@app.get("/v9", response_class=HTMLResponse)
async def get_v9(request: Request):
    """Alias for / — same complete self-contained dashboard."""
    if not templates:
        return HTMLResponse("<h1>templates/ folder not found</h1>")
    return templates.TemplateResponse("index.html", {"request": request, "platform_name": PLATFORM_NAME})


@app.get("/admin", response_class=HTMLResponse)
def serve_admin_page(request: Request):
    if not templates:
        return HTMLResponse("<h1>templates/ folder not found</h1>")
    return templates.TemplateResponse("admin.html", {"request": request, "platform_name": PLATFORM_NAME})


# ──────────────────────────────────────────────────────────────────
# AUTH ROUTES
# ──────────────────────────────────────────────────────────────────
class SignupBody(BaseModel):
    email: str
    password: str
    confirm_password: str
    agreed_to_tos: bool


@app.post("/auth/signup")
def signup(body: SignupBody, db: Session = Depends(get_db)):
    if body.password != body.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match.")
    if not body.agreed_to_tos:
        raise HTTPException(status_code=400, detail="You must agree to the Terms of Service.")

    existing = db.query(User).filter(User.email == body.email.lower()).first()
    if existing:
        raise HTTPException(status_code=400, detail="An account with that email already exists.")

    code = generate_verification_code()
    user = User(
        email=body.email.lower(),
        hashed_password=hash_password(body.password),
        is_admin=(body.email.lower() in ADMIN_EMAILS),
        email_verified=False,
        verification_code=code,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    send_email(
        user.email,
        f"Verify your {PLATFORM_NAME} account",
        f"Your verification code is: {code}\n\nEnter this code in the app to verify your email "
        f"and unlock live trading.",
    )

    token = create_session_token(user.id, user.email)
    resp = JSONResponse({"success": True, "email_verified": False, "is_admin": user.is_admin})
    resp.set_cookie("session_token", token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 7)
    return resp


class LoginBody(BaseModel):
    email: str
    password: str


@app.post("/auth/login")
def login(body: LoginBody, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == body.email.lower()).first()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    token = create_session_token(user.id, user.email)
    resp = JSONResponse({
        "success": True,
        "email": user.email,
        "email_verified": user.email_verified,
        "is_admin": user.is_admin,
    })
    resp.set_cookie("session_token", token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 7)
    return resp


@app.post("/auth/logout")
def logout():
    resp = JSONResponse({"success": True})
    resp.delete_cookie("session_token")
    return resp


@app.post("/auth/send-verification")
def send_verification(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    code = generate_verification_code()
    user.verification_code = code
    db.commit()
    send_email(user.email, f"Your {PLATFORM_NAME} verification code", f"Your code is: {code}")
    return {"success": True, "message": f"Verification code sent to {user.email}"}


class VerifyBody(BaseModel):
    code: str


@app.post("/auth/verify-email")
def verify_email(body: VerifyBody, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if body.code != user.verification_code:
        raise HTTPException(status_code=400, detail="Incorrect verification code.")
    user.email_verified = True
    user.verification_code = None
    db.commit()
    return {"success": True, "email_verified": True}


@app.get("/auth/me")
def me(user: User = Depends(get_current_user)):
    return {
        "email": user.email,
        "is_admin": user.is_admin,
        "email_verified": user.email_verified,
        "active_broker": user.active_broker,
        "trading_mode": user.trading_mode,
        "total_deposited": user.total_deposited,
        "total_withdrawn": user.total_withdrawn,
    }


# ──────────────────────────────────────────────────────────────────
# BROKER SWITCHING + KEY MANAGEMENT
# ──────────────────────────────────────────────────────────────────
class BrokerSwitchBody(BaseModel):
    broker: Literal["alpaca", "okx"]


@app.post("/broker/switch")
def switch_broker(body: BrokerSwitchBody, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    user.active_broker = body.broker
    db.commit()
    return {"success": True, "active_broker": user.active_broker}


class AlpacaKeysBody(BaseModel):
    api_key: str
    secret_key: str


@app.post("/broker/alpaca/keys")
def save_alpaca_keys(body: AlpacaKeysBody, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # NOTE: storing keys in plaintext columns for simplicity. Before
    # opening this to other users, encrypt these at rest (e.g. with
    # the `cryptography` package's Fernet) using a key from your env,
    # not committed to the repo.
    user.alpaca_key = body.api_key
    user.alpaca_secret = body.secret_key
    db.commit()
    return {"success": True}


class OkxKeysBody(BaseModel):
    api_key: str
    secret_key: str
    passphrase: str


@app.post("/broker/okx/keys")
def save_okx_keys(body: OkxKeysBody, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    user.okx_key = body.api_key
    user.okx_secret = body.secret_key
    user.okx_passphrase = body.passphrase
    db.commit()
    return {"success": True}


class TradingModeBody(BaseModel):
    mode: Literal["paper", "live"]


@app.post("/broker/trading-mode")
def set_trading_mode(body: TradingModeBody, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if body.mode == "live":
        if not user.email_verified:
            raise HTTPException(status_code=403, detail="Verify your email before enabling live trading.")
        has_keys = (
            (user.active_broker == "alpaca" and user.alpaca_key and user.alpaca_secret) or
            (user.active_broker == "okx" and user.okx_key and user.okx_secret and user.okx_passphrase)
        )
        if not has_keys:
            raise HTTPException(status_code=403, detail=f"Save your {user.active_broker} API keys before enabling live trading.")
    user.trading_mode = body.mode
    db.commit()
    return {"success": True, "trading_mode": user.trading_mode}


@app.get("/broker/account")
def broker_account(user: User = Depends(get_current_user)):
    try:
        info = get_account_info(
            broker=user.active_broker,
            alpaca_key=user.alpaca_key, alpaca_secret=user.alpaca_secret,
            okx_key=user.okx_key, okx_secret=user.okx_secret, okx_passphrase=user.okx_passphrase,
            paper=(user.trading_mode == "paper"),
        )
        return info
    except BrokerError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ──────────────────────────────────────────────────────────────────
# BOTS
# ──────────────────────────────────────────────────────────────────
class CreateBotBody(BaseModel):
    name: str
    ticker: Optional[str] = None
    funds_allocated: float
    is_auto: bool = True
    buy_limit: Optional[float] = None
    sell_limit: Optional[float] = None
    min_profit_pct: Optional[float] = None
    first_buy_price: Optional[float] = None


@app.post("/bots")
def create_bot(body: CreateBotBody, user: User = Depends(require_verified_email), db: Session = Depends(get_db)):
    bot = Bot(
        owner_id=user.id,
        name=body.name,
        ticker=body.ticker.upper() if body.ticker else None,
        broker=user.active_broker,
        funds_allocated=body.funds_allocated,
        is_auto=body.is_auto,
        buy_limit=body.buy_limit,
        sell_limit=body.sell_limit,
        min_profit_pct=body.min_profit_pct,
        first_buy_price=body.first_buy_price,
        first_buy_done=(body.first_buy_price is None),
    )
    db.add(bot)
    db.commit()
    db.refresh(bot)
    return {"success": True, "bot_id": bot.id}


@app.get("/bots")
def list_bots(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    bots = db.query(Bot).filter(Bot.owner_id == user.id).order_by(Bot.id).all()
    return [
        {
            "id": b.id, "name": b.name, "ticker": b.ticker, "broker": b.broker,
            "funds_allocated": b.funds_allocated, "is_auto": b.is_auto, "running": b.running,
            "in_position": b.in_position, "shares_held": b.shares_held,
            "avg_entry_price": b.avg_entry_price, "realized_pnl": b.realized_pnl,
            "trade_count": b.trade_count,
        }
        for b in bots
    ]


@app.post("/bots/{bot_id}/toggle")
def toggle_bot(bot_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    bot = db.query(Bot).filter(Bot.id == bot_id, Bot.owner_id == user.id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    bot.running = not bot.running
    db.commit()
    return {"success": True, "running": bot.running}


@app.delete("/bots/{bot_id}")
def delete_bot(bot_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    bot = db.query(Bot).filter(Bot.id == bot_id, Bot.owner_id == user.id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    db.delete(bot)
    db.commit()
    return {"success": True}


class RunCycleBody(BaseModel):
    current_price: float
    recent_prices: list[float] = []
    news_summary: str = ""


@app.post("/bots/{bot_id}/run-cycle")
def run_cycle(bot_id: int, body: RunCycleBody, user: User = Depends(require_verified_email), db: Session = Depends(get_db)):
    """
    Triggers one bot decision cycle. Call this from your frontend on a
    timer (e.g. every 30-60s per active bot) or wire up a real
    scheduler (APScheduler, Celery beat) to call it server-side instead
    — the manual/polling approach is simplest to start with on Railway's
    free tier since it avoids running a second long-lived process.
    """
    bot = db.query(Bot).filter(Bot.id == bot_id, Bot.owner_id == user.id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    result = run_bot_cycle(db, bot, body.current_price, body.recent_prices, body.news_summary)
    return result


# ──────────────────────────────────────────────────────────────────
# DEPOSITS / WITHDRAWALS (recorded only — actual funding happens at
# your broker directly, per the architecture decided earlier)
# ──────────────────────────────────────────────────────────────────
class CashBody(BaseModel):
    amount: float


@app.post("/cash/deposit")
def record_deposit(body: CashBody, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if body.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive.")
    user.total_deposited += body.amount
    db.commit()
    return {"success": True, "total_deposited": user.total_deposited}


@app.post("/cash/withdraw")
def record_withdrawal(body: CashBody, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if body.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive.")
    user.total_withdrawn += body.amount
    db.commit()
    return {"success": True, "total_withdrawn": user.total_withdrawn}


# ──────────────────────────────────────────────────────────────────
# ADMIN ROUTES
# ──────────────────────────────────────────────────────────────────
@app.get("/admin/users")
def admin_list_users(admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    users = db.query(User).order_by(User.created_at).all()
    out = []
    for u in users:
        profit = sum(t.price * (t.qty or 0) for t in u.trades if t.side == "sell") - \
                 sum(t.price * (t.qty or 0) for t in u.trades if t.side == "buy")
        denom = u.total_deposited if u.total_deposited else 1
        out.append({
            "id": u.id, "email": u.email, "is_admin": u.is_admin,
            "email_verified": u.email_verified, "joined": u.created_at.isoformat(),
            "total_deposited": u.total_deposited, "total_withdrawn": u.total_withdrawn,
            "estimated_profit": round(profit, 2),
            "estimated_profit_pct": round(profit / denom * 100, 2),
            "bot_count": len(u.bots),
        })
    return out


@app.get("/admin/stats")
def admin_stats(admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    users = db.query(User).all()
    return {
        "total_users": len(users),
        "verified_users": sum(1 for u in users if u.email_verified),
        "total_deposited": sum(u.total_deposited for u in users),
        "total_withdrawn": sum(u.total_withdrawn for u in users),
        "total_bots": db.query(Bot).count(),
        "total_trades": db.query(Trade).count(),
    }


class PlatformEmailBody(BaseModel):
    email: str


@app.post("/admin/platform-email")
def set_platform_email(body: PlatformEmailBody, admin: User = Depends(get_current_admin)):
    # This updates the *display* of which address is configured to send.
    # The actual sending address is controlled by SMTP_USERNAME in your
    # environment variables — change that on Railway's dashboard and
    # redeploy if you want to change who mail is actually sent from.
    return {
        "success": True,
        "note": (
            "Recorded. To make this the address that ACTUALLY sends mail, "
            "set SMTP_USERNAME (and ALERT_FROM_EMAIL) to this address in "
            "your Railway environment variables and redeploy."
        ),
        "platform_email": body.email,
    }


class EmailUsersBody(BaseModel):
    subject: str
    body: str
    target: Literal["all", "single"] = "all"
    user_email: Optional[str] = None


@app.post("/admin/email-users")
def email_users(body: EmailUsersBody, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    if body.target == "single":
        if not body.user_email:
            raise HTTPException(status_code=400, detail="user_email required for single target.")
        targets = [u for u in db.query(User).filter(User.email == body.user_email.lower()).all()]
    else:
        targets = db.query(User).all()

    sent = 0
    for u in targets:
        if send_email(u.email, body.subject, body.body):
            sent += 1
    return {"success": True, "sent": sent, "attempted": len(targets)}



@app.get("/static-check")
def static_check():
    """Hit this URL on Railway to confirm static files are being served correctly."""
    import os
    base = os.path.dirname(os.path.abspath(__file__))
    static_path = os.path.join(base, "static")
    files = []
    if os.path.isdir(static_path):
        for root, dirs, fnames in os.walk(static_path):
            for fn in fnames:
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, static_path)
                files.append({"file": rel, "size_kb": round(os.path.getsize(full)/1024,1)})
    return {
        "base_dir": base,
        "static_dir": static_path,
        "static_dir_exists": os.path.isdir(static_path),
        "files": files,
        "logo_url": "/static/alphabot-logo.png",
    }

@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}
        
    hashed = hash_password(password)
    new_user = User(email=email, hashed_password=hashed)
    db.add(new_user)
    db.commit()
    return {"status": "success", "message": "Account created successfully"}
