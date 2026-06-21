"""
AlphaBot Trading Backend
========================
A secure FastAPI backend that executes real (or paper) trades on Alpaca
on your behalf, and sends you an email alert every time a trade fires.

Run locally with:
    uvicorn main:app --reload --port 8000

This file is intentionally written as a single module so it's easy to
read top-to-bottom. Once you're comfortable with it, feel free to split
it into multiple files (routers/, services/, etc).
"""

import os
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.common.exceptions import APIError


# ──────────────────────────────────────────────────────────────────
# 1. LOAD SECRETS FROM .env  (never hardcode keys in this file!)
# ──────────────────────────────────────────────────────────────────
load_dotenv()  # reads the .env file sitting next to this script

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").lower() == "true"

# A separate, locally-generated secret that your frontend must send
# with every request, so random people on the internet can't hit
# your /buy and /sell endpoints and trade with your money.
BACKEND_API_TOKEN = os.getenv("BACKEND_API_TOKEN")

# Email alert settings (use a separate "alerts" email, not your main one)
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")          # your alerts email address
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")          # app password, NOT your real password
ALERT_FROM_EMAIL = os.getenv("ALERT_FROM_EMAIL", SMTP_USERNAME)
ALERT_TO_EMAIL = os.getenv("ALERT_TO_EMAIL")        # where YOU want to receive alerts

# Fail loudly at startup if critical secrets are missing — better to
# crash now than silently trade with bad credentials.
REQUIRED_VARS = {
    "ALPACA_API_KEY": ALPACA_API_KEY,
    "ALPACA_SECRET_KEY": ALPACA_SECRET_KEY,
    "BACKEND_API_TOKEN": BACKEND_API_TOKEN,
}
missing = [k for k, v in REQUIRED_VARS.items() if not v]
if missing:
    raise RuntimeError(
        f"Missing required environment variables in your .env file: {', '.join(missing)}. "
        f"See the .env.example file for the full list and instructions."
    )


# ──────────────────────────────────────────────────────────────────
# 2. LOGGING
# ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("alphabot")


# ──────────────────────────────────────────────────────────────────
# 3. ALPACA TRADING CLIENT
# ──────────────────────────────────────────────────────────────────
trading_client = TradingClient(
    api_key=ALPACA_API_KEY,
    secret_key=ALPACA_SECRET_KEY,
    paper=ALPACA_PAPER,  # True = paper trading, False = REAL MONEY
)

logger.info(
    "Alpaca client initialized in %s mode.",
    "PAPER" if ALPACA_PAPER else "LIVE (REAL MONEY)",
)


# ──────────────────────────────────────────────────────────────────
# 4. EMAIL ALERTS
# ──────────────────────────────────────────────────────────────────
def send_email_alert(subject: str, body: str) -> bool:
    """
    Sends an email alert using smtplib. Returns True on success,
    False on failure (failures are logged but never crash a trade —
    you don't want a bad email server to block real order execution).
    """
    if not SMTP_USERNAME or not SMTP_PASSWORD or not ALERT_TO_EMAIL:
        logger.warning("Email alerts not configured — skipping alert: %s", subject)
        return False

    msg = MIMEMultipart()
    msg["From"] = ALERT_FROM_EMAIL
    msg["To"] = ALERT_TO_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()  # encrypt the connection
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(ALERT_FROM_EMAIL, ALERT_TO_EMAIL, msg.as_string())
        logger.info("Email alert sent: %s", subject)
        return True
    except Exception as e:
        logger.error("Failed to send email alert: %s", e)
        return False


# ──────────────────────────────────────────────────────────────────
# 5. AUTH DEPENDENCY — protects every trading endpoint
# ──────────────────────────────────────────────────────────────────
def verify_token(x_api_token: Optional[str] = Header(None)):
    """
    Every request to /buy, /sell, /account, etc. must include this header:
        X-API-Token: <your BACKEND_API_TOKEN from .env>
    This is what stops a stranger from hitting your backend and trading
    with your money even if they find the URL.
    """
    if not x_api_token or x_api_token != BACKEND_API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing API token")
    return True


# ──────────────────────────────────────────────────────────────────
# 6. REQUEST / RESPONSE MODELS
# ──────────────────────────────────────────────────────────────────
class OrderRequest(BaseModel):
    symbol: str = Field(..., description="Ticker symbol, e.g. 'AAPL'")
    qty: Optional[float] = Field(None, description="Number of shares. Provide qty OR notional, not both.")
    notional: Optional[float] = Field(None, description="Dollar amount to buy/sell instead of share count.")
    order_type: Literal["market", "limit"] = "market"
    limit_price: Optional[float] = Field(None, description="Required if order_type is 'limit'.")
    time_in_force: Literal["day", "gtc"] = "day"
    bot_name: Optional[str] = Field(None, description="Optional label for which bot triggered this trade.")


class OrderResponse(BaseModel):
    success: bool
    order_id: Optional[str] = None
    symbol: str
    side: str
    status: Optional[str] = None
    message: str


# ──────────────────────────────────────────────────────────────────
# 7. FASTAPI APP
# ──────────────────────────────────────────────────────────────────
app = FastAPI(title="AlphaBot Trading Backend", version="1.0.0")

# CORS: lock this down to your actual frontend domain once deployed.
# "*" is fine for local testing only.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ⚠️ replace with ["https://your-frontend-domain.com"] in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {
        "service": "AlphaBot Trading Backend",
        "mode": "PAPER" if ALPACA_PAPER else "LIVE",
        "status": "running",
    }


@app.get("/account", dependencies=[Depends(verify_token)])
def get_account():
    """Returns your current Alpaca account info — balance, buying power, etc."""
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
