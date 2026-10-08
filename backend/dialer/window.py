"""Call-window math: keep dials inside the recruiter's working hours, plus the
canonical next_call_at (de)serializers. Those live here because this module
imports nothing from `dialer`, so every writer can reach them without the
queue -> place_call -> queue import cycle."""
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import pytz
from app_tz import app_zone, default_tz_name  # APP_TIMEZONE (noqa: F401)


def parse_hhmm(s: str) -> Tuple[int, int]:
    try:
        parts = (s or "09:00").split(":")
        return int(parts[0]), int(parts[1] if len(parts) > 1 else "0")
    except Exception:
        return 9, 0


def next_in_window(scheduled_at: datetime, tz_name: str, start_hhmm: str, end_hhmm: str, allowed_days: List[int]) -> datetime:
    """Return scheduled_at if within window/days; else move forward to next valid moment."""
    try:
        tz = pytz.timezone(tz_name or default_tz_name())
    except Exception:
        tz = pytz.UTC
    sh, sm = parse_hhmm(start_hhmm)
    eh, em = parse_hhmm(end_hhmm)
    local = scheduled_at.astimezone(tz)
    for _ in range(8):  # max 8 days lookahead
        weekday = local.weekday()
        if weekday in (allowed_days or [0, 1, 2, 3, 4]):
            window_start = local.replace(hour=sh, minute=sm, second=0, microsecond=0)
            window_end = local.replace(hour=eh, minute=em, second=0, microsecond=0)
            if local < window_start:
                local = window_start
                break
            elif local <= window_end:
                break  # already in window
            else:
                # past window — push to next day's window start
                local = (local + timedelta(days=1)).replace(hour=sh, minute=sm, second=0, microsecond=0)
        else:
            # not allowed day — push to next day at window start
            local = (local + timedelta(days=1)).replace(hour=sh, minute=sm, second=0, microsecond=0)
    return local.astimezone(timezone.utc)


def store_next_call_at(run_at: datetime, tz_name: str) -> str:
    """Canonical next_call_at: an ET-naive wall-clock ISO string
    ("2026-08-16T14:30:00") in the pipeline's timezone.

    Every writer must go through here. The Call Queue sorts on this field as a
    plain string and the slot-packing query in `queue.schedule_call_with_window`
    range-matches it against ET-naive bounds, so a stray offset-aware value
    renders 4-5 hours out, escapes the slot count and sorts to the bottom."""
    try:
        tz = pytz.timezone(tz_name or default_tz_name())
    except Exception:
        tz = pytz.UTC
    if run_at.tzinfo is None:
        # Callers work in UTC (state.now_utc, next_in_window); say so explicitly
        # rather than letting astimezone() assume the server's local zone.
        run_at = pytz.UTC.localize(run_at)
    return run_at.astimezone(tz).strftime("%Y-%m-%dT%H:%M:%S")


def parse_next_call_at(value: str, tz_name: str) -> Optional[datetime]:
    """Parse next_call_at whether it is ET-naive or offset-aware, returning an
    aware UTC datetime. Returns None on unparseable input — never raises.

    Rows written before every writer used `store_next_call_at` still carry the
    offset-aware form, so readers must tolerate both indefinitely."""
    if not value:
        return None
    try:
        tz = pytz.timezone(tz_name or default_tz_name())
    except Exception:
        tz = pytz.UTC
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = tz.localize(dt)
    return dt.astimezone(timezone.utc)
