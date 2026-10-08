"""The last net under the ElevenLabs sync: no call stays empty in silence.

Two things copy a screening out of ElevenLabs into Mongo — the post-call
webhook and the auto-sync sweep — and both used to conclude "no transcript"
the moment EL hadn't produced one yet. Both now defer instead. But deferring
only helps while a row still LOOKS unfinished: the auto-sync sweep finds work
by status (in_progress/initiated/ringing/queued), so any conversation that
reaches a terminal status with an empty transcript drops out of its view
permanently. That is how real screenings, several of them five minutes
long, became invisible to the platform, and how their
candidates ended up flagged as "booked but never screened".

This sweep is deliberately blind to status. It re-asks ElevenLabs for anything
that still has no transcript, a few times, for a day.

It writes to the CONVERSATION only — never to the candidate. A late transcript
must not re-run screening outcomes on an old call: that would fire rejection
emails, slot-picker links and retry dials at people whose cases have long since
moved on. Restoring the record is enough; `ensure_screening_outcome` reads it
for anyone still booked, and a recruiter can read it in the drawer.
"""
import logging
from datetime import timedelta

from . import state
from .state import now_utc

logger = logging.getLogger("auto_dialer")

# Bounded on every axis, because the common case is a call that genuinely has
# nothing to fetch: many dials never connect.
MAX_PER_TICK = 40
MAX_ATTEMPTS = 3
LOOKBACK_HOURS = 24
# Below this the auto-sync sweep still owns the row and is actively polling it.
SETTLE_MINUTES = 20


async def late_transcript_recovery_sweep() -> dict:
    if state._db is None:
        return {"checked": 0, "recovered": 0}
    db = state._db
    now = now_utc()
    rows = await db.conversations.find(
        {
            "elevenlabs_conversation_id": {"$exists": True, "$ne": None},
            "created_at": {
                "$gte": (now - timedelta(hours=LOOKBACK_HOURS)).isoformat(),
                "$lt": (now - timedelta(minutes=SETTLE_MINUTES)).isoformat(),
            },
            "$or": [
                {"transcript": None},
                {"transcript": {"$size": 0}},
                {"transcript": {"$exists": False}},
            ],
            "transcript_recovery_attempts": {"$lt": MAX_ATTEMPTS},
        },
        {"_id": 0, "id": 1, "elevenlabs_conversation_id": 1, "duration_seconds": 1,
         "status": 1, "candidate_id": 1},
    ).to_list(MAX_PER_TICK)
    if not rows:
        return {"checked": 0, "recovered": 0}

    from transcript_sync import recover_conversation

    recovered = 0
    for r in rows:
        was = r.get("status")
        got = await recover_conversation(db, r)
        # Count the attempt either way, so a dial that truly rang out is asked
        # about three times and then left alone forever.
        await db.conversations.update_one(
            {"id": r["id"]}, {"$inc": {"transcript_recovery_attempts": 1}})
        if not got:
            continue
        recovered += 1
        logger.warning(
            f"late transcript recovery: {r['elevenlabs_conversation_id']} "
            f"({r.get('duration_seconds')}s, was '{was}') had "
            f"{len(r.get('transcript') or [])} turns at ElevenLabs that never reached "
            f"us — restored for candidate {r.get('candidate_id')}"
        )
    if recovered:
        logger.info(f"late transcript recovery: {recovered}/{len(rows)} restored")
    return {"checked": len(rows), "recovered": recovered}


def schedule_late_transcript_recovery() -> None:
    """Every 15 minutes, first run 5 minutes after startup."""
    if state._scheduler is None:
        return
    try:
        state._scheduler.add_job(
            late_transcript_recovery_sweep,
            "interval",
            minutes=15,
            id="late-transcript-recovery",
            replace_existing=True,
            misfire_grace_time=600,
            max_instances=1,
            next_run_time=now_utc() + timedelta(minutes=5),
        )
        logger.info("late transcript recovery scheduled (every 15 min, first run in 5 min)")
    except Exception as e:
        logger.warning(f"late transcript recovery schedule failed: {e}")
