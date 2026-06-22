import os
import smtplib
import logging
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

# 1. INITIALIZE APP FIRST
app = FastAPI(title="AlphaBot Trading Backend", version="1.0.0")

# 2. ADMIN GATEKEEPER
def get_current_user_email(request: Request) -> str:
    # This is a placeholder for your future authentication service (e.g. Clerk/Supabase)
    return request.session.get("user_email") if hasattr(request, "session") else ""

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    # Fetch your list from Railway Variables
    admin_emails = os.getenv("ADMIN_EMAILS", "").split(",")
    current_user_email = get_current_user_email(request)
    
    if current_user_email not in admin_emails:
        raise HTTPException(status_code=403, detail="Access Denied")
        
    return templates.TemplateResponse("admin.html", {"request": request})

Python
@app.get("/admin/users", dependencies=[Depends(verify_token)])
async def get_admin_users(request: Request):
    # Verify Admin Access
    admin_emails = os.getenv("ADMIN_EMAILS", "").split(",")
    current_user_email = get_current_user_email(request)
    if current_user_email not in admin_emails:
        raise HTTPException(status_code=403, detail="Access Denied")
     return [{"email": email} for email in admin_emails]

# 3. NOW PROCEED TO YOUR EXISTING CODE
# ──────────────────────────────────────────────────────────────────
# 1. LOAD SECRETS FROM .env
# ──────────────────────────────────────────────────────────────────
    

# ──────────────────────────────────────────────────────────────────
# 1. LOAD SECRETS FROM .env
# ──────────────────────────────────────────────────────────────────
load_dotenv()

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").lower() == "true"
BACKEND_API_TOKEN = os.getenv("BACKEND_API_TOKEN")

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
ALERT_FROM_EMAIL = os.getenv("ALERT_FROM_EMAIL", SMTP_USERNAME)
ALERT_TO_EMAIL = os.getenv("ALERT_TO_EMAIL")

REQUIRED_VARS = {
    "ALPACA_API_KEY": ALPACA_API_KEY,
    "ALPACA_SECRET_KEY": ALPACA_SECRET_KEY,
    "BACKEND_API_TOKEN": BACKEND_API_TOKEN,
}
missing = [k for k, v in REQUIRED_VARS.items() if not v]
if missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}.")

# ──────────────────────────────────────────────────────────────────
# 2. LOGGING & CLIENTS
# ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("alphabot")

class OrderRequest(BaseModel):
    symbol: str = Field(..., description="Ticker symbol, e.g. 'AAPL'")
    qty: Optional[float] = Field(None, description="Number of shares.")
    notional: Optional[float] = Field(None, description="Dollar amount.")
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
    
trading_client = TradingClient(api_key=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY, paper=ALPACA_PAPER)

# ──────────────────────────────────────────────────────────────────
# 3. FASTAPI APP & SETUP
# ──────────────────────────────────────────────────────────────────
app = FastAPI(title="AlphaBot Trading Backend", version="1.0.0")

# --- MOUNT STATIC AND TEMPLATES ---
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")

# --- CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ──────────────────────────────────────────────────────────────────
# 4. ROUTES
# ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Serves your dashboard index.html."""
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api-status")
def api_status():
    """Endpoint for checking service status."""
    return {"service": "AlphaBot Trading Backend", "mode": "PAPER" if ALPACA_PAPER else "LIVE", "status": "running"}

# --- (All your existing trading functions remain exactly the same) ---
def verify_token(x_api_token: Optional[str] = Header(None)):
    if not x_api_token or x_api_token != BACKEND_API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing API token")
    return True

def send_email_alert(subject: str, body: str) -> bool:
    if not SMTP_USERNAME or not SMTP_PASSWORD or not ALERT_TO_EMAIL: return False
    msg = MIMEMultipart()
    msg["From"], msg["To"], msg["Subject"] = ALERT_FROM_EMAIL, ALERT_TO_EMAIL, subject
    msg.attach(MIMEText(body, "plain"))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls(); server.login(SMTP_USERNAME, SMTP_PASSWORD); server.sendmail(ALERT_FROM_EMAIL, ALERT_TO_EMAIL, msg.as_string())
        return True
    except: return False

