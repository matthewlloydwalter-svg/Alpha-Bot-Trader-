import os
import random
import resend
import logging
from typing import Optional, Literal
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from alpaca.trading.client import TradingClient

# --- 1. SETUP & SECRETS ---
load_dotenv()

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").lower() == "true"
BACKEND_API_TOKEN = os.getenv("BACKEND_API_TOKEN")
resend.api_key = os.getenv("RESEND_API_KEY")

if not all([ALPACA_API_KEY, ALPACA_SECRET_KEY]):
    raise RuntimeError("Missing Alpaca API Keys in Environment Variables!")

# --- 2. CLIENT INITIALIZATION ---
trading_client = TradingClient(
    api_key=ALPACA_API_KEY, 
    secret_key=ALPACA_SECRET_KEY, 
    paper=ALPACA_PAPER
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("alphabot")

app = FastAPI()

# Mount the static directory for CSS/JS
# Make sure you have a folder named 'static' in your root
app.mount("/static", StaticFiles(directory="static"), name="static")

# Setup templates to look in the 'templates' folder
templates = Jinja2Templates(directory="templates")

@app.get("/")
async def get_index(request: Request):
    # This will now correctly look for index.html inside the 'templates' folder
    return templates.TemplateResponse("index.html", {"request": request})

# If you have other routes (like trading or auth), put them below here
# Make sure they are indented exactly like the 'get_index' function


# ── AUTH ENDPOINTS ────────────────────────────────────────────────
@app.post("/signup")
def signup(data: dict, db: Session = Depends(get_db)):
    email = data.get("email")
    password = data.get("password")
    
    if not email or not password:
        raise HTTPException(status_code=400, detail="Missing email or password")
        
    existing_user = db.query(User).filter(User.email == email).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")
        
    hashed = hash_password(password)
    new_user = User(email=email, hashed_password=hashed)
    db.add(new_user)
    db.commit()
    return {"status": "success", "message": "Account created successfully"}


@app.post("/login")
def login(data: dict, response: Response, db: Session = Depends(get_db)):
    email = data.get("email")
    password = data.get("password")
    
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Invalid email or password")
        
    token = create_session_token(user_id=user.id, email=user.email)
    response.set_cookie(key="session_token", value=token, httponly=True)
    return {"status": "success", "message": "Logged in successfully"}


@app.post("/logout")
def logout(response: Response):
    response.delete_cookie("session_token")
    return {"status": "success", "message": "Logged out successfully"}


# ── TRADING & PORTFOLIO ENDPOINTS ─────────────────────────────────
@app.get("/account")
def get_user_account(current_user: User = Depends(get_current_user)):
    """Fetches real-time balances using user credentials from the database."""
    broker_name = getattr(current_user, "trading_mode", "alpaca") 
    
    try:
        account_info = brokers.get_account_info(
            broker=broker_name,
            alpaca_key=getattr(current_user, "alpaca_key", None),
            alpaca_secret=getattr(current_user, "alpaca_secret", None),
            okx_key=getattr(current_user, "okx_key", None),
            okx_secret=getattr(current_user, "okx_secret", None),
            okx_passphrase=getattr(current_user, "okx_passphrase", None),
            paper=(getattr(current_user, "trading_mode", "paper") == "paper")
        )
        return account_info
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Broker integration error: {str(e)}")


@app.get("/positions")
def get_user_positions(current_user: User = Depends(get_current_user)):
    """Returns an array of current open holdings for the dashboard views."""
    return []


# ── BOT SCHEDULER TRIGGER RUNNER ──────────────────────────────────
@app.post("/bots/{bot_id}/run-cycle")
def run_bot_cycle(bot_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Manually executes a trading evaluation cycle using the bot engine."""
    bot = db.query(Bot).filter(Bot.id == bot_id, Bot.owner_id == current_user.id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot configuration not found")
        
    # Placeholder metrics parameters for your bot engine processing rules
    current_price = 100.0
    recent_prices = [98.0, 99.0, 100.0]
    news_summary = "Market conditions stable."
    
    try:
        # FIXED: Argument order perfectly matches bot_engine.py signature (db first, then bot)
        result = bot_engine.run_bot_cycle(db, bot, current_price, recent_prices, news_summary)
        return {"status": "success", "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
