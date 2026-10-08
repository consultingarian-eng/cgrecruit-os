"""No-show revival: AI courtesy calls to archived no-show candidates.

Flow: hourly tick (registered in scheduler.start_scheduler) → eligibility
query → staggered date jobs → `place_revival_call` → if the candidate answers
and is still interested, the agent books via the same `book_slot` tool the
screening agent uses (book-by-phone un-archives + restores them to the
kanban) → post-call handled by `handle_revival_post_call`, branched from BOTH
routes/webhooks.py and scheduler._auto_sync_stuck_conversations (the latter
is the primary path in practice — the EL webhook is unreliable).

Revival dialing deliberately never touches stage / screening_status /
archived_at / appointment_at — the candidate stays archived unless they
actually rebook mid-call.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytz

from . import state
from ._call_helpers import build_dynamic_variables, compute_caps, is_blocked
from .state import now_utc, settings_for
from .window import next_in_window
from app_tz import app_zone, default_tz_name  # APP_TIMEZONE (noqa: F401)

logger = logging.getLogger("auto_dialer")

_DEFAULTS = {
    "enabled": False,
    "days_after_appointment": 7,
    "max_attempts": 3,
    "attempt_gap_hours": 24,
    "lookback_days": 60,
    "daily_cap": 15,
}

# Never call archived no-shows whose outcome is already final.
_FINAL_OUTCOMES = ("rebooked", "declined")


def revival_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Merge the stored no_show_revival section over defaults — existing
    settings docs won't have the key until first save."""
    return {**_DEFAULTS, **(settings.get("no_show_revival") or {})}


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    """Tolerant ISO parse across the codebase's mixed formats (Z / +00:00 /
    naive). Naive treated as UTC — hours-level skew is irrelevant against the
    day-granularity revival thresholds."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


async def eligible_revival_candidates(
    db, user_id: str, pipeline_id: str, rev: Dict[str, Any], limit: int = 50,
) -> List[Dict[str, Any]]:
    """Archived no-shows due for a revival call. Shared by the hourly tick and
    the /revival/preview dry-run endpoint (zero writes)."""
    now = now_utc()
    max_attempts = int(rev.get("max_attempts") or 3)
    gap_hours = int(rev.get("attempt_gap_hours") or 24)
    newest = now - timedelta(days=int(rev.get("days_after_appointment") or 7))
    oldest = now - timedelta(days=int(rev.get("lookback_days") or 60))

    # Coarse string bounds on the date prefix work across the mixed ISO formats
    # (both start YYYY-MM-DD); exact boundaries re-verified in Python below.
    q = {
        "user_id": user_id,
        "pipeline_id": pipeline_id,
        "archived_at": {"$ne": None},
        "archived_reason": "no_show_not_rescheduled",
        "phone": {"$nin": [None, ""]},
        "appointment_at": {
            "$gte": (oldest - timedelta(days=2)).strftime("%Y-%m-%d"),
            "$lte": (newest + timedelta(days=2)).strftime("%Y-%m-%dT23:59:59"),
        },
        "$or": [
            {"revival_call_attempts": {"$exists": False}},
            {"revival_call_attempts": {"$lt": max_attempts}},
        ],
        "revival_outcome": {"$nin": list(_FINAL_OUTCOMES)},
    }
    docs = await db.candidates.find(q, {"_id": 0}).sort("appointment_at", -1).to_list(500)

    # One query to find phones that also belong to an ACTIVE candidate in this
    # pipeline — book-by-phone matches newest-first by phone, so dialing the
    # archived doc would book against the wrong (active) record.
    phones = [c["phone"] for c in docs if c.get("phone")]
    active_phones = set()
    if phones:
        async for a in db.candidates.find(
            {"pipeline_id": pipeline_id, "phone": {"$in": phones},
             "$or": [{"archived_at": None}, {"archived_at": {"$exists": False}}]},
            {"_id": 0, "phone": 1},
        ):
            active_phones.add(a.get("phone"))

    out: List[Dict[str, Any]] = []
    for c in docs:
        appt = _parse_iso(c.get("appointment_at"))
        if not appt or appt > newest or appt < oldest:
            continue
        if c.get("phone") in active_phones:
            continue
        last = _parse_iso(c.get("revival_last_attempt_at"))
        if last and (now - last) < timedelta(hours=gap_hours):
            continue
        out.append(c)
        if len(out) >= limit:
            break
    return out


def sched_revival_call(user_id: str, candidate_id: str, run_at: Optional[datetime] = None) -> str:
    """Schedule a revival dial. The `call:` prefix is deliberate — the generic
    job sweeps in dialer.retry (cancel_pending_retry_calls / _call_jobs) match
    `call:*:{candidate_id}:*`, so a booking through any path automatically
    cancels queued revival dials too."""
    if state._scheduler is None:
        raise RuntimeError("scheduler not started")
    run_at = run_at or now_utc()
    job_id = f"call:revival:{user_id}:{candidate_id}:{int(run_at.timestamp())}"
    state._scheduler.add_job(
        place_revival_call,
        "date",
        run_date=run_at,
        args=[user_id, candidate_id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=600,
    )
    logger.info(f"scheduled revival call {job_id} for {run_at.isoformat()}")
    return job_id


async def _last_stated_reason(db, cand: Dict[str, Any]) -> tuple:
    """Return (reason, last_message) describing why this candidate wasn't there,
    drawn from what they actually told us by text or email.

    Returns ("", "") when they never said anything — the agent then falls back
    to asking. Everything here is candidate-authored text handed to a voice
    agent as context, so it is truncated and never treated as instruction."""
    cid = cand.get("id")
    reason = (cand.get("appointment_cancel_reason") or "").strip()
    latest_text = ""
    latest_at = ""
    try:
        rows = await db.training_sms_messages.find(
            {"candidate_id": cid, "direction": "in"},
            {"_id": 0, "body": 1, "timestamp": 1},
        ).sort("timestamp", -1).limit(1).to_list(1)
        if rows:
            latest_text = (rows[0].get("body") or "").strip()
            latest_at = str(rows[0].get("timestamp") or "")
    except Exception as e:
        logger.warning(f"revival: sms history lookup failed for {cid}: {e}")
    try:
        rows = await db.email_reply_messages.find(
            {"candidate_id": cid, "direction": "inbound"},
            {"_id": 0, "body": 1, "sent_at": 1},
        ).sort("sent_at", -1).limit(1).to_list(1)
        if rows and str(rows[0].get("sent_at") or "") > latest_at:
            latest_text = (rows[0].get("body") or "").strip()
    except Exception as e:
        logger.warning(f"revival: email history lookup failed for {cid}: {e}")
    if not reason:
        reason = latest_text
    return (reason[:300], latest_text[:300])


async def place_revival_call(user_id: str, candidate_id: str, manual: bool = False) -> Dict[str, Any]:
    """Dial one archived no-show with the pipeline's revival agent.
    `manual=True` (drawer button / API trigger) bypasses the attempt cap and
    call window but keeps the archived/final-outcome/concurrency guards."""
    if state._db is None:
        return {"status": "failed", "error": "db not initialized"}
    db = state._db

    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user_id}, {"_id": 0})
    if not cand:
        return {"status": "failed", "error": "candidate not found"}
    if not cand.get("phone"):
        return {"status": "failed", "error": "candidate has no phone"}
    if not cand.get("archived_at"):
        return {"status": "skipped", "reason": "candidate is no longer archived"}
    # Hard gate on the archive reason — the script opens with "you couldn't
    # make it", which is only ever true for lapsed no-shows. Attended-no-form,
    # manual archives, rejections etc. must never get this call, even via a
    # direct API hit (the UI already hides the button for them).
    if cand.get("archived_reason") != "no_show_not_rescheduled":
        return {"status": "failed", "error": f"Revival calls only target lapsed no-shows — this candidate is archived as '{cand.get('archived_reason') or 'unknown'}'"}
    if cand.get("revival_outcome") in _FINAL_OUTCOMES:
        return {"status": "skipped", "reason": f"revival already {cand.get('revival_outcome')}"}

    settings = await settings_for(user_id, cand.get("pipeline_id"))
    rev = revival_settings(settings)
    # A pending DND redial belongs to the attempt that scheduled it — it must
    # run even when that attempt was the last one allowed.
    if (
        not manual
        and not cand.get("dnd_retry_active")
        and int(cand.get("revival_call_attempts") or 0) >= int(rev.get("max_attempts") or 3)
    ):
        return {"status": "skipped", "reason": "max revival attempts reached"}

    pipe = await db.pipelines.find_one(
        {"id": cand.get("pipeline_id"), "user_id": user_id}, {"_id": 0},
    ) or {}
    agent_id = pipe.get("revival_agent_id") or ""
    if not agent_id:
        return {"status": "failed", "error": "No revival agent for this pipeline — run POST /api/revival/agent/sync first (Settings → No-Show Revival)"}

    # Don't dial the archived doc if an active candidate now shares the phone —
    # book-by-phone would book against the wrong record.
    active_dup = await db.candidates.find_one(
        {"pipeline_id": cand.get("pipeline_id"), "phone": cand["phone"], "id": {"$ne": candidate_id},
         "$or": [{"archived_at": None}, {"archived_at": {"$exists": False}}]},
        {"_id": 0, "id": 1},
    )
    if active_dup:
        return {"status": "skipped", "reason": "an active candidate shares this phone number"}

    # Concurrency — same dual caps as screening, counted through dialer.live_count
    # so the revival cap, the screening cap and the Call Queue meter can never
    # disagree. Both counts are conversation-derived, so the revival calls already
    # on the phone count themselves — no private in-flight query here.
    pipe_cap, tenant_cap, postpone_secs = compute_caps(settings)
    if pipe_cap > 0 or tenant_cap > 0:
        from .live_count import count_pipeline_live, count_tenant_live
        # Exclude self on both terms: a DND redial must not lose a cap slot to its
        # own predecessor conversation, which stays non-terminal for the whole
        # ghost window while the post-call sync catches up.
        live_total = await count_tenant_live(db, user_id, exclude_candidate_id=candidate_id)
        live_pipe = await count_pipeline_live(
            db, user_id, cand.get("pipeline_id"), exclude_candidate_id=candidate_id,
        )
        blocked, reason = is_blocked(live_pipe, live_total, pipe_cap, tenant_cap)
        if blocked:
            if manual:
                return {"status": "busy", "reason": f"concurrency cap hit ({reason}) — try again in a minute"}
            run_at = now_utc() + timedelta(seconds=postpone_secs)
            sched_revival_call(user_id, candidate_id, run_at=run_at)
            logger.info(f"revival cap hit ({reason}) — postponed {candidate_id} by {postpone_secs}s")
            return {"status": "postponed", "reason": reason}

    # No point calling with nothing to offer — the tick re-picks once slots open.
    if pipe.get("availability_rules"):
        from availability_service import compute_available_slots
        region = settings.get("region_language") or {}
        appt_settings = settings.get("appointments") or {}
        booked = await db.candidates.find(
            {"pipeline_id": pipe["id"], "appointment_at": {"$ne": None}},
            {"_id": 0, "appointment_at": 1},
        ).to_list(2000)
        slots = compute_available_slots(
            pipe, [c["appointment_at"] for c in booked if c.get("appointment_at")],
            days_ahead=5, tz_name=region.get("timezone") or default_tz_name(),
            default_capacity=int(appt_settings.get("applicant_limit") or 50), max_slots=1,
        )
        if not slots:
            return {"status": "skipped", "reason": "no bookable slots in the next 5 days — add availability first"}

    sca = settings.get("screen_call_agent", {}) or {}
    profile = settings.get("recruiter_profile", {}) or {}
    job = None
    if cand.get("job_id"):
        job = await db.jobs.find_one({"id": cand["job_id"], "user_id": user_id}, {"_id": 0})

    agent_name = pipe.get("agent_name_override") or sca.get("agent_name", "Olivia")
    phone_number_id = pipe.get("elevenlabs_phone_number_id_override") or sca.get("elevenlabs_phone_number_id", "")

    dyn = build_dynamic_variables(cand, pipe, profile, job, agent_name)
    region = settings.get("region_language") or {}
    try:
        tz = pytz.timezone(region.get("timezone") or default_tz_name())
    except Exception:
        tz = pytz.UTC
    appt_dt = _parse_iso(cand.get("appointment_at"))
    if appt_dt:
        local_appt = appt_dt.astimezone(tz)
        dyn["original_appointment_date"] = local_appt.strftime("%A, %B %-d at %-I:%M %p")
        dyn["days_since_appointment"] = str(max(0, (now_utc() - appt_dt).days))
    else:
        dyn["original_appointment_date"] = "recently"
        dyn["days_since_appointment"] = "a few"

    # What the candidate already told us, in their own words. Opening with "we
    # noticed you couldn't make it" at someone who texted about a family
    # emergency two days ago reads as nobody having read it.
    reason, said = await _last_stated_reason(db, cand)
    dyn["known_absence_reason"] = reason
    dyn["candidate_last_message"] = said

    from voice_service import initiate_elevenlabs_outbound_call
    from models import Conversation

    # DND-bypass redial (scheduled by handle_revival_post_call on voicemail):
    # shares its set's attempt increment, exactly like the screening dialer —
    # build_dynamic_variables reads dnd_retry_active → is_dnd_retry=true, which
    # tells the agent to hang up silently on a second voicemail.
    is_dnd_redial = bool(cand.get("dnd_retry_active"))

    result = initiate_elevenlabs_outbound_call(
        candidate_phone=cand["phone"],
        agent_id=agent_id,
        agent_phone_number_id=phone_number_id,
        dynamic_variables=dyn,
        conversation_config_override=None,
    )
    conv = Conversation(
        candidate_id=candidate_id, user_id=user_id,
        elevenlabs_conversation_id=result.get("conversation_id"),
        twilio_call_sid=result.get("call_sid"),
        status="initiated" if result.get("status") == "initiated" else "failed",
        call_type="no_show_revival",
        revival_prev_appointment_at=cand.get("appointment_at"),
        dynamic_variables=dyn,
    )
    await db.conversations.insert_one(conv.model_dump())
    update: Dict[str, Any] = {
        "$set": {
            "revival_last_attempt_at": now_utc().isoformat(),
            "last_call_status": result.get("status"),
            "updated_at": now_utc().isoformat(),
        },
    }
    if is_dnd_redial:
        update["$unset"] = {"dnd_retry_active": ""}
    else:
        update["$inc"] = {"revival_call_attempts": 1}
    await db.candidates.update_one({"id": candidate_id, "user_id": user_id}, update)
    logger.info(f"revival call placed for {candidate_id}: {result.get('status')} (conv {conv.id}, dnd_redial={is_dnd_redial})")
    return {"status": result.get("status"), "conversation_id": conv.id, "agent_id": agent_id, "result": result}


async def _no_show_revival_tick() -> None:
    """Hourly: per enabled pipeline, queue due revival calls — inside the call
    window, under the daily cap, staggered like batch-dial. Lost date jobs
    (redeploy) are re-picked here because the attempt is stamped at DIAL time."""
    if state._db is None or state._scheduler is None:
        return
    db = state._db
    pipelines = await db.pipelines.find({}, {"_id": 0}).to_list(200)
    for pipe in pipelines:
        try:
            user_id = pipe.get("user_id")
            if not user_id:
                continue
            settings = await settings_for(user_id, pipe["id"])
            rev = revival_settings(settings)
            if not rev.get("enabled"):
                continue
            if not pipe.get("revival_agent_id"):
                logger.info(f"revival tick: pipeline '{pipe.get('name')}' enabled but has no revival agent — run /api/revival/agent/sync")
                continue
            ad = settings.get("auto_dialer") or {}
            rl = settings.get("region_language") or {}
            tz_name = rl.get("timezone") or default_tz_name()
            now = now_utc()
            in_window_at = next_in_window(
                now, tz_name=tz_name,
                start_hhmm=ad.get("call_window_start", "09:00"),
                end_hhmm=ad.get("call_window_end", "19:00"),
                allowed_days=ad.get("call_window_days") or [0, 1, 2, 3, 4],
            )
            if (in_window_at - now).total_seconds() > 60:
                continue  # outside call window — next tick will catch it

            # Daily cap counts attempts since local midnight.
            try:
                tz = pytz.timezone(tz_name)
            except Exception:
                tz = pytz.UTC
            local_midnight = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
            midnight_utc_iso = local_midnight.astimezone(timezone.utc).isoformat()
            used = await db.candidates.count_documents({
                "user_id": user_id, "pipeline_id": pipe["id"],
                "revival_last_attempt_at": {"$gte": midnight_utc_iso},
            })
            remaining = int(rev.get("daily_cap") or 15) - used
            if remaining <= 0:
                continue

            cands = await eligible_revival_candidates(db, user_id, pipe["id"], rev, limit=remaining)
            if not cands:
                continue

            # Don't double-queue candidates that already have a pending job.
            pending: set = set()
            for job in state._scheduler.get_jobs():
                jid = job.id or ""
                if jid.startswith("call:revival:"):
                    parts = jid.split(":")
                    if len(parts) > 3:
                        pending.add(parts[3])

            interval = int(ad.get("batch_interval_seconds", 30))
            queued = 0
            for c in cands:
                if c["id"] in pending:
                    continue
                sched_revival_call(user_id, c["id"], run_at=now + timedelta(seconds=queued * interval + 5))
                queued += 1
            if queued:
                logger.info(f"revival tick: queued {queued} call(s) for pipeline '{pipe.get('name')}' ({used + queued}/{rev.get('daily_cap')} today)")
        except Exception as e:
            logger.warning(f"revival tick failed for pipeline {pipe.get('id')}: {e}")


async def handle_revival_post_call(
    db, conv: Dict[str, Any], cand: Dict[str, Any],
    transcript: List[Dict[str, str]], duration: Optional[int], voicemail: bool,
) -> Dict[str, Any]:
    """Post-call processing for revival conversations — called from BOTH the
    ElevenLabs webhook and the auto-sync sweep. Never applies screening logic
    (verdict / screening_status / retries / DND): the only state transitions
    are the revival_* fields, plus whatever book-by-phone already did mid-call
    if the candidate rebooked."""
    user_id = conv["user_id"]
    cand_id = conv["candidate_id"]

    # Rebooked = archive cleared AND appointment moved off the dial-time
    # snapshot (appointment_at held the OLD missed slot when we dialed).
    prev_at = conv.get("revival_prev_appointment_at")
    new_at = cand.get("appointment_at")
    rebooked = bool(not cand.get("archived_at") and new_at and new_at != prev_at)
    if rebooked:
        new_dt = _parse_iso(new_at)
        if new_dt and new_dt < now_utc():
            rebooked = False

    candidate_spoke = any(t.get("role") in ("user", "human") for t in (transcript or []))
    is_complete = candidate_spoke and (duration or 0) > 30 and not voicemail

    summary: Dict[str, Any] = {}
    if (is_complete or rebooked) and transcript:
        try:
            from ai_service import summarize_revival_call
            summary = await summarize_revival_call(transcript, cand.get("first_name", ""))
        except Exception as e:
            logger.warning(f"revival summary failed for {cand_id}: {e}")

    still = summary.get("still_interested")
    if rebooked:
        outcome = "rebooked"
    elif is_complete and still is False:
        outcome = "declined"
    elif is_complete:
        outcome = "interested_no_booking"  # spoke but didn't book — retryable
    elif voicemail:
        outcome = "voicemail"
    else:
        outcome = "no_answer"

    conv_status = "completed" if (is_complete or rebooked) else "no_answer"
    await db.conversations.update_one(
        {"id": conv["id"], "user_id": user_id},
        {"$set": {
            "transcript": transcript or [],
            "summary": summary.get("summary", ""),
            "duration_seconds": duration,
            "status": conv_status,
        }},
    )
    cand_update: Dict[str, Any] = {
        "revival_outcome": outcome,
        "last_call_status": conv_status,
        "last_call_voicemail": bool(voicemail),
        "updated_at": now_utc().isoformat(),
    }
    feedback = (summary.get("feedback") or "").strip()
    if feedback:
        cand_update["revival_feedback"] = feedback[:500]
    await db.candidates.update_one({"id": cand_id, "user_id": user_id}, {"$set": cand_update})

    settings = await settings_for(user_id, cand.get("pipeline_id"))
    rev = revival_settings(settings)
    attempts = int(cand.get("revival_call_attempts") or 0)

    # DND-bypass redial — mirrors the screening dialer's set model: one ~25s
    # redial per attempt on voicemail (iOS 'Allow Repeated Callers' rings
    # through DND when the same number calls back within 3 minutes). The redial
    # shares its set's attempt increment; `revival_dnd_for_attempt` guarantees
    # exactly one bypass per set so a persistent voicemail can't loop.
    dnd_scheduled = False
    if (
        outcome == "voicemail"
        and cand.get("archived_at")
        and cand.get("revival_dnd_for_attempt") != attempts
        and state._scheduler is not None
    ):
        await db.candidates.update_one(
            {"id": cand_id, "user_id": user_id},
            {"$set": {"dnd_retry_active": True, "revival_dnd_for_attempt": attempts}},
        )
        sched_revival_call(user_id, cand_id, run_at=now_utc() + timedelta(seconds=25))
        dnd_scheduled = True
        logger.info(f"revival DND bypass redial for {cand_id} attempt #{attempts} in 25s")

    # First-attempt follow-up email: the set ended in voicemail/no-answer and
    # no redial is pending → email the rebooking link. Once per candidate —
    # booking through that link un-archives them via the public reschedule
    # portal, same as booking on the call.
    if (
        not dnd_scheduled
        and outcome in ("voicemail", "no_answer")
        and attempts == 1
        and rev.get("followup_email_enabled", True)
        and not cand.get("revival_followup_email_at")
        and cand.get("email")
    ):
        try:
            from deps import send_stage_email
            refreshed = await db.candidates.find_one({"id": cand_id, "user_id": user_id}, {"_id": 0}) or cand
            email_res = await send_stage_email(user_id=user_id, candidate=refreshed, template_key="revival_followup")
            if email_res.get("status") in ("sent", "queued"):
                await db.candidates.update_one(
                    {"id": cand_id, "user_id": user_id},
                    {"$set": {"revival_followup_email_at": now_utc().isoformat()}},
                )
                logger.info(f"revival follow-up email sent to {cand_id}")
            else:
                logger.info(f"revival follow-up email skipped for {cand_id}: {email_res.get('reason')}")
        except Exception as e:
            logger.warning(f"revival follow-up email failed for {cand_id}: {e}")

    from pubsub import broadcast
    await broadcast(user_id)
    logger.info(f"revival post-call for {cand_id}: outcome={outcome}, duration={duration}s")
    return {"ok": True, "call_type": "no_show_revival", "candidate_id": cand_id, "revival_outcome": outcome}
