"""
pattern_analysis.py — the shared "market brain".

This module turns a raw stream of OHLCV candles into a fully structured,
decision-ready analysis. The SAME output object is consumed by two places:

  1. The Market Dashboard API (so the UI can draw indicators/overlays).
  2. The autonomous bot engine (so bots evaluate the entire chart structure
     instead of a single spot price).

Everything here is pure Python (no numpy/pandas) so it has zero extra
dependencies, is trivially unit-testable, and is safe to run inside a
request handler or a background thread.

Glossary of the structured output (see ``analyze_candles``):
  - indicators: latest values of SMA/EMA/RSI/MACD/Bollinger/ATR
  - series:     full arrays aligned to the candle index (for charting)
  - levels:     detected support / resistance price levels
  - patterns:   list of named structural patterns detected on the chart
  - signal:     a single normalized recommendation the bot acts on
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

logger = logging.getLogger("alphabot.patterns")


# ────────────────────────────────────────────────────────────────────
# Candle container
# ────────────────────────────────────────────────────────────────────
@dataclass
class Candle:
    """A single OHLCV bar. ``ts`` is a unix epoch (seconds)."""
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def to_dict(self) -> dict:
        return {
            "time": int(self.ts),
            "open": round(self.open, 6),
            "high": round(self.high, 6),
            "low": round(self.low, 6),
            "close": round(self.close, 6),
            "volume": round(self.volume, 4),
        }


# ────────────────────────────────────────────────────────────────────
# Low-level indicator math (pure python, list-in / list-out)
# ────────────────────────────────────────────────────────────────────
def sma(values: list[float], period: int) -> list[Optional[float]]:
    """Simple moving average. Output aligned to input (None until warmed up)."""
    out: list[Optional[float]] = []
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= period:
            running -= values[i - period]
        out.append(running / period if i >= period - 1 else None)
    return out


def ema(values: list[float], period: int) -> list[Optional[float]]:
    """Exponential moving average, seeded with the first SMA."""
    out: list[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = (values[i] - prev) * k + prev
        out[i] = prev
    return out


def rsi(values: list[float], period: int = 14) -> list[Optional[float]]:
    """Wilder's RSI."""
    out: list[Optional[float]] = [None] * len(values)
    if len(values) <= period:
        return out
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
    return out


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """Returns (macd_line, signal_line, histogram), each aligned to input."""
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    macd_line: list[Optional[float]] = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(ema_fast, ema_slow)
    ]
    # Signal line = EMA of the (defined portion of the) macd line.
    defined = [(i, v) for i, v in enumerate(macd_line) if v is not None]
    signal_line: list[Optional[float]] = [None] * len(values)
    hist: list[Optional[float]] = [None] * len(values)
    if len(defined) >= signal:
        vals_only = [v for _, v in defined]
        sig_vals = ema(vals_only, signal)
        for (orig_idx, _), sv in zip(defined, sig_vals):
            signal_line[orig_idx] = sv
        for i in range(len(values)):
            if macd_line[i] is not None and signal_line[i] is not None:
                hist[i] = macd_line[i] - signal_line[i]
    return macd_line, signal_line, hist


def bollinger(values: list[float], period: int = 20, mult: float = 2.0):
    """Returns (middle, upper, lower) Bollinger Bands aligned to input."""
    mid = sma(values, period)
    upper: list[Optional[float]] = [None] * len(values)
    lower: list[Optional[float]] = [None] * len(values)
    for i in range(len(values)):
        if i >= period - 1:
            window = values[i - period + 1 : i + 1]
            m = mid[i]
            var = sum((x - m) ** 2 for x in window) / period
            sd = var ** 0.5
            upper[i] = m + mult * sd
            lower[i] = m - mult * sd
    return mid, upper, lower


def atr(candles: list[Candle], period: int = 14) -> list[Optional[float]]:
    """Average True Range — the bedrock of volatility-adaptive stops."""
    out: list[Optional[float]] = [None] * len(candles)
    if len(candles) <= period:
        return out
    trs: list[float] = [0.0]
    for i in range(1, len(candles)):
        c = candles[i]
        prev_close = candles[i - 1].close
        tr = max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))
        trs.append(tr)
    seed = sum(trs[1 : period + 1]) / period
    out[period] = seed
    prev = seed
    for i in range(period + 1, len(candles)):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