@app.get("/account", dependencies=[Depends(verify_token)])
def get_account():
    account = trading_client.get_account()
    return {"cash": account.cash, "portfolio_value": account.portfolio_value, "buying_power": account.buying_power}

@app.get("/positions", dependencies=[Depends(verify_token)])
def get_positions():
    return [{"symbol": p.symbol, "qty": p.qty} for p in trading_client.get_all_positions()]

@app.post("/buy", dependencies=[Depends(verify_token)])
def buy(order: dict):
    # (Existing buy logic)
    return {"success": True}

@app.post("/sell", dependencies=[Depends(verify_token)])
def sell(order: dict):
    # (Existing sell logic)
    return {"success": True}

# ... (Keep the rest of your original functions here) ...    """Returns your current Alpaca account info — balance, buying power, etc."""
    try:
        account = trading_client.get_account()
        return {
            "account_number": account.account_number,
            "status": str(account.status),
            "currency": account.currency,
            "cash": account.cash,
            "portfolio_value": account.portfolio_value,
            "buying_power": account.buying_power,
            "equity": account.equity,
            "trading_blocked": account.trading_blocked,
            "paper_trading": ALPACA_PAPER,
        }
    except APIError as e:
        raise HTTPException(status_code=502, detail=f"Alpaca API error: {e}")


@app.get("/positions", dependencies=[Depends(verify_token)])
def get_positions():
    """Returns all currently open positions."""
    try:
        positions = trading_client.get_all_positions()
        return [
            {
                "symbol": p.symbol,
                "qty": p.qty,
                "avg_entry_price": p.avg_entry_price,
                "current_price": p.current_price,
                "unrealized_pl": p.unrealized_pl,
                "unrealized_plpc": p.unrealized_plpc,
                "market_value": p.market_value,
            }
            for p in positions
        ]
    except APIError as e:
        raise HTTPException(status_code=502, detail=f"Alpaca API error: {e}")


def _build_order_request(order: OrderRequest, side: OrderSide):
    """Shared helper that builds a Market or Limit order request object."""
    tif = TimeInForce.DAY if order.time_in_force == "day" else TimeInForce.GTC

    if not order.qty and not order.notional:
        raise HTTPException(status_code=400, detail="Provide either qty or notional.")
    if order.qty and order.notional:
        raise HTTPException(status_code=400, detail="Provide qty OR notional, not both.")

    common_kwargs = dict(
        symbol=order.symbol.upper(),
        side=side,
        time_in_force=tif,
    )
    if order.qty:
        common_kwargs["qty"] = order.qty
    else:
        common_kwargs["notional"] = order.notional

    if order.order_type == "market":
        return MarketOrderRequest(**common_kwargs)
    else:
        if order.limit_price is None:
            raise HTTPException(status_code=400, detail="limit_price is required for limit orders.")
        return LimitOrderRequest(**common_kwargs, limit_price=order.limit_price)


@app.post("/buy", response_model=OrderResponse, dependencies=[Depends(verify_token)])
def buy(order: OrderRequest):
    """
    Places a BUY order on Alpaca.
    Call this from your bot logic whenever it decides to enter a position.
    """
    req = _build_order_request(order, OrderSide.BUY)

    try:
        result = trading_client.submit_order(order_data=req)
    except APIError as e:
        send_email_alert(
            subject=f"❌ AlphaBot — BUY order FAILED for {order.symbol}",
            body=f"Bot: {order.bot_name or 'manual'}\nSymbol: {order.symbol}\nError: {e}",
        )
        raise HTTPException(status_code=502, detail=f"Alpaca rejected the order: {e}")

    qty_desc = f"{order.qty} shares" if order.qty else f"${order.notional} notional"
    send_email_alert(
        subject=f"✅ AlphaBot — BOUGHT {order.symbol}",
        body=(
            f"Bot: {order.bot_name or 'manual'}\n"
            f"Symbol: {order.symbol}\n"
            f"Amount: {qty_desc}\n"
            f"Order type: {order.order_type}\n"
            f"Mode: {'PAPER' if ALPACA_PAPER else 'LIVE — REAL MONEY'}\n"
            f"Order ID: {result.id}\n"
            f"Status: {result.status}\n"
        ),
    )
    logger.info("BUY order submitted: %s (%s) — id=%s", order.symbol, qty_desc, result.id)

    return OrderResponse(
        success=True,
        order_id=str(result.id),
        symbol=order.symbol.upper(),
        side="buy",
        status=str(result.status),
        message=f"Buy order submitted for {order.symbol}",
    )


