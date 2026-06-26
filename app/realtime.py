"""
realtime.py — a tiny, dependency-free publish/subscribe event bus that bridges
the background worker threads (APScheduler jobs in ``app/scheduler.py``) to the
asyncio world of FastAPI's SSE responses.

Why this exists
----------------
The market-data poller and the bot evaluator run on background *threads*. The
SSE endpoint that streams updates to the browser lives on the asyncio event
loop. You cannot safely push items into an ``asyncio.Queue`` from another
thread, so every publish is marshalled onto the captured event loop with
``loop.call_soon_threadsafe``.

Each connected browser gets its own bounded queue. Events optionally carry a
``user_id`` so portfolio/trade events are only delivered to their owner, while
market-quote events are broadcast to everyone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Optional

logger = logging.getLogger("alphabot.realtime")

_MAX_QUEUE = 1000


class _Subscriber:
    __slots__ = ("queue", "user_id")

    def __init__(self, queue: "asyncio.Queue", user_id: Optional[int]):
        self.queue = queue
        self.user_id = user_id


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[_Subscriber] = []
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ── lifecycle ────────────────────────────────────────────────
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Capture the main event loop at startup so threads can publish."""
        self._loop = loop

    # ── subscription (asyncio side) ──────────────────────────────
    def subscribe(self, user_id: Optional[int] = None) -> "asyncio.Queue":
        q: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUE)
        sub = _Subscriber(q, user_id)
        with self._lock:
            self._subscribers.append(sub)
        logger.info("[SSE] subscriber added (user=%s, total=%d)", user_id, len(self._subscribers))
        return q

    def unsubscribe(self, q: "asyncio.Queue") -> None:
        with self._lock:
            self._subscribers = [s for s in self._subscribers if s.queue is not q]
        logger.info("[SSE] subscriber removed (total=%d)", len(self._subscribers))

    # ── publishing (works from any thread) ───────────────────────
    def publish(self, event_type: str, data: dict, user_id: Optional[int] = None) -> None:
        """
        Deliver an event to matching subscribers. ``user_id=None`` means a
        global broadcast (e.g. market quotes); a concrete id restricts delivery
        to that user's connections (e.g. portfolio/trade updates).
        """
        payload = {"type": event_type, "data": data}
        # Delivery rules:
        #   * broadcast event (user_id is None)  -> every subscriber
        #   * scoped event (user_id is X)        -> only subscribers bound to X
        # An anonymous (None) subscriber therefore only ever sees broadcasts,
        # never another user's trade/portfolio events.
        with self._lock:
            targets = [
                s for s in self._subscribers
                if user_id is None or s.user_id == user_id
            ]
        if not targets:
            return

        loop = self._loop
        for sub in targets:
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(self._safe_put, sub.queue, payload)
            else:
                # Same-thread fallback (tests / no running loop).
                self._safe_put(sub.queue, payload)

    @staticmethod
    def _safe_put(queue: "asyncio.Queue", payload: dict) -> None:
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            # Drop the oldest item to make room — live data favours freshness.
            try:
                queue.get_nowait()
                queue.put_nowait(payload)
            except Exception:
                pass


# Module-level singleton shared by the scheduler, the engine and the SSE route.
bus = EventBus()


def sse_format(payload: dict) -> str:
    """Render a payload as a Server-Sent Events frame."""
    return f"event: {payload.get('type', 'message')}\ndata: {json.dumps(payload.get('data', {}))}\n\n"
