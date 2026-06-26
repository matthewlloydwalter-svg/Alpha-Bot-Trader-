"""
news_analysis.py — lightweight per-asset news sentiment.

This is an *overlay / sanity check* for the autonomous engine, NOT the primary
signal. The chart-pattern analysis decides WHAT to buy; this module only vetoes
or flags an otherwise-good dip entry when the surrounding news is clearly,
strongly negative. It is intentionally conservative and never raises — any
fetch/parse failure degrades to a neutral verdict so trading is never blocked
by a flaky feed.
"""

from __future__ import annotations

import time
import logging
import threading
import urllib.parse
import xml.etree.ElementTree as ET

import requests

logger = logging.getLogger("alphabot.news")

# Keyword lexicons — deliberately small and finance-flavored.
_BULLISH = [
    "surge", "soar", "rally", "jump", "gain", "beat", "beats", "upgrade", "upgraded",
    "record", "high", "outperform", "bullish", "buy", "growth", "profit", "strong",
    "rebound", "recover", "breakthrough", "approval", "wins", "tops", "raise", "raised",
    "boost", "optimistic", "expand", "partnership", "adoption",
]
_BEARISH = [
    "plunge", "plummet", "crash", "drop", "fall", "falls", "tumble", "slump", "miss",
    "missed", "downgrade", "downgraded", "low", "underperform", "bearish", "sell",
    "loss", "losses", "weak", "lawsuit", "probe", "investigation", "fraud", "hack",
    "hacked", "ban", "banned", "warning", "cut", "cuts", "layoff", "layoffs", "bankruptcy",
    "default", "selloff", "fear", "decline", "scandal", "delist",
]

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 600  # 10 minutes — news doesn't change every cycle
_LOCK = threading.Lock()


def _score_text(text: str) -> int:
    t = text.lower()
    score = 0
    for w in _BULLISH:
        if w in t:
            score += 1
    for w in _BEARISH:
        if w in t:
            score -= 1
    return score


def _fetch_headlines(query: str, limit: int = 12) -> list[str]:
    """Pull recent headlines from Google News RSS for a query. Best-effort."""
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(query)
           + "&hl=en-US&gl=US&ceid=US:en")
    try:
        resp = requests.get(url, timeout=7, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return []
        root = ET.fromstring(resp.content)
        titles = []
        for item in root.findall(".//item")[:limit]:
            title = (item.findtext("title") or "").strip()
            if title:
                titles.append(title)
        return titles
    except Exception as e:
        logger.debug("News fetch failed for %r: %s", query, e)
        return []


def get_asset_sentiment(symbol: str, name: str | None = None, asset_class: str = "Equity") -> dict:
    """
    Returns a sentiment verdict for an asset:
      {
        "label": "positive" | "neutral" | "negative",
        "score": float (-1..1),
        "headline_count": int,
        "veto": bool,          # True only when news is STRONGLY negative
        "headlines": [...],
        "note": str,
      }
    """
    key = symbol.upper()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and (time.time() - hit[0]) < _CACHE_TTL:
            return hit[1]

    qualifier = "crypto" if asset_class.lower().startswith("crypto") else "stock"
    query = f"{name or symbol} {symbol} {qualifier}"
    headlines = _fetch_headlines(query)

    if not headlines:
        verdict = {"label": "neutral", "score": 0.0, "headline_count": 0,
                   "veto": False, "headlines": [],
                   "note": "No recent news found — treating as neutral."}
        with _LOCK:
            _CACHE[key] = (time.time(), verdict)
        return verdict

    raw = sum(_score_text(h) for h in headlines)
    # Normalize to roughly -1..1 by headline count.
    norm = max(-1.0, min(1.0, raw / max(len(headlines), 1)))
    if norm > 0.12:
        label = "positive"
    elif norm < -0.12:
        label = "negative"
    else:
        label = "neutral"

    # Veto only on a STRONG negative consensus — the sanity-check, not the driver.
    veto = norm <= -0.35

    verdict = {
        "label": label, "score": round(norm, 3), "headline_count": len(headlines),
        "veto": veto, "headlines": headlines[:5],
        "note": (f"{len(headlines)} headlines scanned; "
                 f"{'strong negative — veto entry' if veto else 'sentiment within tolerance'}."),
    }
    logger.info("[NEWS] %s sentiment=%s score=%.2f veto=%s", key, label, norm, veto)
    with _LOCK:
        _CACHE[key] = (time.time(), verdict)
    return verdict


def classify_headline_sentiment(title: str) -> str:
    """Classify a single headline as bullish/bearish/neutral (used by the news tab)."""
    s = _score_text(title)
    if s > 0:
        return "bullish"
    if s < 0:
        return "bearish"
    return "neutral"
