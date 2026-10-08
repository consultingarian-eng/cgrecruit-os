"""Module-level singletons shared across dialer sub-modules.

`_scheduler` and `_db` are mutated in-place by `start_scheduler()`. Every
sub-module imports this module (not the names) and reads `state._scheduler`
so updates are visible everywhere — direct `from .state import _scheduler`
would create a local binding frozen at import time.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from motor.motor_asyncio import AsyncIOMotorDatabase

_scheduler: Optional[AsyncIOScheduler] = None
_db: Optional[AsyncIOMotorDatabase] = None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def settings_for(user_id: str, pipeline_id: Optional[str]) -> Dict[str, Any]:
    """Resolve per-pipeline overrides; falls back to global. Lazy import to
    avoid a circular at module load time."""
    from deps import resolve_settings
    return await resolve_settings(user_id, pipeline_id) or {}
