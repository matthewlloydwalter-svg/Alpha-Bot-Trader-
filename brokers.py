"""
brokers.py — thin wrapper around Alpaca and OKX so the rest of the app
can call place_order(...) without caring which broker is active.

Alpaca uses alpaca-py. OKX uses ccxt, which gives unified access to
dozens of crypto exchanges with one library — useful if you ever want
to add more crypto exchanges later without rewriting this file.
"""

import logging
from datetime import datetime, timedelta, timezone

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.common.exceptions import APIError as AlpacaAPIError
import ccxt

logger = logging.getLogger("alphabot")


class BrokerError(Exception):
    pass


# Map a human timeframe ("1h", "15m", "1d") to the args each library needs.
_OKX_TIMEFRAMES = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "1w"}


# ── ALPACA ───────────────────────────────────────────────────────
def get_alpaca_client(api_key: str, secret_key: str, paper: bool) -> TradingClient:
    if not api_key or not secret_key:
        return None 
    return TradingClient(api_key=api_key, secret_key=secret_key, paper=paper)


def alpaca_account_info(client: TradingClient) -> dict:
    # If the client is None (no keys), we handle it gracefully
    if client is None:
        return {"error": "API keys not set"}
        
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
        return None
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


# ── HISTORICAL CANDLE DATA ───────────────────────────────────────
# Both feeders return a normalized list of dicts:
#   {"ts": int(epoch_seconds), "open","high","low","close","volume"}
# so the pattern layer never has to know which exchange it came from.

def get_alpaca_bars(symbol: str, timeframe: str, limit: int,
                    api_key: str, secret_key: str) -> list[dict]:
    """
    Fetch historical stock bars via alpaca-py's market-data client.
    Works with paper or live keys (data feed = IEX on free plans).
    """
    if not api_key or not secret_key:
        raise BrokerError("Alpaca data requires API keys. Add them in Account → Alpaca API Keys.")
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        tf_map = {
            "1m": TimeFrame(1, TimeFrameUnit.Minute),
            "5m": TimeFrame(5, TimeFrameUnit.Minute),
            "15m": TimeFrame(15, TimeFrameUnit.Minute),
            "30m": TimeFrame(30, TimeFrameUnit.Minute),
            "1h": TimeFrame(1, TimeFrameUnit.Hour),
            "1d": TimeFrame(1, TimeFrameUnit.Day),
        }
        tf = tf_map.get(timeframe, TimeFrame(1, TimeFrameUnit.Day))

        # Choose a lookback window generous enough to satisfy `limit` bars.
        per_bar = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "1d": 1440}.get(timeframe, 1440)
        minutes_back = per_bar * limit * 3 + 1440
        start = datetime.now(timezone.utc) - timedelta(minutes=minutes_back)

        client = StockHistoricalDataClient(api_key, secret_key)
        req = StockBarsRequest(symbol_or_symbols=symbol.upper(), timeframe=tf, start=start, limit=limit)
        bars = client.get_stock_bars(req)

        rows = []
        data = bars.data.get(symbol.upper(), []) if hasattr(bars, "data") else []
        for b in data:
            rows.append({
                "ts": int(b.timestamp.timestamp()),
                "open": float(b.open), "high": float(b.high),
                "low": float(b.low), "close": float(b.close),
                "volume": float(b.volume or 0),
            })
        logger.info("[BARS] Alpaca %s tf=%s -> %d bars", symbol, timeframe, len(rows))
        return rows[-limit:]
    except BrokerError:
        raise
    except AlpacaAPIError as e:
        raise BrokerError(f"Alpaca data error: {e}")
    except Exception as e:
        raise BrokerError(f"Alpaca data fetch failed: {e}")


def get_okx_candles(symbol: str, timeframe: str, limit: int,
                    api_key: str = None, secret_key: str = None,
                    passphrase: str = None, paper: bool = True) -> list[dict]:
    """
    Fetch OHLCV candles from OKX. Public market data does NOT require keys,
    so the dashboard works even before a user connects their account.
    """
    tf = timeframe if timeframe in _OKX_TIMEFRAMES else "1H".lower()
    market_symbol = symbol if "/" in symbol else f"{symbol.upper()}/USDT"
    try:
        cfg = {"enableRateLimit": True}
        if api_key and secret_key and passphrase:
            cfg.update({"apiKey": api_key, "secret": secret_key, "password": passphrase})
        exchange = ccxt.okx(cfg)
        if paper and api_key:
            exchange.set_sandbox_mode(True)
        raw = exchange.fetch_ohlcv(market_symbol, timeframe=tf, limit=limit)
        rows = [{
            "ts": int(r[0] / 1000), "open": float(r[1]), "high": float(r[2]),
            "low": float(r[3]), "close": float(r[4]), "volume": float(r[5] or 0),
        } for r in raw]
        logger.info("[BARS] OKX %s tf=%s -> %d candles", market_symbol, tf, len(rows))
        return rows[-limit:]
    except Exception as e:
        raise BrokerError(f"OKX candle fetch failed for {market_symbol}: {e}")


def get_candles(broker: str, symbol: str, timeframe: str = "1h", limit: int = 200,
                alpaca_key: str = None, alpaca_secret: str = None,
                okx_key: str = None, okx_secret: str = None, okx_passphrase: str = None,
                paper: bool = True) -> list[dict]:
    """Unified candle feeder used by both the dashboard API and the bot engine."""
    if broker == "alpaca":
        return get_alpaca_bars(symbol, timeframe, limit, alpaca_key, alpaca_secret)
    elif broker == "okx":
        return get_okx_candles(symbol, timeframe, limit, okx_key, okx_secret, okx_passphrase, paper)
    raise BrokerError(f"Unknown broker: {broker}")


def get_spot_price(broker: str, symbol: str,
                   alpaca_key: str = None, alpaca_secret: str = None,
                   okx_key: str = None, okx_secret: str = None, okx_passphrase: str = None,
                   paper: bool = True) -> float:
    """Best-effort latest traded price for a symbol."""
    if broker == "okx":
        market_symbol = symbol if "/" in symbol else f"{symbol.upper()}/USDT"
        try:
            exchange = ccxt.okx({"enableRateLimit": True})
            return float(exchange.fetch_ticker(market_symbol)["last"])
        except Exception as e:
            raise BrokerError(f"OKX price fetch failed: {e}")
    elif broker == "alpaca":
        bars = get_alpaca_bars(symbol, "1m", 1, alpaca_key, alpaca_secret)
        if not bars:
            raise BrokerError("No recent Alpaca price data.")
        return bars[-1]["close"]
    raise BrokerError(f"Unknown broker: {broker}")


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
