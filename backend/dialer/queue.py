"""Outbound-call queueing: window-aware scheduling + batch dialing."""
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import pytz

from . import state
from ._call_helpers import should_skip_call
from .place_call import place_call_now
from .pre_call_sms import sched_pre_call_sms
from .state import now_utc, settings_for
from .window import next_in_window, parse_hhmm, store_next_call_at
from app_tz import app_zone, default_tz_name  # APP_TIMEZONE (noqa: F401)

logger = logging.getLogger("auto_dialer")


def sched_call(user_id: str, candidate_id: str, run_at: Optional[datetime] = None) -> str:
    """Schedule an outbound call. Returns the job id."""
    if state._scheduler is None:
        raise RuntimeError("scheduler not started")
    run_at = run_at or now_utc()
    job_id = f"call:{user_id}:{candidate_id}:{int(run_at.timestamp())}"
    state._scheduler.add_job(
        place_call_now,
        "date",
        run_date=run_at,
        args=[user_id, candidate_id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=300,
    )
    logger.info(f"scheduled call {job_id} for {run_at.isoformat()}")
    return job_id


async def schedule_call_with_window(user_id: str, candidate_id: str, base_delay_minutes: int = 0, force: bool = False) -> Dict[str, Any]:
    """Pick the next valid call time inside the user's call window, then schedule.
    If force=True, ignore the call window — schedule for `now + base_delay_minutes`.
    Also schedules a pre-call warmup SMS at `call_at - pre_call_sms_minutes`.

    Idempotent: cancels any pre-existing queued call+pre-call-SMS jobs for
    this candidate FIRST so re-scheduling (e.g. recruiter drags to SCREENING
    after the apply portal already queued a call) never produces duplicate
    SMS deliveries — see `cancel_pending_call_jobs` for the matching rules.
    """
    if state._db is None:
        return {"status": "failed", "error": "db not initialized"}
    cand = await state._db.candidates.find_one({"id": candidate_id, "user_id": user_id}, {"_id": 0}) or {}
    settings = await settings_for(user_id, cand.get("pipeline_id"))
    ad = (settings.get("auto_dialer") or {})
    rl = (settings.get("region_language") or {})
    tz_name = (rl.get("timezone") or default_tz_name())
    if not ad.get("enabled", True) and not force:
        return {"status": "skipped", "reason": "auto_dialer disabled"}

    # Same gate `place_call_now` applies at dial time. Without it we mint a
    # 'queued' row with a live countdown for someone the dialer will refuse
    # forever (no phone, archived, auto_dial off, past screening), and the Call
    # Queue advertises a call that is never going to happen.
    #
    # `force` means the candidate picked this moment themselves (retry portal,
    # "call me at 10" by SMS or email reply). Those callers confirm the booking
    # to the candidate without reading this status, so declining silently is
    # worse than the row we'd rather not mint — main booked them whatever the
    # record said, and place_call_now still refuses at dial time.
    #
    # A recruiter's pause is the exception that survives `force`: it is the
    # recruiter overruling the candidate, and booking here would arm a redial
    # and a warmup SMS for someone the Paused list promises we won't contact.
    # /queue/resume — the only exit from 'paused' — clears the status to
    # 'pending' before calling, and this function re-fetches the doc, so the
    # deliberate un-pause reaches the write below and nothing else does.
    skip, skip_reason = should_skip_call(cand)
    if skip and (not force or cand.get("screening_status") == "paused"):
        # '__failed__:' marks a hard failure at dial time; at queue time every
        # ineligibility is simply a call we decline to book.
        return {"status": "skipped", "reason": (skip_reason or "").removeprefix("__failed__:")}

    # Wipe any existing queued call/SMS jobs for this candidate before we add
    # new ones — prevents duplicate SMS deliveries when this function is called
    # more than once (apply → drag-to-SCREENING, manual /call, etc.).
    from .retry import cancel_pending_call_jobs
    cancel_pending_call_jobs(candidate_id)

    desired = now_utc() + timedelta(minutes=base_delay_minutes)

    if force:
        actual = desired
    else:
        actual = next_in_window(
            desired,
            tz_name=tz_name,
            start_hhmm=ad.get("call_window_start", "09:00"),
            end_hhmm=ad.get("call_window_end", "19:00"),
            allowed_days=ad.get("call_window_days") or [0, 1, 2, 3, 4],
        )
        # Slot-based stagger: pack up to `max_concurrent_calls` candidates into
        # each slot, then overflow to the next slot.  This lets the dialer fully
        # utilise concurrent ElevenLabs agents instead of forcing every candidate
        # to wait their own turn.
        max_concurrent = int(ad.get("max_concurrent_calls", 5))
        # Slot size = average call duration so the next batch only fires after
        # the current one has mostly finished, keeping peak concurrency ≤ max_concurrent.
        slot_minutes = int(ad.get("call_slot_minutes", 5))

        # All next_call_at values are stored as Eastern-Time naive ISO strings
        # ("2026-05-18T17:17:00").  We must compare against the same format.
        # When a slot crosses the window end we jump forward to the next valid
        # window start (tomorrow / next allowed day) so calls are never placed
        # outside working hours.
        try:
            _tz = pytz.timezone(tz_name)
        except Exception:
            _tz = pytz.UTC
        _eh, _em = parse_hhmm(ad.get("call_window_end", "19:00"))
        _win_start_hhmm = ad.get("call_window_start", "09:00")
        _win_end_hhmm = ad.get("call_window_end", "19:00")
        _win_days = ad.get("call_window_days") or [0, 1, 2, 3, 4]

        slot_start = actual
        for _ in range(7 * 24 * 60 // slot_minutes):  # up to 7 days of slots
            local_slot = slot_start.astimezone(_tz)
            win_end_local = local_slot.replace(hour=_eh, minute=_em, second=0, microsecond=0)
            if local_slot >= win_end_local:
                # Past today's window — jump to next valid window start.
                slot_start = next_in_window(
                    slot_start, tz_name=tz_name,
                    start_hhmm=_win_start_hhmm, end_hhmm=_win_end_hhmm,
                    allowed_days=_win_days,
                )
                local_slot = slot_start.astimezone(_tz)
            slot_end = slot_start + timedelta(minutes=slot_minutes)
            # Bounds go through the same serializer as the write below, so the
            # occupancy query can never drift out of sync with the stored format.
            slot_start_et = store_next_call_at(slot_start, tz_name)
            slot_end_et = store_next_call_at(slot_end, tz_name)
            count_in_slot = await state._db.candidates.count_documents({
                "user_id": user_id,
                "id": {"$ne": candidate_id},
                "screening_status": "queued",
                "next_call_at": {
                    "$gte": slot_start_et,
                    "$lt": slot_end_et,
                },
            })
            if count_in_slot < max_concurrent:
                actual = slot_start
                break
            slot_start = slot_end
        else:
            actual = slot_start
    job_id = sched_call(user_id, candidate_id, run_at=actual)
    next_call_at_str = store_next_call_at(actual, tz_name)
    await state._db.candidates.update_one(
        {"id": candidate_id, "user_id": user_id},
        # Overwriting screening_status here can only ever lift a pause that the
        # resume endpoint already cleared: the gate above turns away every doc
        # that is still 'paused', force or not.
        #
        # `force` means the candidate picked this moment themselves, so the
        # fire-time window gate has to let it through — and the job can outlive a
        # restart by up to 14 days, which no scheduler argument survives. Writing
        # the flag on both branches means an ordinary recruiter re-queue clears a
        # stale grant in the same breath, so it can't leak into a campaign dial.
        {"$set": {"next_call_at": next_call_at_str, "screening_status": "queued",
                  "candidate_initiated_call": force}},
    )

    # Schedule pre-call warmup SMS at (call_at - pre_call_sms_minutes).
    #
    # Suppressed under chat_first/chat_only. There the candidate already got the
    # arrival text and up to three chase texts; a "warmup" SMS three minutes
    # before the fallback call would say "expect a call in ~3 min for a quick
    # 5-min AI screening" — contradicting the whole text-first promise and
    # arriving as yet another message. The call under those modes is a fallback,
    # not a pre-announced event.
    from models import resolve_screening_mode
    _mode = resolve_screening_mode(settings)
    sms_job_id = None
    sms_at = None
    if ad.get("pre_call_sms_enabled", True) and _mode not in ("chat_first", "chat_only"):
        lead_min = int(ad.get("pre_call_sms_minutes", 3))
        sms_at = actual - timedelta(minutes=lead_min)
        if sms_at <= now_utc() + timedelta(seconds=15):
            # SMS would be in the past — fire 20s from now
            sms_at = now_utc() + timedelta(seconds=20)
        sms_job_id = sched_pre_call_sms(user_id, candidate_id, run_at=sms_at)

    return {
        "status": "scheduled",
        "scheduled_at": next_call_at_str,
        "job_id": job_id,
        "pre_call_sms_at": store_next_call_at(sms_at, tz_name) if sms_job_id else None,
        "pre_call_sms_job_id": sms_job_id,
    }


async def batch_dial_pipeline(user_id: str, pipeline_id: str) -> Dict[str, Any]:
    """Dial all eligible candidates in a pipeline (stage=APPLICANT or queued/no_answer,
    has phone, auto_dial=true, not archived/paused/finalised, retries left)."""
    if state._db is None:
        return {"status": "failed", "error": "db not initialized"}
    db = state._db
    settings = await settings_for(user_id, pipeline_id)
    ad = (settings.get("auto_dialer") or {})
    interval = int(ad.get("batch_interval_seconds", 30))
    # The `stage: APPLICANT` arm of the `$or` readmits candidates regardless of
    # screening_status, so paused/archived people the recruiter deliberately
    # pulled out come straight back; `$nin` + `archived_at` close that. The
    # `call_attempts` bound mirrors the cap `retry.maybe_schedule_incomplete_retry`
    # enforces so "Dial all" can't resurrect a no_answer candidate the retry
    # ladder already gave up on (`$not` also matches docs with no counter yet).
    max_attempts = int(ad.get("max_retry_attempts", 3))
    candidates = await db.candidates.find(
        {"user_id": user_id, "pipeline_id": pipeline_id, "auto_dial": True,
         "phone": {"$ne": ""},
         "archived_at": None,
         "screening_status": {"$nin": ["paused", "in_progress", "approved", "rejected"]},
         "call_attempts": {"$not": {"$gte": max_attempts}},
         "$or": [{"stage": "APPLICANT"}, {"screening_status": "queued"}, {"screening_status": "no_answer"}]},
        {"_id": 0},
    ).to_list(500)
    scheduled = []
    for i, c in enumerate(candidates):
        delay_min = (i * interval) // 60
        sched = await schedule_call_with_window(user_id, c["id"], base_delay_minutes=delay_min)
        scheduled.append({"candidate_id": c["id"], "name": f"{c.get('first_name','')} {c.get('last_name','')}".strip(), **sched})
    # Count only what was actually booked — schedule_call_with_window declines
    # (dialer off, candidate ineligible) without writing anything, and the toast
    # reporting this number sits next to the queue tile it has to agree with.
    return {"queued": sum(1 for s in scheduled if s.get("status") == "scheduled"), "scheduled": scheduled}