@app.post("/sell", response_model=OrderResponse, dependencies=[Depends(verify_token)])
def sell(order: OrderRequest):
    """
    Places a SELL order on Alpaca.
    Call this from your bot logic whenever it decides to exit a position.
    """
    req = _build_order_request(order, OrderSide.SELL)

    try:
        result = trading_client.submit_order(order_data=req)
    except APIError as e:
        send_email_alert(
            subject=f"❌ AlphaBot — SELL order FAILED for {order.symbol}",
            body=f"Bot: {order.bot_name or 'manual'}\nSymbol: {order.symbol}\nError: {e}",
        )
        raise HTTPException(status_code=502, detail=f"Alpaca rejected the order: {e}")

    qty_desc = f"{order.qty} shares" if order.qty else f"${order.notional} notional"
    send_email_alert(
        subject=f"✅ AlphaBot — SOLD {order.symbol}",
        body=(
            f"Bot: {order.bot_name or 'manual'}\n"
            f"Symbol: {order.symbol}\n"
            f"Amount: {qty_desc}\n"
            f"Order type: {order.order_type}\n"
            f"Mode: {'PAPER' if ALPACA_PAPER else 'LIVE — REAL MONEY'}\n"
            f"Order ID: {result.id}\n"
            f"Status: {result.status}\n"
        ),
    )
    logger.info("SELL order submitted: %s (%s) — id=%s", order.symbol, qty_desc, result.id)

    return OrderResponse(
        success=True,
        order_id=str(result.id),
        symbol=order.symbol.upper(),
        side="sell",
        status=str(result.status),
        message=f"Sell order submitted for {order.symbol}",
    )


@app.get("/orders", dependencies=[Depends(verify_token)])
def get_orders(status: Literal["open", "closed", "all"] = "open"):
    """Lists recent orders, useful for your bot or UI to check order history."""
    try:
        orders = trading_client.get_orders()
        return [
            {
                "id": str(o.id),
                "symbol": o.symbol,
                "side": str(o.side),
                "qty": o.qty,
                "filled_qty": o.filled_qty,
                "status": str(o.status),
                "submitted_at": str(o.submitted_at),
            }
            for o in orders
        ]
    except APIError as e:
        raise HTTPException(status_code=502, detail=f"Alpaca API error: {e}")


@app.delete("/orders/{order_id}", dependencies=[Depends(verify_token)])
def cancel_order(order_id: str):
    """Cancels a specific open order by ID."""
    try:
        trading_client.cancel_order_by_id(order_id)
        return {"success": True, "message": f"Order {order_id} cancelled"}
    except APIError as e:
        raise HTTPException(status_code=502, detail=f"Alpaca API error: {e}")


@app.post("/test-email", dependencies=[Depends(verify_token)])
def test_email():
    """Hit this once after setup to confirm your email alerts are wired correctly."""
    ok = send_email_alert(
        subject="🔔 AlphaBot — Test alert",
        body="If you're reading this, your email alerts are working correctly.",
    )
    if not ok:
        raise HTTPException(
            status_code=500,
            detail="Email failed to send — check SMTP_USERNAME, SMTP_PASSWORD, and ALERT_TO_EMAIL in your .env file.",
        )
    return {"success": True, "message": "Test email sent — check your inbox."}
