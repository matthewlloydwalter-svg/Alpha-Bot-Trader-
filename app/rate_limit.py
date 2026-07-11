"""
rate_limit.py — simple in-memory IP rate limiter for auth and expensive routes.
"""

from __future__ import annotations

import time
import threading
from collections import defaultdict, deque
from fastapi import HTTPException, Request


class RateLimiter:
    def __init__(self):
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int, window_sec: float) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and (now - q[0]) > window_sec:
                q.popleft()
            if len(q) >= limit:
                raise HTTPException(
                    status_code=429,
                    detail="Too many requests. Please wait a moment and try again.",
                )
            q.append(now)


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    if request.client:
        return request.client.host or "unknown"
    return "unknown"


def limit_auth(request: Request, limit: int = 10, window_sec: float = 60.0) -> None:
    """Rate-limit signup/login by IP."""
    limiter.check(f"auth:{client_ip(request)}", limit, window_sec)


def limit_verification(request: Request, limit: int = 5, window_sec: float = 300.0) -> None:
    limiter.check(f"verify:{client_ip(request)}", limit, window_sec)
