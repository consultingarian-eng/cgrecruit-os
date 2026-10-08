"""Booked-but-unscreened candidates get the gates asked by text, automatically.

The outcome hook flags bookings that carry no screening conversation at all
(screening_material="none") and rings the bell — but a bell is a request for
attention, not a mechanism. This sweep is the mechanism: every hour, anyone
flagged, still booked in the future, and not yet texted gets the gate-check
opener. Their reply routes into the SMS screener's gate-check mode (questions
only — the booking is never touched). Idempotent per candidate via
gate_check_sent_at, held to daytime hours: a compliance question at 11pm
reads as a system, not a person.
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import state
from .state import now_utc
from app_tz import app_zone, default_tz_name  # APP_TIMEZONE (noqa: F401)

logger = logging.getLogger("auto_dialer")

ET = app_zone()


def _in_texting_hours(now=None) -> bool:
    local = (now or now_utc()).astimezone(ET)
    return 9 <= local.hour < 19


async def gate_check_sweep() -> dict:
    if state._db is None:
        return {"checked": 0, "sent": 0}
    if not _in_texting_hours():
        return {"checked": 0, "sent": 0, "skipped": "outside texting hours"}
    db = state._db

    # Booked candidates with NO verdict and NO material stamp yet: the outcome
    # hook normally runs at booking time, but it can die mid-flight — one
    # candidate's conclude was killed by overnight LLM flakiness, leaving them
    # booked with a full screening and an empty card, and nothing ever came
    # back for them. ensure_screening_outcome is idempotent:
    # it summarises when material exists, stamps screening_material="none" and
    # rings the bell when it doesn't (which the pass below then texts), and
    # defers when a transcript is still syncing.
    from screening_outcome import GATE_CHECKS_BY_TEXT, ensure_screening_outcome, send_gate_check
    unjudged = await db.candidates.find(
        {
            "appointment_at": {"$gte": now_utc().isoformat()},
            "verdict": None,
            "screening_material": {"$ne": "none"},
        },
        {"_id": 0, "id": 1},
    ).to_list(100)
    judged = 0
    for c in unjudged:
        try:
            res = await ensure_screening_outcome(db, c["id"])
            if res.get("summarized") or res.get("deferred"):
                judged += 1
        except Exception as e:
            logger.warning(f"gate-check sweep: ensure failed for {c.get('id')}: {e}")

    cands = []
    sent = 0
    if GATE_CHECKS_BY_TEXT:
        cands = await db.candidates.find(
            {
                "screening_material": "none",
                "gate_check_sent_at": None,
                "appointment_at": {"$gte": now_utc().isoformat()},
                "phone": {"$nin": [None, ""]},
                "sms_opted_out": {"$ne": True},
            },
            {"_id": 0},
        ).to_list(100)
        for c in cands:
            try:
                if await send_gate_check(db, c):
                    sent += 1
            except Exception as e:
                logger.warning(f"gate-check sweep failed for {c.get('id')}: {e}")
    if cands or unjudged:
        logger.info(f"gate-check sweep: judged {judged}/{len(unjudged)} unjudged bookings; "
                    f"checked {len(cands)}, sent {sent}")
    return {"checked": len(cands), "sent": sent, "unjudged": len(unjudged), "judged": judged}


def schedule_gate_check_sweep() -> None:
    """Hourly, first run 3 minutes after startup so a deploy drains the backlog."""
    if state._scheduler is None:
        return
    try:
        state._scheduler.add_job(
            gate_check_sweep,
            "interval",
            hours=1,
            id="gate-check-sweep",
            replace_existing=True,
            misfire_grace_time=3600,
            max_instances=1,
            next_run_time=now_utc() + timedelta(minutes=3),
        )
        logger.info("gate-check sweep scheduled (hourly, first run in 3 min)")
    except Exception as e:
        logger.warning(f"gate-check sweep schedule failed: {e}")
