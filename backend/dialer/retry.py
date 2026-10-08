"""Auto-retry policy + cancellation for queued retry calls."""
import asyncio
import logging
from datetime import timedelta
from typing import Any, Dict, Optional

from . import state
from .state import now_utc, settings_for
from .window import parse_next_call_at, store_next_call_at
from pubsub import broadcast
from app_tz import app_zone, default_tz_name  # APP_TIMEZONE (noqa: F401)

logger = logging.getLogger("auto_dialer")


async def maybe_schedule_incomplete_retry(
    user_id: str,
    candidate_id: str,
    extra_stagger_seconds: int = 0,
    base_delay_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    """Schedule an automatic re-call for a candidate whose last screening attempt
    didn't complete (voicemail, no-answer, hang-up, no time agreed).

    Cadence is per-attempt: `auto_dialer.retry_delays[idx]` gives the hours to
    wait before the NEXT set (idx 0 = wait after the 1st set), falling back to
    the flat `retry_delay_hours` (default 2h) when the array isn't configured or
    doesn't cover this attempt. Respects the configured call window. Capped at
    `max_retry_attempts` (default 3) using the `call_attempts` counter — which
    counts SETS, not raw calls (the immediate DND-bypass redial shares its set's
    increment; see `place_call._mark_call_initiated`). The actual call is placed
    by `_place_call_now`, which independently aborts if the candidate has
    finished screening online (appointment_at set / screening_status approved or
    rejected) — so this function does NOT need to coordinate with the web-retry
    flow. Mirrors the per-attempt logic in `follow_up._maybe_schedule_retry`.

    `base_delay_minutes` overrides the configured retry_delay_hours when set —
    pass 0 from startup recovery to re-queue past-due candidates immediately
    instead of adding another full delay on top of the one that already elapsed.

    Idempotent: if a future call is already on the books (`next_call_at > now`),
    skips re-scheduling so we don't pile up duplicate jobs."""
    if state._db is None:
        return {"status": "failed", "error": "db not initialized"}
    db = state._db
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user_id}, {"_id": 0})
    if not cand:
        return {"status": "skipped", "reason": "candidate not found"}
    if not cand.get("phone"):
        return {"status": "skipped", "reason": "no phone"}
    if not cand.get("auto_dial", True):
        return {"status": "skipped", "reason": "auto_dial disabled for candidate"}
    # Already finished via web → no point re-dialing.
    if cand.get("appointment_at"):
        return {"status": "skipped", "reason": "appointment already booked"}
    if cand.get("screening_status") in ("approved", "rejected", "paused"):
        return {"status": "skipped", "reason": f"screening_status={cand.get('screening_status')}"}

    settings = await settings_for(user_id, cand.get("pipeline_id"))
    ad = (settings.get("auto_dialer") or {})
    if not ad.get("enabled", True):
        return {"status": "skipped", "reason": "auto_dialer disabled"}
    attempts = int(cand.get("call_attempts") or 0)
    max_attempts = int(ad.get("max_retry_attempts", 3))
    if attempts >= max_attempts:
        return {"status": "skipped", "reason": f"max_retry_attempts={max_attempts} reached"}

    # Idempotency: don't pile up if a future call is already scheduled.
    next_at = cand.get("next_call_at")
    if next_at:
        # Stored ET-naive; parse_next_call_at also tolerates the offset-aware rows
        # older writers left behind, and returns None (→ re-schedule) on garbage.
        tz_name = (settings.get("region_language") or {}).get("timezone") or default_tz_name()
        existing = parse_next_call_at(next_at, tz_name)
        if existing and existing > now_utc():
            return {"status": "skipped", "reason": "future call already scheduled", "scheduled_at": next_at}

    # Per-attempt cadence: retry_delays[idx] is the wait (hours) before the NEXT
    # set. `attempts` (== call_attempts) already counts completed SETS, so
    # idx 0 = wait after the 1st set. Fall back to the flat retry_delay_hours
    # when the array is unset or too short. Kept in lockstep with the per-attempt
    # lookup in follow_up._maybe_schedule_retry.
    retry_delays = ad.get("retry_delays") or []
    idx = max(0, attempts - 1)
    if retry_delays and idx < len(retry_delays):
        delay_hours = int(retry_delays[idx])
    else:
        delay_hours = int(ad.get("retry_delay_hours", 2))
    configured_minutes = delay_hours * 60
    base_minutes = (base_delay_minutes if base_delay_minutes is not None else configured_minutes) + extra_stagger_seconds // 60
    extra_secs_rem = extra_stagger_seconds % 60
    from .queue import schedule_call_with_window
    sched = await schedule_call_with_window(
        user_id, candidate_id, base_delay_minutes=base_minutes + (1 if extra_secs_rem >= 30 else 0),
    )
    logger.info(
        f"incomplete-retry call {attempts + 1}/{max_attempts} scheduled "
        f"for {candidate_id} at {sched.get('scheduled_at')}"
    )
    return {"status": "scheduled", **sched}


