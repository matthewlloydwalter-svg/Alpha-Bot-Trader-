"""
brokers.py — thin wrapper around Alpaca and OKX so the rest of the app
can call place_order(...) without caring which broker is active.

Alpaca uses alpaca-py. OKX uses ccxt, which gives unified access to
dozens of crypto exchanges with one library — useful if you ever want
to add more crypto exchanges later without rewriting this file.
"""

import logging
import time
from datetime import datetime, timedelta, timezone

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
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
        non_marginable_buying_power = getattr(a, "non_marginable_buying_power", None)
        multiplier = getattr(a, "multiplier", None)
        return {
            "cash": float(a.cash),
            "portfolio_value": float(a.portfolio_value),
            "buying_power": float(a.buying_power),
            "equity": float(a.equity),
            "trading_blocked": a.trading_blocked,
            "multiplier": str(multiplier) if multiplier is not None else None,
            "non_marginable_buying_power": float(non_marginable_buying_power) if non_marginable_buying_power is not None else None,
            "multiplier": float(multiplier) if multiplier is not None else None,
        }
    except AlpacaAPIError as e:
        raise BrokerError(f"Alpaca error: {e}")


def alpaca_place_order(client: TradingClient, symbol: str, side: str, qty: float = None,
                        notional: float = None, order_type: str = "market",
                        limit_price: float = None, time_in_force: str = "day") -> dict:
    # No keys → client is None. Raise BrokerError (not AttributeError) so the
    # caller's paper-sim fallback handles it instead of crashing the cycle.
    if client is None:
        raise BrokerError("Alpaca API keys not configured.")
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


def alpaca_liquidate_position(client: TradingClient, symbol: str,
                             cancel_timeout: float = 6.0, poll_interval: float = 0.4) -> dict:
    """
    Cleanly exit an Alpaca position WITHOUT the "insufficient qty" race:

      1. Cancel every OPEN order on this symbol (those orders reserve shares, so
         a naive sell of the held qty fails while they are live).
      2. Wait for the cancellations to actually clear (poll until no open orders
         remain for the symbol, bounded by ``cancel_timeout``).
      3. Close the whole position with a single market order via
         ``close_position`` (liquidates 100% of the real broker quantity, so we
         never over- or under-sell relative to what the account actually holds).

    Returns a normalized order dict. Raises BrokerError when keys are missing so
    the caller can fall back to a simulated fill.
    """
    if client is None:
        raise BrokerError("Alpaca API keys not configured.")
    sym = symbol.upper()
    try:
        # 1) Cancel open orders that are reserving shares for this symbol.
        try:
            open_orders = client.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[sym])
            )
        except AlpacaAPIError as e:
            raise BrokerError(f"Alpaca order lookup failed for {sym}: {e}")
        cancelled = 0
        for o in (open_orders or []):
            try:
                client.cancel_order_by_id(o.id)
                cancelled += 1
            except AlpacaAPIError as e:
                logger.warning("[LIQUIDATE] cancel failed for order %s (%s): %s", getattr(o, "id", "?"), sym, e)

        # 2) Wait for confirmation that the cancellations have cleared.
        if cancelled:
            deadline = time.monotonic() + cancel_timeout
            while time.monotonic() < deadline:
                try:
                    still_open = client.get_orders(
                        filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[sym])
                    )
                except AlpacaAPIError:
                    still_open = []
                if not still_open:
                    break
                time.sleep(poll_interval)

        # 3) Is there anything to close?
        try:
            pos = client.get_open_position(sym)
        except AlpacaAPIError:
            pos = None
        if pos is None:
            return {"order_id": None, "status": "no_position", "symbol": sym,
                    "side": "sell", "qty": 0.0, "cancelled_orders": cancelled}

        # 4) Liquidate the entire real position with a market order.
        order = client.close_position(sym)
        return {
            "order_id": str(getattr(order, "id", "")),
            "status": str(getattr(order, "status", "submitted")),
            "symbol": sym, "side": "sell",
            "qty": float(getattr(order, "qty", 0) or getattr(pos, "qty", 0) or 0),
            "cancelled_orders": cancelled,
        }
    except BrokerError:
        raise
    except AlpacaAPIError as e:
        raise BrokerError(f"Alpaca liquidation failed for {sym}: {e}")


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
    # No keys → exchange is None. Raise BrokerError (not AttributeError) so the
    # caller's paper-sim fallback handles it instead of crashing the cycle.
    if exchange is None:
        raise BrokerError("OKX API keys not configured.")
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


