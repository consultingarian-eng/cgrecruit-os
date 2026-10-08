"""
Lightweight in-process pub/sub for Server-Sent Events.
Each connected browser tab gets an asyncio.Queue. When a candidate status
changes, call broadcast(user_id) and every tab for that user instantly
receives a push — no polling lag.
"""
import asyncio
from collections import defaultdict

_subs: dict[str, set[asyncio.Queue]] = defaultdict(set)


def subscribe(user_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=20)
    _subs[user_id].add(q)
    return q


def unsubscribe(user_id: str, q: asyncio.Queue) -> None:
    _subs[user_id].discard(q)


async def broadcast(user_id: str, event: str = "refresh") -> None:
    dead: set[asyncio.Queue] = set()
    for q in list(_subs.get(user_id, set())):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            dead.add(q)
    for q in dead:
        _subs[user_id].discard(q)