# ────────────────────────────────────────────────────────────────────
# Structural detection: support / resistance via swing pivots
# ────────────────────────────────────────────────────────────────────
def find_pivots(candles: list[Candle], left: int = 3, right: int = 3):
    """
    Detect swing highs and swing lows. A swing high is a candle whose high
    is strictly greater than the ``left`` candles before and ``right`` after.
    Returns (highs, lows) as lists of (index, price).
    """
    highs, lows = [], []
    n = len(candles)
    for i in range(left, n - right):
        hi = candles[i].high
        lo = candles[i].low
        if all(hi > candles[j].high for j in range(i - left, i)) and \
           all(hi >= candles[j].high for j in range(i + 1, i + right + 1)):
            highs.append((i, hi))
        if all(lo < candles[j].low for j in range(i - left, i)) and \
           all(lo <= candles[j].low for j in range(i + 1, i + right + 1)):
            lows.append((i, lo))
    return highs, lows


def cluster_levels(prices: list[float], tolerance_pct: float = 0.6) -> list[float]:
    """
    Group nearby price points into single horizontal levels so we don't
    report ten near-identical support lines. ``tolerance_pct`` controls
    how close two pivots must be (as a % of price) to merge.
    """
    if not prices:
        return []
    ordered = sorted(prices)
    clusters: list[list[float]] = [[ordered[0]]]
    for p in ordered[1:]:
        anchor = clusters[-1][0]
        if anchor > 0 and abs(p - anchor) / anchor * 100 <= tolerance_pct:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    return [round(sum(c) / len(c), 6) for c in clusters]


# ────────────────────────────────────────────────────────────────────
# Result containers
# ────────────────────────────────────────────────────────────────────
@dataclass
class Signal:
    action: str = "HOLD"          # BUY / SELL / HOLD
    strength: float = 0.0          # 0..1 conviction score
    confidence: str = "low"        # low / medium / high
    reasons: list[str] = field(default_factory=list)
    headline: str = "Scanning market structure"
    bias: str = "neutral"          # bullish / bearish / neutral

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Analysis:
    symbol: str
    exchange: str
    timeframe: str
    last_price: float
    indicators: dict
    levels: dict
    patterns: list[dict]
    signal: Signal
    series: dict
    candles: list[dict]
    generated_at: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["signal"] = self.signal.to_dict()
        return d


