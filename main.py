import os
import smtplib
import logging
import os
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
from alpaca.common.exceptions import APIError

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

# --- 3. SECURITY & HELPERS ---
def get_current_user_email(request: Request) -> str:
    return request.session.get("user_email") if hasattr(request, "session") else ""

async def verify_token(x_api_token: Optional[str] = Header(None)):
    if not x_api_token or x_api_token != BACKEND_API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing API token")
    return True

def send_email_alert(subject: str, body: str) -> bool:
    smtp_user = os.getenv("SMTP_USERNAME")
    smtp_pass = os.getenv("SMTP_PASSWORD")
    alert_to = os.getenv("ALERT_TO_EMAIL")
    if not smtp_user or not smtp_pass or not alert_to: return False
    msg = MIMEMultipart()
    msg["From"], msg["To"], msg["Subject"] = smtp_user, alert_to, subject
    msg.attach(MIMEText(body, "plain"))
    try:
        with smtplib.SMTP(os.getenv("SMTP_HOST", "smtp.gmail.com"), int(os.getenv("SMTP_PORT", "587"))) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, alert_to, msg.as_string())
        return True
    except: return False

# --- 4. CLIENTS & CONFIGURATION ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("alphabot")
trading_client = TradingClient(api_key=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY, paper=ALPACA_PAPER)

if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

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
@app.post("/send-verification")
async def send_verification(email: str):
    # 1. Generate a random 6-digit code
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
        
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    admin_emails = os.getenv("ADMIN_EMAILS", "").split(",")
    if get_current_user_email(request) not in admin_emails:
        raise HTTPException(status_code=403, detail="Access Denied")
    return templates.TemplateResponse("admin.html", {"request": request})

@app.get("/admin/users", dependencies=[Depends(verify_token)])
async def get_admin_users(request: Request):
    admin_emails = os.getenv("ADMIN_EMAILS", "").split(",")
    return [{"email": email} for email in admin_emails]

@app.get("/account", dependencies=[Depends(verify_token)])
def get_account():
    account = trading_client.get_account()
    return {
        "cash": account.cash, 
        "portfolio_value": account.portfolio_value, 
        "buying_power": account.buying_power
    }

@app.get("/positions", dependencies=[Depends(verify_token)])
def get_positions():
    return [{"symbol": p.symbol, "qty": p.qty} for p in trading_client.get_all_positions()]

def _build_order_request(order: OrderRequest, side: OrderSide):
    tif = TimeInForce.DAY if order.time_in_force == "day" else TimeInForce.GTC
    common_kwargs = {"symbol": order.symbol.upper(), "side": side, "time_in_force": tif}
    if order.qty: common_kwargs["qty"] = order.qty
    else: common_kwargs["notional"] = order.notional
    if order.order_type == "market": return MarketOrderRequest(**common_kwargs)
    return LimitOrderRequest(**common_kwargs, limit_price=order.limit_price)

@app.post("/buy", response_model=OrderResponse, dependencies=[Depends(verify_token)])
def buy(order: OrderRequest):
    req = _build_order_request(order, OrderSide.BUY)
    result = trading_client.submit_order(order_data=req)
    return OrderResponse(success=True, order_id=str(result.id), symbol=order.symbol.upper(), side="buy", status=str(result.status), message="Order submitted")

@app.post("/sell", response_model=OrderResponse, dependencies=[Depends(verify_token)])
def sell(order: OrderRequest):
    req = _build_order_request(order, OrderSide.SELL)
    result = trading_client.submit_order(order_data=req)
    return OrderResponse(success=True, order_id=str(result.id), symbol=order.symbol.upper(), side="sell", status=str(result.status), message="Order submitted")

@app.get("/orders", dependencies=[Depends(verify_token)])
def get_orders():
    return [{"id": str(o.id), "symbol": o.symbol, "status": str(o.status)} for o in trading_client.get_orders()]

@app.delete("/orders/{order_id}", dependencies=[Depends(verify_token)])
def cancel_order(order_id: str):
    trading_client.cancel_order_by_id(order_id)
    return {"success": True}

@app.post("/test-email", dependencies=[Depends(verify_token)])
def test_email():
    ok = send_email_alert("Test", "Working")
    return {"success": ok}
