import os
import logging
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from sqlalchemy.orm import Session

load_dotenv()

from database import engine, Base, init_db, get_db, SessionLocal, User, Bot, Trade, ActivityLog
from auth import (
    hash_password, verify_password, create_session_token, decode_session_token,
    generate_verification_code, send_verification_email, is_user_admin, PLATFORM_NAME,
    get_current_user, EmailError
)
from brokers import get_account_info, get_spot_price, BrokerError
from market_data import get_market_analysis
from markets_universe import MARKET_UNIVERSE
from credentials import resolve_credentials, has_credentials, keys_payload
import bot_engine # Imported bot engine to wire up the run-cycle logic

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("alphabot")

app = FastAPI(title=f"{PLATFORM_NAME} Engine Core")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

if not os.path.exists(STATIC_DIR):
    os.makedirs(STATIC_DIR)
if not os.path.exists(TEMPLATES_DIR):
    os.makedirs(TEMPLATES_DIR)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

init_db()

# Models
class AuthModel(BaseModel):
    email: str
    password: str

class VerificationChallengeModel(BaseModel):
    code: str

class AlpacaKeysModel(BaseModel):
    api_key: str
    secret_key: str
    mode: Optional[str] = "paper"   # "paper" or "live"

class OKXKeysModel(BaseModel):
    api_key: str
    secret_key: str
    passphrase: str
    mode: Optional[str] = "paper"   # "paper" or "live"

def get_current_user_from_cookie(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get("session_token")
    if not token:
        raise HTTPException(status_code=401, detail="Session matrix signature missing.")
    payload = decode_session_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Session signature validation expired.")
    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user:
        raise HTTPException(status_code=401, detail="User record context purged.")
    return user

@app.get("/", response_class=HTMLResponse)
def index_pane(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "PLATFORM_NAME": PLATFORM_NAME})

@app.get("/terms", response_class=HTMLResponse)
def terms_pane():
    return HTMLResponse(
        f"<html><head><title>{PLATFORM_NAME} — Terms of Service</title>"
        "<style>body{font-family:system-ui;max-width:760px;margin:40px auto;padding:0 20px;"
        "background:#0d0f14;color:#e8eaf0;line-height:1.7}h1{color:#4d9fff}</style></head>"
        f"<body><h1>{PLATFORM_NAME} — Terms of Service</h1>"
        "<p>This platform provides automated trading tools for educational and "
        "informational purposes. Trading involves substantial risk of loss. You are "
        "solely responsible for your broker credentials, capital, and trading decisions. "
        "Paper trading is strongly recommended before deploying live capital.</p>"
        "<p>By creating an account you acknowledge that the operators are not liable "
        "for trading losses, and that automated strategies (including stop-losses and "
        "capital rotation) may not execute as intended during market disruptions.</p>"
        "</body></html>"
    )


@app.get("/admin", response_class=HTMLResponse)
def admin_pane(request: Request, db: Session = Depends(get_db)):
    try:
        u = get_current_user_from_cookie(request, db)
        if not u.is_admin:
            return RedirectResponse(url="/")
        return templates.TemplateResponse("admin.html", {"request": request, "PLATFORM_NAME": PLATFORM_NAME})
    except:
        return templates.TemplateResponse("index.html", {"request": request, "PLATFORM_NAME": PLATFORM_NAME})

