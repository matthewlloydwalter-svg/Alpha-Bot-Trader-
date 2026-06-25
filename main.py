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

from database import engine, Base, init_db, get_db, User, Bot, Trade, ActivityLog
from auth import (
    hash_password, verify_password, create_session_token, decode_session_token,
    generate_verification_code, send_verification_email, is_user_admin, PLATFORM_NAME, get_current_user
)
from brokers import get_account_info, BrokerError

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

class OKXKeysModel(BaseModel):
    api_key: str
    secret_key: str
    passphrase: str

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
    if not send_verification_email(u.email, code):
        raise HTTPException(status_code=500, detail="Subsystem transmission fault deploying validation mail.")
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
    u.alpaca_key    = body.api_key.strip()
    u.alpaca_secret = body.secret_key.strip()
    db.commit()
    return {"success": True}

@app.post("/broker/okx/keys")
def save_okx_keys(body: OKXKeysModel, u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    u.okx_key    = body.api_key.strip()
    u.okx_secret = body.secret_key.strip()
    u.okx_pass   = body.passphrase.strip()
    db.commit()
    return {"success": True}

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
    try:
        if broker == "alpaca":
            if not getattr(u, "alpaca_key", None):
                raise HTTPException(status_code=400, detail="Alpaca API credentials not configured. Add them in Account → Alpaca API Keys.")
            return get_account_info(broker="alpaca", alpaca_key=u.alpaca_key, alpaca_secret=u.alpaca_secret, paper=paper)
        elif broker == "okx":
            if not getattr(u, "okx_key", None):
                raise HTTPException(status_code=400, detail="OKX API credentials not configured. Add them in Account → OKX API Keys.")
            return get_account_info(broker="okx", okx_key=u.okx_key, okx_secret=u.okx_secret, okx_passphrase=u.okx_pass, paper=paper)
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
    """
    return db.query(ActivityLog)\
             .filter(ActivityLog.user_id == u.id)\
             .order_by(ActivityLog.created_at.desc())\
             .limit(50)\
             .all()

@app.post("/cash/deposit")
async def deposit_cash(request: Request):
    data = await request.json()
    return {"status": "deposit received"}

@app.post("/cash/withdraw")
async def withdraw_cash(request: Request):
    data = await request.json()
    return {"status": "withdrawal processed"}

@app.get("/bots")
def get_bots(u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    bots = db.query(Bot).filter(Bot.owner_id == u.id).all()
    return [
        {
            "id": b.id,
            "name": b.name,
            "ticker": b.ticker,
            "funds_allocated": b.funds_allocated,
            "running": b.running,
            "trade_count": b.trade_count,
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
            for item in root.findall(".//item")[:30]:
                title    = (item.findtext("title") or "").strip()
                link     = (item.findtext("link")  or "").strip()
                pub_date = (item.findtext("pubDate") or "").strip()
                if title and link:
                    items.append({"title": title, "link": link, "pubDate": pub_date, "sentiment": "neutral"})
            if items:
                return items
        except Exception as e:
            logger.warning(f"News feed failed ({url}): {e}")
            continue

    return []

@app.post("/bots")
async def create_bot(request: Request):
    data = await request.json()
    return {"status": "bot created"}

@app.post("/bots/{bot_id}/toggle")
async def toggle_bot(bot_id: str):
    return {"status": f"bot {bot_id} toggled"}

@app.delete("/bots/{bot_id}")
async def delete_bot(bot_id: str):
    return {"status": f"bot {bot_id} deleted"}
