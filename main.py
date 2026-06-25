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

mount("/static", StaticFiles(directory="static"), name="static")

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

class UpdateKeysModel(BaseModel):
    alpaca_key: str
    alpaca_secret: str

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
        # --- NEW FIELDS FOR FRONTEND ---
        "trading_mode": "paper",
        "active_broker": "alpaca",
        "total_deposited": 0.0,
        "total_withdrawn": 0.0
    }

@app.get("/auth/me")
def current_user_endpoint(u: User = Depends(get_current_user_from_cookie)):
    return {
        "id": u.id, 
        "email": u.email, 
        "is_admin": u.is_admin, 
        "email_verified": u.email_verified,
        # --- NEW FIELDS FOR FRONTEND ---
        "trading_mode": "paper",
        "active_broker": "alpaca",
        "total_deposited": 0.0,
        "total_withdrawn": 0.0
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

@app.post("/broker/trading-mode")
async def update_trading_mode(request: Request):
    data = await request.json()
    return {"status": "success", "message": "Trading mode updated"}

@app.post("/broker/switch")
async def switch_broker(request: Request):
    data = await request.json()
    return {"status": "switched"}

@app.post("/broker/keys")
def save_broker_keys(body: UpdateKeysModel, u: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    u.alpaca_key = body.alpaca_key.strip()
    u.alpaca_secret = body.alpaca_secret.strip()
    db.commit()
    return {"success": True}

@app.get("/broker/account")
def get_broker_account(u: User = Depends(get_current_user_from_cookie)):
    if not getattr(u, 'alpaca_key', None):
        raise HTTPException(status_code=400, detail="API credentials unconfigured.")
    try:
        return get_account_info(broker="alpaca", alpaca_key=u.alpaca_key, alpaca_secret=u.alpaca_secret, paper=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# ════════════════════ BACKEND HISTORICAL TRADES LEDGER ENDPOINT ════════════════════
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
