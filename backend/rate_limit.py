"""Sliding-window limits for public endpoints that cost money or reach people.

/public/apply files a candidate and then texts or calls the phone number it was
given, and the retry chat sends every turn to the LLM. Neither has a login, so
without a limit a script can run up Twilio and Anthropic bills or use the
company's number to pester someone.

In memory on purpose, like login_throttle: CGRecruit runs one uvicorn worker
and a deploy resets the counts. The client IP is the rightmost
X-Forwarded-For hop (the one our edge saw), never request.client, which is
Railway's proxy for everyone.
"""
import time
from collections import deque
from typing import Deque, Dict, Optional

_hits: Dict[str, Deque[float]] = {}
_MAX_KEYS = 50_000


def client_ip(forwarded_for: Optional[str], client_host: Optional[str]) -> str:
    return (forwarded_for or "").split(",")[-1].strip() or (client_host or "unknown")


def allow(key: str, limit: int, window_seconds: float, now: Optional[float] = None) -> bool:
    """Record one hit for `key` and say whether it is within `limit` hits per
    `window_seconds`. A refused hit is not recorded, so waiting works."""
    now = time.time() if now is None else now
    q = _hits.setdefault(key, deque())
    while q and now - q[0] > window_seconds:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    if len(_hits) > _MAX_KEYS:
        _prune(now, window_seconds)
    return True


def _prune(now: float, window_seconds: float) -> None:
    for k in [k for k, q in _hits.items() if not q or now - q[-1] > window_seconds]:
        _hits.pop(k, None)


def reset() -> None:
    _hits.clear()
