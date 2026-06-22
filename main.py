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

# --- 3. APP INITIALIZATION ---
app = FastAPI(title="AlphaBot Trading Backend")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 4. MODELS ---
class OrderRequest(BaseModel):
    symbol: str
    qty: Optional[float] = None
    notional: Optional[float] = None
    order_type: Literal["market", "limit"] = "market"
    limit_price: Optional[float] = None
    time_in_force: Literal["day", "gtc"] = "day"

# --- 5. ROUTES ---
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/signup")
async def signup(request: Request):
    # Add database logic here
    return {"status": "success"}

@app.get("/account")
async def get_account():
    try:
        account = trading_client.get_account()
        return {
            "cash": float(account.cash), 
            "portfolio_value": float(account.portfolio_value), 
            "buying_power": float(account.buying_power)
        }
    except Exception as e:
        logger.error(f"Account fetch error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/positions")
async def get_positions():
    try:
        positions = trading_client.get_all_positions()
        return [{"symbol": p.symbol, "qty": float(p.qty)} for p in positions]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/send-verification")
async def send_verification(email: str):
    code = str(random.randint(100000, 999999))
    try:
        resend.Emails.send({
            "from": "alerts@yourdomain.com",
            "to": email,
            "subject": "Your AlphaBot Verification Code",
            "html": f"<p>Your verification code is: <strong>{code}</strong></p>" 
        })
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