async def schedule_dnd_retry(user_id: str, candidate_id: str, delay_seconds: int = 25) -> Dict[str, Any]:
    """Schedule an immediate redial after a first-call voicemail/no-answer.

    iOS 'Allow Repeated Callers' lets the same number ring through DND if
    called again within 3 minutes — 25 s keeps us well inside that window.

    Tracked per call-attempt via `dnd_retry_for_attempt` so each attempt gets
    exactly one DND bypass try. The retry call carries `dnd_retry_active=True`
    on the candidate doc so `build_dynamic_variables` sets `is_dnd_retry=true`,
    instructing the AI to hang up silently if it hits voicemail a second time.
    """
    if state._db is None:
        return {"status": "failed", "error": "db not initialized"}
    db = state._db

    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user_id}, {"_id": 0})
    if not cand:
        return {"status": "skipped", "reason": "candidate not found"}
    if not cand.get("phone"):
        return {"status": "skipped", "reason": "no phone"}
    if cand.get("appointment_at") or cand.get("screening_status") in ("approved", "rejected"):
        return {"status": "skipped", "reason": "candidate already screened"}
    # A pause commonly lands mid-call: /queue/pause cancels queued jobs and
    # clears next_call_at but cannot stop the call that is already ringing. When
    # that call drops to voicemail this is the writer that would flip the
    # candidate back to 'queued' 25 s out — dialing someone the Paused list
    # promises we won't, and hiding them from that list so the recruiter never
    # sees it. Matches the gate `place_call_now`/`should_skip_call` apply.
    if cand.get("screening_status") == "paused":
        return {"status": "skipped", "reason": "dialing paused by recruiter"}

    # One DND retry per call attempt — compare against the current call_attempts count.
    current_attempt = int(cand.get("call_attempts") or 0)
    if cand.get("dnd_retry_for_attempt") == current_attempt:
        return {"status": "skipped", "reason": "DND retry already done for this attempt"}

    settings = await settings_for(user_id, cand.get("pipeline_id"))
    ad = (settings.get("auto_dialer") or {})
    if not ad.get("enabled", True):
        return {"status": "skipped", "reason": "auto_dialer disabled"}
    # Don't DND-retry beyond the normal call cap — each DND retry increments
    # call_attempts, so without this check a persistent voicemail would loop
    # indefinitely and never fall through to the normal 2h retry cadence.
    max_attempts = int(ad.get("max_retry_attempts", 3))
    if current_attempt >= max_attempts:
        return {"status": "skipped", "reason": f"max_retry_attempts={max_attempts} reached"}

    if state._scheduler is None:
        return {"status": "failed", "error": "scheduler not started"}

    # Cancel any 2h retry job that was just queued, replace with the 25s one.
    cancel_pending_call_jobs(candidate_id)

    run_at = now_utc() + timedelta(seconds=delay_seconds)
    from .queue import sched_call
    job_id = sched_call(user_id, candidate_id, run_at=run_at)

    tz_name = (settings.get("region_language") or {}).get("timezone") or default_tz_name()
    next_call_at = store_next_call_at(run_at, tz_name)
    # Update DB only after job is confirmed scheduled — if sched_call threw,
    # dnd_retry_for_attempt stays unset so the next webhook can retry.
    await db.candidates.update_one(
        {"id": candidate_id, "user_id": user_id},
        {"$set": {
            "dnd_retry_active": True,           # signals is_dnd_retry=true to the agent
            # Ages the bypass: a crash between here and the redial would otherwise
            # leave dnd_retry_active set forever, exempting later dials from the
            # call window. place_call expires the flag off this stamp.
            "dnd_retry_armed_at": now_utc().isoformat(),
            "dnd_retry_for_attempt": current_attempt,
            "next_call_at": next_call_at,
            "screening_status": "queued",
            "updated_at": now_utc().isoformat(),
        }},
    )
    logger.info(f"DND bypass retry for {candidate_id} attempt #{current_attempt} in {delay_seconds}s ({job_id})")
    await broadcast(user_id)
    return {"status": "scheduled", "scheduled_at": next_call_at, "job_id": job_id}


