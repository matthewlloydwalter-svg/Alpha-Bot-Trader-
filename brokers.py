"""
brokers.py — thin wrapper around Alpaca and OKX so the rest of the app
can call place_order(...) without caring which broker is active.

Alpaca uses alpaca-py. OKX uses ccxt, which gives unified access to
dozens of crypto exchanges with one library — useful if you ever want
to add more crypto exchanges later without rewriting this file.
"""

import logging
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.common.exceptions import APIError as AlpacaAPIError
import ccxt

logger = logging.getLogger("alphabot")


class BrokerError(Exception):
    pass


# ── ALPACA ───────────────────────────────────────────────────────
def get_alpaca_client(api_key: str, secret_key: str, paper: bool) -> TradingClient:
    if not api_key or not secret_key:
        raise BrokerError("Alpaca API key/secret not set for this user.")
    return TradingClient(api_key=api_key, secret_key=secret_key, paper=paper)


def alpaca_account_info(client: TradingClient) -> dict:
    try:
        a = client.get_account()
        return {
            "cash": float(a.cash),
            "portfolio_value": float(a.portfolio_value),
            "buying_power": float(a.buying_power),
            "equity": float(a.equity),
            "trading_blocked": a.trading_blocked,
        }
    except AlpacaAPIError as e:
        raise BrokerError(f"Alpaca error: {e}")


def alpaca_place_order(client: TradingClient, symbol: str, side: str, qty: float = None,
                        notional: float = None, order_type: str = "market",
                        limit_price: float = None, time_in_force: str = "day") -> dict:
    if not qty and not notional:
        raise BrokerError("Provide qty or notional.")

    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
    tif = TimeInForce.DAY if time_in_force == "day" else TimeInForce.GTC

    kwargs = dict(symbol=symbol.upper(), side=order_side, time_in_force=tif)
    if qty:
        kwargs["qty"] = qty
    else:
        kwargs["notional"] = notional

    try:
        if order_type == "market":
            req = MarketOrderRequest(**kwargs)
        else:
            if limit_price is None:
                raise BrokerError("limit_price required for limit orders.")
            req = LimitOrderRequest(**kwargs, limit_price=limit_price)
        result = client.submit_order(order_data=req)
        return {
            "order_id": str(result.id),
            "status": str(result.status),
            "symbol": result.symbol,
            "side": side,
        }
    except AlpacaAPIError as e:
        raise BrokerError(f"Alpaca rejected order: {e}")


# ── OKX (via ccxt) ───────────────────────────────────────────────
def get_okx_client(api_key: str, secret_key: str, passphrase: str, paper: bool) -> ccxt.okx:
    if not api_key or not secret_key or not passphrase:
        raise BrokerError("OKX API key/secret/passphrase not set for this user.")
    exchange = ccxt.okx({
        "apiKey": api_key,
        "secret": secret_key,
        "password": passphrase,
        "enableRateLimit": True,
    })
    if paper:
        # OKX's demo trading flag — requires demo-trading API keys generated
        # from OKX's demo trading section, separate from live keys.
        exchange.set_sandbox_mode(True)
    return exchange


def okx_account_info(exchange: ccxt.okx) -> dict:
    try:
        balance = exchange.fetch_balance()
        total = balance.get("total", {})
        return {"balances": {k: v for k, v in total.items() if v}}
    except Exception as e:
        raise BrokerError(f"OKX error: {e}")


def okx_place_order(exchange: ccxt.okx, symbol: str, side: str, qty: float = None,
                     notional: float = None, order_type: str = "market",
                     limit_price: float = None) -> dict:
    # ccxt expects symbols like "BTC/USDT"
    market_symbol = symbol if "/" in symbol else f"{symbol.upper()}/USDT"

    try:
        amount = qty
        if not amount and notional:
            ticker = exchange.fetch_ticker(market_symbol)
            amount = notional / ticker["last"]

        if order_type == "market":
            result = exchange.create_order(market_symbol, "market", side, amount)
        else:
            if limit_price is None:
                raise BrokerError("limit_price required for limit orders.")
            result = exchange.create_order(market_symbol, "limit", side, amount, limit_price)

        return {
            "order_id": str(result.get("id")),
            "status": result.get("status", "submitted"),
            "symbol": market_symbol,
            "side": side,
        }
    except Exception as e:
        raise BrokerError(f"OKX rejected order: {e}")


# ── UNIFIED INTERFACE ────────────────────────────────────────────
def place_order(broker: str, side: str, symbol: str, qty: float = None, notional: float = None,
                 order_type: str = "market", limit_price: float = None,
                 alpaca_key: str = None, alpaca_secret: str = None,
                 okx_key: str = None, okx_secret: str = None, okx_passphrase: str = None,
                 paper: bool = True) -> dict:
    """
    Single entry point the rest of the app calls. Picks Alpaca or OKX
    based on the `broker` argument, using whichever credentials are
    provided for that broker.
    """
    if broker == "alpaca":
        client = get_alpaca_client(alpaca_key, alpaca_secret, paper)
        return alpaca_place_order(client, symbol, side, qty, notional, order_type, limit_price)
    elif broker == "okx":
        exchange = get_okx_client(okx_key, okx_secret, okx_passphrase, paper)
        return okx_place_order(exchange, symbol, side, qty, notional, order_type, limit_price)
    else:
        raise BrokerError(f"Unknown broker: {broker}")


def get_account_info(broker: str, alpaca_key: str = None, alpaca_secret: str = None,
                      okx_key: str = None, okx_secret: str = None, okx_passphrase: str = None,
                      paper: bool = True) -> dict:
    if broker == "alpaca":
        client = get_alpaca_client(alpaca_key, alpaca_secret, paper)
        return alpaca_account_info(client)
    elif broker == "okx":
        exchange = get_okx_client(okx_key, okx_secret, okx_passphrase, paper)
        return okx_account_info(exchange)
    else:
        raise BrokerError(f"Unknown broker: {broker}")