@app.post("/auth/signup")
def register_endpoint(body: AuthModel, response: Response, db: Session = Depends(get_db)):
    normalized_email = body.email.strip().lower()
    existing = db.query(User).filter(User.email == normalized_email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered in system databases.")
    
    new_user = User(
        email=normalized_email,
        hashed_password=hash_password(body.password),
        is_admin=is_user_admin(normalized_email),
        email_verified=False
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    
    token = create_session_token(new_user.id, new_user.email)
    response.set_cookie(key="session_token", value=token, httponly=True, max_age=86400)
    return {"id": new_user.id, "email": new_user.email, "is_admin": new_user.is_admin, "email_verified": new_user.email_verified}

@app.post("/auth/login")
def login_endpoint(body: AuthModel, response: Response, db: Session = Depends(get_db)):
    normalized_email = body.email.strip().lower()
    user = db.query(User).filter(User.email == normalized_email).first()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Invalid credential combination supplied.")
        
    token = create_session_token(user.id, user.email)
    response.set_cookie(key="session_token", value=token, httponly=True, max_age=86400)
    
    return {
        "id": user.id,
        "email": user.email,
        "is_admin": user.is_admin,
        "email_verified": user.email_verified,
        "trading_mode": user.trading_mode or "paper",
        "active_broker": user.active_broker or "alpaca",
        "total_deposited": user.total_deposited or 0.0,
        "total_withdrawn": user.total_withdrawn or 0.0,
    }

@app.get("/auth/me")
def current_user_endpoint(u: User = Depends(get_current_user_from_cookie)):
    return {
        "id": u.id,
        "email": u.email,
        "is_admin": u.is_admin,
        "email_verified": u.email_verified,
        "trading_mode": u.trading_mode or "paper",
        "active_broker": u.active_broker or "alpaca",
        "total_deposited": u.total_deposited or 0.0,
        "total_withdrawn": u.total_withdrawn or 0.0,
    }

@app.post("/auth/logout")
def logout_endpoint(response: Response):
    response.delete_cookie("session_token")
    return {"success": True}

@app.post("/auth/trigger-verification")
def trigger_verification(u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    code = generate_verification_code()
    u.verification_code = code
    db.commit()
    try:
        send_verification_email(u.email, code)
    except EmailError as e:
        # Surface the ACTUAL reason (SMTP not configured / bad app password / etc.)
        logger.error("Verification email failed for %s: %s", u.email, e)
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error("Unexpected email error for %s: %s", u.email, e)
        raise HTTPException(status_code=500, detail=f"Unexpected mail error: {e}")
    return {"success": True}

@app.post("/auth/confirm-verification")
def confirm_verification(body: VerificationChallengeModel, u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    if not u.verification_code or u.verification_code != body.code.strip():
        raise HTTPException(status_code=400, detail="Invalid challenge matching hash sequence provided.")
    u.email_verified = True
    u.verification_code = None
    db.commit()
    return {"success": True}

@app.post("/broker/alpaca/keys")
def save_alpaca_keys(body: AlpacaKeysModel, u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    mode = (body.mode or "paper").lower()
    if mode not in ("paper", "live"):
        raise HTTPException(status_code=400, detail="mode must be 'paper' or 'live'.")
    key, secret = body.api_key.strip(), body.secret_key.strip()
    if mode == "paper":
        u.alpaca_key_paper, u.alpaca_secret_paper = key, secret
        # Keep legacy columns in sync so existing flows stay consistent.
        u.alpaca_key, u.alpaca_secret = key, secret
    else:
        u.alpaca_key_live, u.alpaca_secret_live = key, secret
    db.commit()
    logger.info("[KEYS] Saved Alpaca %s keys for user %s", mode, u.id)
    return {"success": True, "mode": mode}

@app.post("/broker/okx/keys")
def save_okx_keys(body: OKXKeysModel, u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    mode = (body.mode or "paper").lower()
    if mode not in ("paper", "live"):
        raise HTTPException(status_code=400, detail="mode must be 'paper' or 'live'.")
    key, secret, passphrase = body.api_key.strip(), body.secret_key.strip(), body.passphrase.strip()
    if mode == "paper":
        u.okx_key_paper, u.okx_secret_paper, u.okx_pass_paper = key, secret, passphrase
        u.okx_key, u.okx_secret, u.okx_pass = key, secret, passphrase
    else:
        u.okx_key_live, u.okx_secret_live, u.okx_pass_live = key, secret, passphrase
    db.commit()
    logger.info("[KEYS] Saved OKX %s keys for user %s", mode, u.id)
    return {"success": True, "mode": mode}

@app.get("/broker/keys")
def get_broker_keys(u: User = Depends(get_current_user_from_cookie)):
    """
    Return the user's stored keys per exchange/mode so the Account UI can
    auto-populate the boxes. Only ever returns the requesting user's own keys.
    """
    return keys_payload(u)

@app.post("/broker/trading-mode")
async def set_trading_mode(request: Request, u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    data = await request.json()
    mode = data.get("mode", "paper")
    if mode not in ["paper", "live"]:
        raise HTTPException(status_code=400, detail="Invalid mode. Use 'paper' or 'live'.")
    u.trading_mode = mode
    db.commit()
    return {"trading_mode": u.trading_mode}

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
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/broker/trades-ledger")
def get_trades_ledger(u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    # Query database and map trade results clean
    records = db.query(Trade).filter(Trade.owner_id == u.id).order_by(Trade.created_at.desc()).all()
    return [
        {
            "id": r.id,
            "ticker": r.ticker,
            "side": r.side,
            "qty": r.qty,
            "price": r.price,
            "mode": r.mode,
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
        "total_deposited": 500000,
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
    return [
        {
            "id": b.id,
            "name": b.name,
            "ticker": b.ticker,
            "auto_select": b.auto_select,
            "broker": b.broker,
            "timeframe": b.timeframe,
            "funds_allocated": b.funds_allocated,
            "is_auto": b.is_auto, # Added to expose the column to the frontend
            "running": b.running,
            "trade_count": b.trade_count,
            "in_position": b.in_position,
            "avg_entry_price": b.avg_entry_price,
            "shares_held": b.shares_held,
            "realized_pnl": b.realized_pnl,
            "stop_price": b.stop_price,
            "take_profit_price": b.take_profit_price,
            "last_signal": b.last_signal,
            "last_pattern_summary": b.last_pattern_summary,
            "last_analysis_at": b.last_analysis_at.isoformat() if b.last_analysis_at else None,
        }
        for b in bots
    ]

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
            from news_analysis import classify_headline_sentiment
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
                     u: User = Depends(get_current_user_from_cookie)):
    """
    The heart of the clickable Market Dashboard.

    Fetches historical candles for the asset, runs them through the shared
    pattern-analysis brain (indicators + structural patterns + a normalized
    signal) and returns a fully-prepared payload the UI can render directly.
    The bots consume this exact same analysis via market_data.
    """
    ex = exchange.lower()
    if ex not in MARKET_UNIVERSE:
        raise HTTPException(status_code=404, detail=f"Unknown exchange '{exchange}'.")

    cfg = MARKET_UNIVERSE[ex]
    meta = next((i for i in cfg["items"] if i["symbol"].upper() == symbol.upper()), None)
    asset_name = meta["name"] if meta else symbol.upper()
    display = meta["display"] if meta else symbol.upper()

    limit = max(50, min(int(limit), 500))
    paper = (u.trading_mode or "paper") == "paper"
    creds = resolve_credentials(u, ex, paper)

    try:
        analysis = get_market_analysis(
            broker=ex, symbol=symbol, timeframe=timeframe, limit=limit,
            alpaca_key=creds.get("alpaca_key"), alpaca_secret=creds.get("alpaca_secret"),
            okx_key=creds.get("okx_key"), okx_secret=creds.get("okx_secret"),
            okx_passphrase=creds.get("okx_passphrase"),
            paper=paper,
        )
    except BrokerError as e:
        logger.warning("Dashboard data unavailable for %s:%s — %s", ex, symbol, e)
        raise HTTPException(status_code=502, detail=str(e))

    # Find the user's bot(s) trading this asset to surface "Internal Bot Status".
    bot_status = _collect_bot_status(u.id, ex, symbol)

    payload = analysis.to_dict()
    payload.update({
        "asset_name": asset_name,
        "display_symbol": display,
        "asset_class": cfg["asset_class"],
        "quote": cfg["quote"],
        "bot_status": bot_status,
    })
    return payload


def _collect_bot_status(user_id: int, exchange: str, symbol: str) -> dict:
    """Summarize what the internal bots think/are doing for this asset."""
    from database import SessionLocal
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


@app.post("/cash/deposit")
async def deposit_cash(request: Request, u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    data = await request.json()
    amount = float(data.get("amount", 0.0))
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Deposit amount must be greater than zero.")
    
    u.total_deposited = (u.total_deposited or 0.0) + amount
    db.commit()
    return {"status": "deposit received", "new_balance": u.total_deposited}

@app.post("/cash/withdraw")
async def withdraw_cash(request: Request, u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    data = await request.json()
    amount = float(data.get("amount", 0.0))
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Withdrawal amount must be greater than zero.")
        
    u.total_withdrawn = (u.total_withdrawn or 0.0) + amount
    db.commit()
    return {"status": "withdrawal processed", "new_balance": u.total_withdrawn}

@app.post("/bots")
async def create_bot(request: Request, u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    data = await request.json()

    def _num(key):
        v = data.get(key)
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    is_auto = bool(data.get("is_auto", True))
    ticker_raw = (data.get("ticker") or "").strip().upper() or None
    # Fully autonomous = autonomous mode with NO fixed ticker → engine picks the asset.
    auto_select = bool(is_auto and not ticker_raw)

    funds = float(data.get("funds_allocated", 0.0) or 0.0)
    if funds <= 0:
        raise HTTPException(status_code=400, detail="Allocate a funds amount greater than zero.")
    if not auto_select and not ticker_raw:
        raise HTTPException(status_code=400, detail="Manual bots require a ticker symbol.")

    new_bot = Bot(
        owner_id=u.id,
        name=data.get("name") or (f"Autonomous {('OKX' if (data.get('broker') or u.active_broker)=='okx' else 'Alpaca')} Bot" if auto_select else "Unnamed Bot"),
        ticker=ticker_raw,  # None for fully autonomous
        broker=data.get("broker") or (u.active_broker or "alpaca"),
        timeframe=data.get("timeframe") or "1h",
        funds_allocated=funds,
        is_auto=is_auto, # Added is_auto assignment to stop silent drops
        auto_select=auto_select,
        buy_limit=_num("buy_limit"),
        sell_limit=_num("sell_limit"),
        min_profit_pct=_num("min_profit_pct"),
        first_buy_price=_num("first_buy_price"),
        running=False,
        trade_count=0
    )

    db.add(new_bot)
    db.commit()
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
        
    bot.running = not bot.running
    db.commit()
    return {"status": f"bot {bot_id} toggled", "running": bot.running}

@app.delete("/bots/{bot_id}")
async def delete_bot(bot_id: int, u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    bot = db.query(Bot).filter(Bot.id == bot_id, Bot.owner_id == u.id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found or unauthorized.")
        
    db.delete(bot)
    db.commit()
    return {"status": f"bot {bot_id} deleted"}

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


# ── Optional background autonomous engine ────────────────────────────
# Disabled by default for safety. Set BOT_AUTORUN=1 (and an interval via
# BOT_SCAN_INTERVAL seconds) to let the engine continuously scan + act on
# every running bot without a manual trigger.
def _start_autonomous_engine():
    import threading
    import time

    interval = int(os.getenv("BOT_SCAN_INTERVAL", "300"))

    def _loop():
        logger.info("[ENGINE] Autonomous scanner started (interval=%ss)", interval)
        while True:
            time.sleep(interval)
            try:
                summary = bot_engine.run_all_active_bots()
                logger.info("[ENGINE] Autonomous scan complete: %s", summary)
            except Exception as e:
                logger.error("[ENGINE] Autonomous scan error: %s", e)

    t = threading.Thread(target=_loop, name="alphabot-engine", daemon=True)
    t.start()


@app.on_event("startup")
def _on_startup():
    if os.getenv("BOT_AUTORUN", "0") in ("1", "true", "True", "yes"):
        _start_autonomous_engine()
    else:
        logger.info("[ENGINE] Autonomous scanner disabled (set BOT_AUTORUN=1 to enable).")
