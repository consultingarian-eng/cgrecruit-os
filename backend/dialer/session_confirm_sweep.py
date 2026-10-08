"""Ring the bell when a session is about to run on zero confirmations.

The night before today's empty 9:15, the confirm chaser asked all six booked
candidates "are you still good for tomorrow?" — and none replied Y. The
system had the clearest possible signal that the room would be empty, and it
told nobody: the interviewer found out by sitting there.

Every 20 minutes this looks at sessions starting 30–120 minutes out. Any
slot with two or more candidates and NOT ONE SMS confirmation rings
`appointment.session_unconfirmed` — a genuinely human decision: sit in the
room anyway, ring round the list, or collapse the session. One alert per
pipeline+slot, deduped in `session_alerts`.
"""
import logging
from datetime import timedelta

from . import state
from .state import now_utc
from app_tz import app_zone, default_tz_name  # APP_TIMEZONE (noqa: F401)

logger = logging.getLogger("auto_dialer")


async def session_confirm_sweep() -> dict:
    if state._db is None:
        return {"sessions": 0, "alerted": 0}
    db = state._db
    now = now_utc()
    lo = (now + timedelta(minutes=30)).isoformat()
    hi = (now + timedelta(minutes=120)).isoformat()
    rows = await db.candidates.find(
        {"appointment_at": {"$gte": lo, "$lte": hi},
         "stage": "APPOINTMENT", "archived_at": None},
        {"_id": 0, "id": 1, "user_id": 1, "pipeline_id": 1,
         "appointment_at": 1, "appointment_sms_confirmed": 1,
         "appointment_cancelled_at": 1},
    ).to_list(500)
    sessions: dict = {}
    for c in rows:
        if c.get("appointment_cancelled_at"):
            continue
        key = (c.get("pipeline_id"), c.get("appointment_at"))
        sessions.setdefault(key, []).append(c)
    alerted = 0
    for (pipe_id, slot_iso), cands in sessions.items():
        if len(cands) < 2:
            continue  # a 1-person slot going quiet is normal churn, not a session risk
        confirmed = sum(1 for c in cands if c.get("appointment_sms_confirmed"))
        if confirmed > 0:
            continue
        dupe = await db.session_alerts.find_one({"pipeline_id": pipe_id, "slot_iso": slot_iso})
        if dupe:
            continue
        try:
            pipe = await db.pipelines.find_one({"id": pipe_id}, {"_id": 0, "name": 1}) or {}
            from models import parse_appointment_at
            from zoneinfo import ZoneInfo
            dt = parse_appointment_at(slot_iso)
            local = dt.astimezone(app_zone()).strftime("%-I:%M %p") if dt else slot_iso
            from notifications_service import create_notification
            await create_notification(
                cands[0]["user_id"], "appointment.session_unconfirmed",
                f"🪑 {local} {pipe.get('name', '')}: {len(cands)} booked, NONE confirmed",
                body=("Every candidate on this session ignored the confirmation text. "
                      "Recent sessions like this ran empty — decide whether it goes "
                      "ahead, gets a ring-round, or collapses."),
                link="/calendar",
                pipeline_id=pipe_id,
            )
            await db.session_alerts.insert_one(
                {"pipeline_id": pipe_id, "slot_iso": slot_iso, "at": now.isoformat()})
            alerted += 1
        except Exception as e:
            logger.warning(f"session-confirm alert failed for {pipe_id}/{slot_iso}: {e}")
    if alerted:
        logger.info(f"session-confirm sweep: {alerted} at-risk session(s) flagged")
    return {"sessions": len(sessions), "alerted": alerted}


def schedule_session_confirm_sweep() -> None:
    """Every 20 minutes — fine enough that a 30–120min lookahead window never
    lets a session slip through between runs."""
    if state._scheduler is None:
        return
    try:
        state._scheduler.add_job(
            session_confirm_sweep,
            "interval",
            minutes=20,
            id="session-confirm-sweep",
            replace_existing=True,
            misfire_grace_time=1200,
            max_instances=1,
            next_run_time=now_utc() + timedelta(minutes=2),
        )
        logger.info("session-confirm sweep scheduled (every 20 min)")
    except Exception as e:
        logger.warning(f"session-confirm sweep schedule failed: {e}")
