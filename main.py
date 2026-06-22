import os
import smtplib
import logging
import random
import resend
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# --- 1. SETUP & SECRETS ---
load_dotenv()
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").lower() == "true"
BACKEND_API_TOKEN = os.getenv("BACKEND_API_TOKEN")
resend.api_key = os.getenv("RESEND_API_KEY")

if not all([ALPACA_API_KEY, ALPACA_SECRET_KEY, BACKEND_API_TOKEN]):
    raise RuntimeError("Missing critical Environment Variables in Railway!")

# --- 2. APP INITIALIZATION ---
app = FastAPI(title="AlphaBot Trading Backend", version="1.0.0")

# Mount the 'static' folder so your CSS and JS files can be found
app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# --- 3. SECURITY & HELPERS ---
def get_current_user_email(request: Request) -> str:
    return request.session.get("user_email") if hasattr(request, "session") else ""

async def verify_token(x_api_token: Optional[str] = Header(None)):
    if not x_api_token or x_api_token != BACKEND_API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing API token")
    return True

# --- 4. CLIENTS & CONFIGURATION ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("alphabot")
trading_client = TradingClient(api_key=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY, paper=ALPACA_PAPER)

class OrderRequest(BaseModel):
    symbol: str = Field(..., description="Ticker symbol")
    qty: Optional[float] = Field(None)
    notional: Optional[float] = Field(None)
    order_type: Literal["market", "limit"] = "market"
    limit_price: Optional[float] = Field(None)
    time_in_force: Literal["day", "gtc"] = "day"
    bot_name: Optional[str] = Field(None)

class OrderResponse(BaseModel):
    success: bool
    order_id: Optional[str] = None
    symbol: str
    side: str
    status: Optional[str] = None
    message: str

# --- 5. ROUTES ---
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/signup")
async def signup(request: Request):
    data = await request.json()
    email = data.get("email")
    password = data.get("password")
     return {"status": "success"}

@app.post("/send-verification")
async def send_verification(email: str):
    code = str(random.randint(100000, 999999))
    try:
        resend.Emails.send({
            "from": "alerts@yourdomain.com",
            "to": email,
            "subject": "Your AlphaBot Verification Code",
            "html": f"<p>Your verification code is: <strong>{code}</strong></p>" })
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/account")
def get_account():
    # Note: Added simple auth handling or token verification as needed
    account = trading_client.get_account()
    return {
        "cash": account.cash, 
        "portfolio_value": account.portfolio_value, 
        "buying_power": account.buying_power
    }

@app.get("/positions")
def get_positions():
    return [{"symbol": p.symbol, "qty": p.qty} for p in trading_client.get_all_positions()]

# ... (Add back your other buy/sell/order routes here)