def okx_liquidate_position(exchange: ccxt.okx, symbol: str) -> dict:
    """
    Cancel all open orders for the pair (they reserve the base balance), then
    market-sell the entire free base balance. Mirrors the Alpaca cancel-then-
    close flow so a held crypto position can always be exited cleanly.
    """
    if exchange is None:
        raise BrokerError("OKX API keys not configured.")
    market_symbol = symbol if "/" in symbol else f"{symbol.upper()}/USDT"
    base = market_symbol.split("/")[0]
    try:
        # 1) Cancel open orders for this pair (frees the reserved base balance).
        cancelled = 0
        try:
            for o in (exchange.fetch_open_orders(market_symbol) or []):
                try:
                    exchange.cancel_order(o["id"], market_symbol)
                    cancelled += 1
                except Exception as e:
                    logger.warning("[LIQUIDATE] OKX cancel failed (%s): %s", market_symbol, e)
        except Exception as e:
            logger.warning("[LIQUIDATE] OKX open-order lookup failed (%s): %s", market_symbol, e)

        # 2) Determine the free base balance to sell.
        balance = exchange.fetch_balance()
        free = (balance.get("free", {}) or {}).get(base, 0) or 0
        amount = float(free)
        if amount <= 0:
            return {"order_id": None, "status": "no_position", "symbol": market_symbol,
                    "side": "sell", "qty": 0.0, "cancelled_orders": cancelled}

        # 3) Market-sell the whole balance.
        result = exchange.create_order(market_symbol, "market", "sell", amount)
        return {
            "order_id": str(result.get("id")),
            "status": result.get("status", "submitted"),
            "symbol": market_symbol, "side": "sell",
            "qty": amount, "cancelled_orders": cancelled,
        }
    except BrokerError:
        raise
    except Exception as e:
        raise BrokerError(f"OKX liquidation failed for {market_symbol}: {e}")


# ── HISTORICAL CANDLE DATA ───────────────────────────────────────
# Both feeders return a normalized list of dicts:
#   {"ts": int(epoch_seconds), "open","high","low","close","volume"}
# so the pattern layer never has to know which exchange it came from.

def _clean_key(raw: str) -> str:
    """
    Strip leading/trailing ASCII whitespace PLUS any Unicode control/format
    characters (zero-width spaces, invisible separators, etc.) that str.strip()
    doesn't remove, then collapse any internal whitespace to nothing.
    This catches keys that were copy-pasted from a browser that inserted an
    invisible character.
    """
    import unicodedata
    s = (raw or "").strip()
    # Remove every character whose Unicode category starts with 'C'
    # (control, format, surrogate, private-use, unassigned) and any whitespace.
    cleaned = "".join(
        c for c in s
        if not unicodedata.category(c).startswith("C") and not c.isspace()
    )
    return cleaned


