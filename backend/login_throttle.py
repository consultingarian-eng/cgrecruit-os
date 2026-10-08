"""Failed-login throttle for /api/auth/login.

The login had no limit at all, so a password could be guessed at full speed.
This counts FAILURES only (a correct password never adds to a count) per
account and, more loosely, per client IP, over a sliding 15-minute window -
the same rule CG1 uses. While an account is over the limit even the right
password is refused, otherwise the limit would only slow a guesser down.

In memory on purpose: CGRecruit runs one uvicorn worker, and a deploy simply
resets the counts. The IP is the rightmost X-Forwarded-For hop (the one our
edge saw), never request.client, which is Railway's proxy for everyone - an
IP limit on that would lock the whole office out together.
"""
import time
from collections import deque
from typing import Deque, Dict, Iterable, Optional

WINDOW_SECONDS = 15 * 60
ACCOUNT_LIMIT = 10
IP_LIMIT = 100

_fails: Dict[str, Deque[float]] = {}


def keys_for(email: str, forwarded_for: Optional[str], client_host: Optional[str]) -> Dict[str, str]:
    ip = (forwarded_for or "").split(",")[-1].strip() or (client_host or "unknown")
    return {"account": f"acct:{(email or '').strip().lower()}", "ip": f"ip:{ip}"}


def _recent(key: str, now: float) -> Deque[float]:
    q = _fails.setdefault(key, deque())
    while q and now - q[0] > WINDOW_SECONDS:
        q.popleft()
    return q


def is_blocked(keys: Dict[str, str], now: Optional[float] = None) -> bool:
    now = time.time() if now is None else now
    return (len(_recent(keys["account"], now)) >= ACCOUNT_LIMIT
            or len(_recent(keys["ip"], now)) >= IP_LIMIT)


def record_failure(keys: Dict[str, str], now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    for key in keys.values():
        _recent(key, now).append(now)


def clear(keys: Iterable[str]) -> None:
    for key in keys:
        _fails.pop(key, None)