def cancel_pending_retry_calls(candidate_id: str) -> int:
    """Cancel every queued auto-retry call AND pre-call SMS for this candidate.
    Called when an appointment is booked through any path (web, retry portal,
    admin /move, reschedule portal) so we don't dial / text someone who's
    already finished.

    Deliberately does NOT match `followup:` jobs: `follow_up.followup_after_call`
    is the only writer of the transcript, summary and verdict for twiml_bridge
    calls (no ElevenLabs post-call webhook fires for those), and the booking door
    `/public/book-by-phone` runs MID-call. Cancelling it here would erase the
    record of the very call that booked the appointment. Re-dialing is already
    prevented inside that job: it returns early for paused/rejected/withdrawn and
    `_maybe_schedule_retry` schedules nothing once `appointment_at` is set.

    NOTE on prefix matching: queue.sched_call creates job IDs of the shape
    `call:{user_id}:{candidate_id}:{ts}` and pre_call_sms.sched_pre_call_sms
    creates `sms:{user_id}:{candidate_id}:{ts}`. We match on the
    `:{candidate_id}:` substring (NOT prefix), since user_id sits between the
    type prefix and the candidate_id."""
    if state._scheduler is None:
        return 0
    removed = 0
    target = f":{candidate_id}:"
    for job in list(state._scheduler.get_jobs()):
        jid = job.id or ""
        if (jid.startswith("call:") or jid.startswith("sms:")) and target in jid:
            try:
                state._scheduler.remove_job(jid)
                removed += 1
            except Exception:
                pass
    if removed:
        logger.info(f"cancelled {removed} queued call/SMS job(s) for {candidate_id}")
    # Also clear next_call_at on the candidate doc so the UI reflects reality.
    if state._db is not None:
        try:
            asyncio.create_task(state._db.candidates.update_one(
                {"id": candidate_id},
                {"$set": {"next_call_at": None, "updated_at": now_utc().isoformat()}},
            ))
        except Exception:
            pass
    return removed


def cancel_pending_call_jobs(candidate_id: str) -> int:
    """Cancel ONLY queued call+pre-call-SMS jobs for this candidate, without
    clearing `next_call_at` (the caller is about to set a new one). Used by
    `schedule_call_with_window` to ensure rescheduling never produces duplicate
    SMS/call jobs.

    Same matching rules as `cancel_pending_retry_calls`: job IDs follow
    `call:{user}:{candidate}:{ts}` and `sms:{user}:{candidate}:{ts}`, and
    `followup:` is likewise left alone — see that function's docstring.
    """
    if state._scheduler is None:
        return 0
    removed = 0
    target = f":{candidate_id}:"
    for job in list(state._scheduler.get_jobs()):
        jid = job.id or ""
        if (jid.startswith("call:") or jid.startswith("sms:")) and target in jid:
            try:
                state._scheduler.remove_job(jid)
                removed += 1
            except Exception:
                pass
    if removed:
        logger.info(f"cleared {removed} stale call/SMS job(s) for {candidate_id} before re-scheduling")
    return removed
