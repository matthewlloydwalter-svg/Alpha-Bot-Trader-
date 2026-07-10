import os
import json
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

load_dotenv()

from app.database import engine, Base, init_db, get_db, SessionLocal, User, Bot, Trade, ActivityLog, MarketQuote
from app.auth import (
    hash_password, verify_password, create_session_token, decode_session_token,
    generate_verification_code, send_verification_email, is_user_admin, PLATFORM_NAME,
    get_current_user, EmailError
)
from app.config import SESSION_COOKIE_SECURE
from app.brokers import get_account_info, get_spot_price, BrokerError
from app.market_data import get_market_analysis
from app.markets_universe import MARKET_UNIVERSE
from app.credentials import resolve_credentials, has_credentials, keys_payload
from app import bot_engine  # Imported bot engine to wire up the run-cycle logic
from app import market_store, ai_assistant
from app.realtime import bus
from app import scheduler as engine_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("alphabot")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Capture the running event loop so background worker threads can stream
    # events to SSE subscribers, then start the always-on background engine.
    try:
        bus.bind_loop(asyncio.get_running_loop())
    except RuntimeError:
        pass
    engine_scheduler.start_scheduler()
    logger.info("[STARTUP] %s engine core online.", PLATFORM_NAME)
    try:
        yield
    finally:
        engine_scheduler.shutdown_scheduler()


app = FastAPI(title=f"{PLATFORM_NAME} Engine Core", lifespan=lifespan)

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
        raise HTTPException(status_code=401, detail="Session matrix signature missing.")
    payload = decode_session_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Session signature validation expired.")
    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Session signature validation expired.")
    user = db.query(User).filter(User.id == user_id).first()
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
    except Exception:
        return templates.TemplateResponse("index.html", {"request": request, "PLATFORM_NAME": PLATFORM_NAME})

@app.post("/auth/signup")
def register_endpoint(body: AuthModel, response: Response, request: Request, db: Session = Depends(get_db)):
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
    response.set_cookie(key="session_token", value=token, **_cookie_kwargs(request=request))
    return {"id": new_user.id, "email": new_user.email, "is_admin": new_user.is_admin, "email_verified": new_user.email_verified}

@app.post("/auth/login")
def login_endpoint(body: AuthModel, response: Response, request: Request, db: Session = Depends(get_db)):
    normalized_email = body.email.strip().lower()
    user = db.query(User).filter(User.email == normalized_email).first()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Invalid credential combination supplied.")
        
    token = create_session_token(user.id, user.email)
    response.set_cookie(key="session_token", value=token, **_cookie_kwargs(request=request))

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
def logout_endpoint(response: Response, request: Request):
    response.delete_cookie("session_token", samesite="lax", secure=_cookie_kwargs(request=request)["secure"])
    return {"success": True}

@app.post("/auth/trigger-verification")
def trigger_verification(u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    code = generate_verification_code()
    u.verification_code = code
    db.commit()
    try:
        send_verification_email(u.email, code)
    except EmailError as e:
        logger.warning("Verification email failed for %s: %s", u.email, e)
        # Return 200 with smtp_not_configured so the frontend can show a
        # helpful inline message rather than a red error toast.
        return {"success": False, "smtp_not_configured": True, "detail": str(e)}
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
    return [
        {
            "id": b.id,
            "name": b.name,
            "ticker": b.ticker,
            "auto_select": b.auto_select,
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
            "stop_price": b.stop_price,
            "take_profit_price": b.take_profit_price,
            "realized_pnl": b.realized_pnl,
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
                     u: User = Depends(get_current_user_from_cookie),
                     db: Session = Depends(get_db)):
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
    funds_allocated = sum(
        float(b.funds_allocated or 0.0) for b in bots if b.in_position
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
        for b in bots:
            if b.in_position and b.avg_entry_price and b.shares_held:
                q = market_store.get_quote(db, b.broker or "alpaca", b.ticker or "")
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


def _broker_available_funds(user: User) -> Optional[float]:
    """
    Verified, allocatable cash from the user's *active* broker session (Alpaca
    or OKX, paper or live per their current Trading Mode). This is the single
    source of truth for what can be allocated to a Box.

    Returns the available cash as a float, or ``None`` when it cannot be
    verified (no/invalid keys, broker unreachable, no quote-currency balance) —
    callers treat ``None`` as "no verified funds" and block the allocation.
    """
    broker = (user.active_broker or "alpaca").lower()
    paper = (user.trading_mode or "paper") == "paper"
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

    # ── Allocation guardrail ──────────────────────────────────────────────
    # When broker keys are configured, verify the requested funds don't exceed
    # the live broker balance minus what's already allocated to other bots.
    # When keys aren't set yet (common in paper mode / new accounts), skip the
    # check and let the user create bots freely — paper trading doesn't risk
    # real money so this is safe.
    available = _broker_available_funds(u)
    if available is not None:
        already_allocated = sum(
            float(b.funds_allocated or 0.0)
            for b in db.query(Bot).filter(Bot.owner_id == u.id).all()
        )
        if funds > (available - already_allocated) + 1e-6:
            raise HTTPException(status_code=400, detail=INSUFFICIENT_FUNDS_MSG)

    new_bot = Bot(
        owner_id=u.id,
        name=data.get("name") or (f"Autonomous {('OKX' if (data.get('broker') or u.active_broker)=='okx' else 'Alpaca')} Bot" if auto_select else "Unnamed Bot"),
        ticker=ticker_raw,  # None for fully autonomous
        broker=data.get("broker") or (u.active_broker or "alpaca"),
        mode=(u.trading_mode or "paper"),   # assign the bot to the current account
        timeframe=data.get("timeframe") or "1h",
        funds_allocated=funds,
        is_auto=is_auto, # Added is_auto assignment to stop silent drops
        auto_select=auto_select,
        buy_limit=_num("buy_limit"),
        sell_limit=_num("sell_limit"),
        min_profit_pct=_num("min_profit_pct"),
        first_buy_price=_num("first_buy_price"),
        running=True,   # start scanning immediately — user can pause any time
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
    available = _broker_available_funds(u)
    if available is not None:
        others_allocated = sum(
            float(b.funds_allocated or 0.0)
            for b in db.query(Bot).filter(Bot.owner_id == u.id, Bot.id != bot_id).all()
        )
        if new_funds > (available - others_allocated) + 1e-6:
            raise HTTPException(status_code=400, detail=INSUFFICIENT_FUNDS_MSG)

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