# ────────────────────────────────────────────────────────────────────
# The main entry point
# ────────────────────────────────────────────────────────────────────
def analyze_candles(symbol: str, exchange: str, timeframe: str,
                    candles: list[Candle]) -> Analysis:
    """
    Run the full indicator + pattern + signal stack over a list of candles.

    Designed to never raise on thin data — if there aren't enough bars to
    compute an indicator it simply reports ``None`` for it and produces a
    conservative HOLD signal.
    """
    from datetime import datetime, timezone

    closes = [c.close for c in candles]
    n = len(closes)
    last_price = closes[-1] if closes else 0.0

    logger.info("[ANALYZE] %s:%s tf=%s bars=%d last=%.6f",
                exchange, symbol, timeframe, n, last_price)

    sma20 = sma(closes, 20)
    sma50 = sma(closes, 50)
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    rsi14 = rsi(closes, 14)
    macd_line, signal_line, hist = macd(closes)
    bb_mid, bb_up, bb_low = bollinger(closes, 20, 2.0)
    atr14 = atr(candles, 14)

    def last_defined(arr):
        for v in reversed(arr):
            if v is not None:
                return v
        return None

    cur_rsi = last_defined(rsi14)
    cur_sma20 = last_defined(sma20)
    cur_sma50 = last_defined(sma50)
    cur_atr = last_defined(atr14)
    cur_bb_low = last_defined(bb_low)
    cur_bb_up = last_defined(bb_up)
    cur_macd = last_defined(macd_line)
    cur_signal = last_defined(signal_line)
    cur_hist = last_defined(hist)

    # ── Structural levels ──
    highs, lows = find_pivots(candles, left=3, right=3)
    resistances = cluster_levels([p for _, p in highs])
    supports = cluster_levels([p for _, p in lows])
    nearest_support = max([s for s in supports if s <= last_price], default=None)
    nearest_resistance = min([r for r in resistances if r >= last_price], default=None)

    # ── Pattern detection ──
    patterns: list[dict] = []

    # Golden / death cross (SMA20 vs SMA50)
    cross = _detect_ma_cross(sma20, sma50)
    if cross:
        patterns.append(cross)

    # MACD bullish/bearish crossover
    macd_cross = _detect_macd_cross(macd_line, signal_line)
    if macd_cross:
        patterns.append(macd_cross)

    # RSI oversold/overbought
    if cur_rsi is not None:
        if cur_rsi <= 30:
            patterns.append({"name": "RSI Oversold", "bias": "bullish",
                             "detail": f"RSI at {cur_rsi:.1f} (<=30) — exhaustion of sellers."})
        elif cur_rsi >= 70:
            patterns.append({"name": "RSI Overbought", "bias": "bearish",
                             "detail": f"RSI at {cur_rsi:.1f} (>=70) — buyers overextended."})

    # Bollinger band dip / pop
    if cur_bb_low is not None and last_price <= cur_bb_low:
        patterns.append({"name": "Lower Band Dip", "bias": "bullish",
                         "detail": "Price pierced lower Bollinger Band — statistically stretched dip."})
    if cur_bb_up is not None and last_price >= cur_bb_up:
        patterns.append({"name": "Upper Band Pop", "bias": "bearish",
                         "detail": "Price pierced upper Bollinger Band — statistically stretched peak."})

    # Support reclaim / resistance rejection
    if nearest_support is not None and last_price > 0:
        dist = (last_price - nearest_support) / last_price * 100
        if 0 <= dist <= 1.5:
            patterns.append({"name": "Support Test", "bias": "bullish",
                             "detail": f"Price {dist:.2f}% above support {nearest_support:.4f} — dip-buy zone."})
    if nearest_resistance is not None and last_price > 0:
        dist = (nearest_resistance - last_price) / last_price * 100
        if 0 <= dist <= 1.5:
            patterns.append({"name": "Resistance Test", "bias": "bearish",
                             "detail": f"Price {dist:.2f}% below resistance {nearest_resistance:.4f} — peak zone."})

    # Trend reversal: higher-low after a downswing (bullish divergence-ish)
    reversal = _detect_reversal(candles, lows, highs, rsi14)
    if reversal:
        patterns.append(reversal)

    trend = _detect_trend(cur_sma20, cur_sma50, closes)

    # ── Compose the signal ──
    sig = _build_signal(
        last_price=last_price, cur_rsi=cur_rsi, cur_sma20=cur_sma20,
        cur_sma50=cur_sma50, cur_macd=cur_macd, cur_signal=cur_signal,
        cur_hist=cur_hist, nearest_support=nearest_support,
        nearest_resistance=nearest_resistance, patterns=patterns, trend=trend,
    )

    indicators = {
        "rsi": _r(cur_rsi), "sma20": _r(cur_sma20), "sma50": _r(cur_sma50),
        "ema12": _r(last_defined(ema12)), "ema26": _r(last_defined(ema26)),
        "macd": _r(cur_macd), "macd_signal": _r(cur_signal), "macd_hist": _r(cur_hist),
        "bb_upper": _r(cur_bb_up), "bb_lower": _r(cur_bb_low),
        "atr": _r(cur_atr), "trend": trend,
    }
    levels = {
        "support": supports[-5:], "resistance": resistances[:5],
        "nearest_support": _r(nearest_support),
        "nearest_resistance": _r(nearest_resistance),
    }
    series = {
        "sma20": [_r(v) for v in sma20], "sma50": [_r(v) for v in sma50],
        "rsi": [_r(v) for v in rsi14],
        "bb_upper": [_r(v) for v in bb_up], "bb_lower": [_r(v) for v in bb_low],
        "macd": [_r(v) for v in macd_line], "macd_signal": [_r(v) for v in signal_line],
    }

    return Analysis(
        symbol=symbol, exchange=exchange, timeframe=timeframe,
        last_price=round(last_price, 6), indicators=indicators, levels=levels,
        patterns=patterns, signal=sig, series=series,
        candles=[c.to_dict() for c in candles],
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


# ────────────────────────────────────────────────────────────────────
# Internal helpers
# ────────────────────────────────────────────────────────────────────
def _r(v, ndigits: int = 6):
    return round(v, ndigits) if isinstance(v, (int, float)) else None


def _detect_ma_cross(fast_ma, slow_ma):
    """Detect a recent crossover between two MA series (last 3 bars)."""
    pairs = [(i, fast_ma[i], slow_ma[i]) for i in range(len(fast_ma))
             if fast_ma[i] is not None and slow_ma[i] is not None]
    if len(pairs) < 2:
        return None
    for k in range(len(pairs) - 1, max(0, len(pairs) - 4), -1):
        _, f_now, s_now = pairs[k]
        _, f_prev, s_prev = pairs[k - 1]
        if f_prev <= s_prev and f_now > s_now:
            return {"name": "Golden Cross", "bias": "bullish",
                    "detail": "Fast MA crossed above slow MA — momentum turning up."}
        if f_prev >= s_prev and f_now < s_now:
            return {"name": "Death Cross", "bias": "bearish",
                    "detail": "Fast MA crossed below slow MA — momentum turning down."}
    return None


def _detect_macd_cross(macd_line, signal_line):
    pairs = [(i, macd_line[i], signal_line[i]) for i in range(len(macd_line))
             if macd_line[i] is not None and signal_line[i] is not None]
    if len(pairs) < 2:
        return None
    for k in range(len(pairs) - 1, max(0, len(pairs) - 4), -1):
        _, m_now, s_now = pairs[k]
        _, m_prev, s_prev = pairs[k - 1]
        if m_prev <= s_prev and m_now > s_now:
            return {"name": "MACD Bullish Cross", "bias": "bullish",
                    "detail": "MACD crossed above signal line — accelerating upside."}
        if m_prev >= s_prev and m_now < s_now:
            return {"name": "MACD Bearish Cross", "bias": "bearish",
                    "detail": "MACD crossed below signal line — accelerating downside."}
    return None


def _detect_reversal(candles, lows, highs, rsi14):
    """
    Bullish reversal: the most recent swing low is HIGHER than the prior swing
    low while RSI made a lower low (classic bullish divergence), OR simply a
    confirmed higher-low structure after a decline.
    """
    if len(lows) < 2:
        return None
    (i_prev, p_prev), (i_last, p_last) = lows[-2], lows[-1]
    if p_last > p_prev:
        rsi_prev = rsi14[i_prev] if i_prev < len(rsi14) else None
        rsi_last = rsi14[i_last] if i_last < len(rsi14) else None
        if rsi_prev is not None and rsi_last is not None and rsi_last > rsi_prev:
            return {"name": "Bullish Divergence", "bias": "bullish",
                    "detail": "Higher swing-low with rising RSI — sellers losing control."}
        return {"name": "Higher Low Structure", "bias": "bullish",
                "detail": "Price printed a higher swing-low — early trend reversal."}
    return None


def _detect_trend(cur_sma20, cur_sma50, closes):
    if cur_sma20 is None or cur_sma50 is None:
        if len(closes) >= 2:
            return "up" if closes[-1] > closes[0] else "down"
        return "neutral"
    if cur_sma20 > cur_sma50 * 1.001:
        return "up"
    if cur_sma20 < cur_sma50 * 0.999:
        return "down"
    return "neutral"


def _build_signal(last_price, cur_rsi, cur_sma20, cur_sma50, cur_macd,
                  cur_signal, cur_hist, nearest_support, nearest_resistance,
                  patterns, trend) -> Signal:
    """
    Aggregate every detected pattern into one normalized, weighted signal.
    This is the contract the bot engine acts on: ``action`` + ``strength``.
    """
    bull = 0.0
    bear = 0.0
    reasons: list[str] = []

    weights = {
        "Bullish Divergence": 0.30, "Higher Low Structure": 0.18,
        "Golden Cross": 0.22, "Death Cross": 0.22,
        "MACD Bullish Cross": 0.18, "MACD Bearish Cross": 0.18,
        "RSI Oversold": 0.22, "RSI Overbought": 0.22,
        "Lower Band Dip": 0.16, "Upper Band Pop": 0.16,
        "Support Test": 0.20, "Resistance Test": 0.20,
    }
    for p in patterns:
        w = weights.get(p["name"], 0.1)
        if p["bias"] == "bullish":
            bull += w
            reasons.append(f"+ {p['name']}: {p['detail']}")
        elif p["bias"] == "bearish":
            bear += w
            reasons.append(f"- {p['name']}: {p['detail']}")

    # Trend acts as a light tie-breaker, not a dominator.
    if trend == "up":
        bull += 0.05
    elif trend == "down":
        bear += 0.05

    net = bull - bear
    strength = min(abs(net), 1.0)

    if net >= 0.30:
        action, bias = "BUY", "bullish"
        headline = "Confirmed dip / reversal — deploy capital"
    elif net <= -0.30:
        action, bias = "SELL", "bearish"
        headline = "Peak / breakdown risk — protect capital"
    else:
        action, bias = "HOLD", ("bullish" if net > 0 else "bearish" if net < 0 else "neutral")
        headline = "No confirmed edge — scanning for dip"

    confidence = "high" if strength >= 0.5 else "medium" if strength >= 0.28 else "low"
    if not reasons:
        reasons.append("No structural pattern triggered on the current chart.")

    return Signal(action=action, strength=round(strength, 3), confidence=confidence,
                  reasons=reasons, headline=headline, bias=bias)