def get_alpaca_bars(symbol: str, timeframe: str, limit: int,
                    api_key: str, secret_key: str,
                    start: datetime | None = None) -> list[dict]:
    """
    Fetch historical stock bars from data.alpaca.markets.

    Uses the same API keys as the trading account — both paper AND live account
    keys authenticate against the same data endpoint (the data API does not have
    a separate paper/live mode).

    Strategy: try without a feed restriction first so SIP subscribers get their
    preferred feed automatically.  If Alpaca rejects that with a 403
    (subscription required) we retry with DataFeed.IEX, which is free for every
    Alpaca account.  A 401 always means the key/secret pair itself is wrong —
    we surface the first 4 chars of the key so the user can quickly cross-check.

    Optional ``start`` (UTC datetime) overrides the default lookback window
    derived from limit × bar duration — used by chart timeframe presets.
    """
    api_key = _clean_key(api_key)
    secret_key = _clean_key(secret_key)
    if not api_key or not secret_key:
        raise BrokerError(
            "Alpaca data requires API keys. Add them under Account → Alpaca API Keys "
            "for the mode you are using (Paper or Live)."
        )

    key_hint = api_key[:4] + "…"
    logger.info("[BARS] Alpaca %s tf=%s limit=%d key=%s", symbol, timeframe, limit, key_hint)

    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed

    # Accept both internal codes (1m) and Alpaca-style aliases (1Min).
    _tf_aliases = {
        "1Min": "1m", "1MIN": "1m", "1min": "1m",
        "5Min": "5m", "15Min": "15m", "30Min": "30m",
        "1Hour": "1h", "1HOUR": "1h", "1hour": "1h",
        "1Day": "1d", "1Week": "1w",
    }
    timeframe = _tf_aliases.get(timeframe, timeframe)

    tf_map = {
        "1m": TimeFrame(1, TimeFrameUnit.Minute),
        "5m": TimeFrame(5, TimeFrameUnit.Minute),
        "15m": TimeFrame(15, TimeFrameUnit.Minute),
        "30m": TimeFrame(30, TimeFrameUnit.Minute),
        "1h": TimeFrame(1, TimeFrameUnit.Hour),
        "1d": TimeFrame(1, TimeFrameUnit.Day),
        # Weekly bars back the dashboard's 1W / 5Y views; without this they
        # silently fell back to daily bars for Alpaca (stock) charts.
        "1w": TimeFrame(1, TimeFrameUnit.Week),
    }
    tf = tf_map.get(timeframe, TimeFrame(1, TimeFrameUnit.Day))

    if start is not None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        else:
            start = start.astimezone(timezone.utc)
    else:
        per_bar = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "1d": 1440, "1w": 10080}.get(timeframe, 1440)
        minutes_back = per_bar * limit * 3 + 1440
        start = datetime.now(timezone.utc) - timedelta(minutes=minutes_back)
    # Debug: log the computed start window so we can trace timeframe bugs
    try:
        logger.info("[BARS] Alpaca request: symbol=%s timeframe=%s limit=%d start=%s",
                    symbol, timeframe, limit, start.isoformat())
    except Exception:
        pass

    client = StockHistoricalDataClient(api_key, secret_key)

    def _fetch(feed=None) -> list[dict]:
        kwargs = dict(symbol_or_symbols=symbol.upper(), timeframe=tf, start=start, limit=limit)
        if feed is not None:
            kwargs["feed"] = feed
        req = StockBarsRequest(**kwargs)
        bars = client.get_stock_bars(req)
        data = bars.data.get(symbol.upper(), []) if hasattr(bars, "data") else []
        return [
            {
                "ts": int(b.timestamp.timestamp()),
                "open": float(b.open), "high": float(b.high),
                "low": float(b.low), "close": float(b.close),
                "volume": float(b.volume or 0),
            }
            for b in data
        ]

    try:
        # First attempt — no explicit feed restriction (works for SIP + IEX).
        rows = _fetch(feed=None)
        logger.info("[BARS] Alpaca %s tf=%s -> %d bars (default feed)", symbol, timeframe, len(rows))
        # Log first/last timestamps returned for easier debugging of stale ranges
        if rows:
            try:
                first_ts = datetime.fromtimestamp(rows[0]["ts"], timezone.utc).isoformat()
                last_ts = datetime.fromtimestamp(rows[-1]["ts"], timezone.utc).isoformat()
                logger.info("[BARS] Alpaca %s tf=%s returned range %s -> %s", symbol, timeframe, first_ts, last_ts)
            except Exception:
                pass
    except AlpacaAPIError as first_err:
        err_str = str(first_err)
        # 403 / subscription error → retry with the free IEX feed.
        if "403" in err_str or "forbidden" in err_str.lower() or "subscription" in err_str.lower():
            logger.info("[BARS] SIP feed rejected (%s), retrying with IEX…", err_str[:60])
            try:
                rows = _fetch(feed=DataFeed.IEX)
                logger.info("[BARS] Alpaca %s tf=%s -> %d bars (IEX feed)", symbol, timeframe, len(rows))
            except AlpacaAPIError as iex_err:
                raise BrokerError(_humanize_alpaca_error(iex_err, key_hint))
            except Exception as iex_err:
                raise BrokerError(_humanize_alpaca_error(iex_err, key_hint))
        else:
            raise BrokerError(_humanize_alpaca_error(first_err, key_hint))
    except Exception as first_err:
        raise BrokerError(_humanize_alpaca_error(first_err, key_hint))

    if not rows:
        raise BrokerError(
            f"Alpaca returned no bars for {symbol.upper()} ({timeframe}). "
            "Outside US market hours there may be no recent IEX data for short timeframes — "
            "try switching to the 1D timeframe or check that the ticker symbol is correct."
        )
    return rows[-limit:]


def _humanize_alpaca_error(e, key_hint: str = "????") -> str:
    """
    Turn raw Alpaca/transport errors into short, actionable UI messages.
    ``key_hint`` is the first 4 chars of the key actually used, printed so the
    user can cross-check exactly which saved key was sent.
    """
    msg = str(e)
    low = msg.lower()
    if "401" in msg or "authorization required" in low or "unauthorized" in low:
        return (
            f"Alpaca rejected the API key starting with '{key_hint}' (401 Unauthorized). "
            "The key or secret is incorrect — double-check that you copied both the Key ID "
            "AND the Secret Key exactly, with no extra spaces. "
            "Paper keys come from https://app.alpaca.markets/paper/dashboard/overview; "
            "live keys come from https://app.alpaca.markets/account/overview."
        )
    if "403" in msg or "forbidden" in low or "subscription" in low:
        return (
            "Alpaca returned 403 (data subscription issue) — the IEX fallback feed was also tried. "
            "Confirm your keys are active and your Alpaca account is not restricted."
        )
    if "<html" in low or "nginx" in low:
        return (
            "Alpaca returned an HTML error page instead of data. "
            "This usually means the API endpoint is temporarily unavailable — please try again."
        )
    if "timeout" in low or "connection" in low:
        return "Could not reach Alpaca's data servers (connection timeout). Check your network and try again."
    return f"Alpaca data error: {msg[:300]}"


def get_okx_candles(symbol: str, timeframe: str, limit: int,
                    api_key: str = None, secret_key: str = None,
                    passphrase: str = None, paper: bool = True,
                    start: datetime | None = None) -> list[dict]:
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
        since = None
        if start is not None:
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            else:
                start = start.astimezone(timezone.utc)
            since = int(start.timestamp() * 1000)
        raw = exchange.fetch_ohlcv(market_symbol, timeframe=tf, since=since, limit=limit)
        rows = [{
            "ts": int(r[0] / 1000), "open": float(r[1]), "high": float(r[2]),
            "low": float(r[3]), "close": float(r[4]), "volume": float(r[5] or 0),
        } for r in raw]
        logger.info("[BARS] OKX %s tf=%s -> %d candles", market_symbol, tf, len(rows))
        # Debug: also log the returned range to detect stale data
        if rows:
            try:
                first_ts = datetime.fromtimestamp(rows[0]["ts"], timezone.utc).isoformat()
                last_ts = datetime.fromtimestamp(rows[-1]["ts"], timezone.utc).isoformat()
                logger.info("[BARS] OKX %s tf=%s returned range %s -> %s", market_symbol, tf, first_ts, last_ts)
            except Exception:
                pass
        return rows[-limit:]
    except Exception as e:
        raise BrokerError(f"OKX candle fetch failed for {market_symbol}: {e}")


def get_candles(broker: str, symbol: str, timeframe: str = "1h", limit: int = 200,
                alpaca_key: str = None, alpaca_secret: str = None,
                okx_key: str = None, okx_secret: str = None, okx_passphrase: str = None,
                paper: bool = True, start: datetime | None = None) -> list[dict]:
    """Unified candle feeder used by both the dashboard API and the bot engine."""
    if broker == "alpaca":
        return get_alpaca_bars(symbol, timeframe, limit, alpaca_key, alpaca_secret, start=start)
    elif broker == "okx":
        return get_okx_candles(symbol, timeframe, limit, okx_key, okx_secret, okx_passphrase, paper,
                               start=start)
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


def liquidate_position(broker: str, symbol: str,
                       alpaca_key: str = None, alpaca_secret: str = None,
                       okx_key: str = None, okx_secret: str = None, okx_passphrase: str = None,
                       paper: bool = True) -> dict:
    """
    Robustly exit a position: cancel the symbol's open orders, wait for the
    cancellations to clear, then liquidate the full real broker quantity. Use
    this instead of a fixed-qty sell whenever closing a position so reserved
    shares / qty drift can never cause "insufficient quantity" errors.
    """
    if broker == "alpaca":
        client = get_alpaca_client(alpaca_key, alpaca_secret, paper)
        return alpaca_liquidate_position(client, symbol)
    elif broker == "okx":
        exchange = get_okx_client(okx_key, okx_secret, okx_passphrase, paper)
        return okx_liquidate_position(exchange, symbol)
    else:
        raise BrokerError(f"Unknown broker: {broker}")


def get_position_snapshot(broker: str, symbol: str,
                           alpaca_key: str = None, alpaca_secret: str = None,
                           okx_key: str = None, okx_secret: str = None, okx_passphrase: str = None,
                           paper: bool = True) -> dict | None:
    """Return live broker position data for a symbol when available."""
    if broker == "alpaca":
        client = get_alpaca_client(alpaca_key, alpaca_secret, paper)
        if client is None:
            raise BrokerError("Alpaca API keys not configured.")
        try:
            pos = client.get_open_position(symbol.upper())
            return {
                "symbol": symbol.upper(),
                "broker": "alpaca",
                "avg_entry_price": float(getattr(pos, "avg_entry_price", 0) or 0),
                "current_price": float(getattr(pos, "current_price", 0) or 0),
                "unrealized_pl": float(getattr(pos, "unrealized_pl", 0) or 0),
                "market_value": float(getattr(pos, "market_value", 0) or 0),
                "qty": float(getattr(pos, "qty", 0) or 0),
            }
        except Exception as e:
            raise BrokerError(f"Alpaca position lookup failed for {symbol.upper()}: {e}")
    elif broker == "okx":
        exchange = get_okx_client(okx_key, okx_secret, okx_passphrase, paper)
        if exchange is None:
            return None
        try:
            market_symbol = symbol if "/" in symbol else f"{symbol.upper()}/USDT"
            ticker = exchange.fetch_ticker(market_symbol)
            return {
                "symbol": market_symbol,
                "broker": "okx",
                "avg_entry_price": None,
                "current_price": float(ticker.get("last", 0) or 0),
                "unrealized_pl": None,
                "market_value": None,
                "qty": None,
            }
        except Exception:
            return None
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
