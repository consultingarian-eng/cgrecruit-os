"""Appointment reminders: built-in 1h/10m + confirm-chaser + user-defined custom reminders.
Also handles the 2-hour form-completion reminder."""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from . import state
from .state import now_utc, settings_for
from app_tz import app_zone, default_tz_name  # APP_TIMEZONE (noqa: F401)

logger = logging.getLogger("auto_dialer")

# (job_key,                  template_key,                  channel, default_lead_minutes)
# Default lead minutes are used only if the template doesn't override
# `lead_minutes_before_appointment` in Settings → Applicant Comms.
REMINDER_KINDS: Tuple[Tuple[str, str, str, int], ...] = (
    ("appt-reminder-1h-email",   "appointment_reminder_1h",     "email", 60),
    ("appt-reminder-1h-sms",     "appointment_reminder_1h",     "sms",   60),
    ("appt-reminder-10m-email",  "appointment_reminder_10m",    "email", 10),
    ("appt-reminder-10m-sms",    "appointment_reminder_10m",    "sms",   10),
    # Confirm-chaser: only fires for candidates who have NOT replied Y yet
    # (skip-if-confirmed check lives in send_appointment_reminder). For morning
    # appointments the 3h lead would land before 8 AM — quiet-hours shifting
    # moves it to 7 PM the evening before instead.
    ("appt-confirm-chaser-email", "appointment_confirm_chaser", "email", 180),
    ("appt-confirm-chaser-sms",   "appointment_confirm_chaser", "sms",   180),
)

# Quiet hours: never fire a chaser/missed reminder between 9 PM and 8 AM local.
QUIET_START_HOUR = 21
QUIET_END_HOUR = 8
EVENING_SEND_HOUR = 19  # where morning-appointment chasers get shifted to


def _tenant_tz(settings: Dict[str, Any]) -> ZoneInfo:
    tz_name = ((settings or {}).get("region_language") or {}).get("timezone") or default_tz_name()
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return app_zone()


def _shift_chaser_out_of_quiet_hours(run_at: datetime, tz: ZoneInfo) -> datetime:
    """Morning appointments push the chaser into pre-8AM territory — shift it to
    7 PM the previous evening (the best confirmation window anyway). A chaser
    landing after 9 PM shifts back to 7 PM the same day."""
    local = run_at.astimezone(tz)
    if local.hour < QUIET_END_HOUR:
        shifted = (local - timedelta(days=1)).replace(hour=EVENING_SEND_HOUR, minute=0, second=0, microsecond=0)
        return shifted.astimezone(run_at.tzinfo)
    if local.hour >= QUIET_START_HOUR:
        shifted = local.replace(hour=EVENING_SEND_HOUR, minute=0, second=0, microsecond=0)
        return shifted.astimezone(run_at.tzinfo)
    return run_at


def _clamp_to_waking_hours(dt: datetime, tz: ZoneInfo) -> datetime:
    """Push a send time forward out of quiet hours: pre-8AM → 8 AM same day,
    post-9PM → 8 AM next day. Used for immediate fires of missed reminders so
    a 2 AM online booking doesn't trigger a 2 AM text."""
    local = dt.astimezone(tz)
    if local.hour < QUIET_END_HOUR:
        clamped = local.replace(hour=QUIET_END_HOUR, minute=0, second=0, microsecond=0)
        return clamped.astimezone(dt.tzinfo)
    if local.hour >= QUIET_START_HOUR:
        clamped = (local + timedelta(days=1)).replace(hour=QUIET_END_HOUR, minute=0, second=0, microsecond=0)
        return clamped.astimezone(dt.tzinfo)
    return dt


def _reminder_job_id(kind: str, candidate_id: str) -> str:
    """APScheduler job-id for a single reminder kind on a candidate. Stable across
    re-schedules — replace_existing on add_job will overwrite older versions."""
    return f"{kind}:{candidate_id}"


async def send_appointment_reminder(user_id: str, candidate_id: str, template_key: str, channel: str) -> Dict[str, Any]:
    """Fire one reminder (email or SMS). No-ops if the candidate or appointment
    is missing/changed/cancelled, so a stale job is safe even if we fail to
    cancel it on a reschedule."""
    if state._db is None:
        return {"status": "failed", "error": "db not initialized"}
    db = state._db
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user_id}, {"_id": 0})
    if not cand:
        return {"status": "skipped", "reason": "candidate not found"}
    if not cand.get("appointment_at"):
        return {"status": "skipped", "reason": "no appointment_at"}
    # A reminder firing far past its place in the timeline reads as chaos, not
    # care: after a deploy-restart re-arm, "your interview is in 1 hour" went
    # out at 9:11 for a 9:15 interview — four minutes AFTER the 10-minute
    # reminder. If most of the reminder's lead has already elapsed, the nearer
    # reminder owns this window; drop this one.
    from datetime import datetime, timezone
    from models import parse_appointment_at
    _appt = parse_appointment_at(cand.get("appointment_at"))
    if _appt:
        _remaining_min = (_appt - datetime.now(timezone.utc)).total_seconds() / 60
        _lead = {"appointment_reminder_1h": 60, "appointment_reminder_10m": 10}.get(template_key)
        if _remaining_min < -1:
            return {"status": "skipped", "reason": "appointment already started"}
        if _lead and _remaining_min < _lead / 3:
            return {"status": "skipped",
                    "reason": f"stale: {int(_remaining_min)} min left on a {_lead}-min-lead reminder"}
    # Defensive: even if the cancel-on-stage-move logic is bypassed somehow
    # (webhook update, /retry portal, direct DB write, etc.), don't ping a
    # candidate who's no longer in the APPOINTMENT funnel. The user's mental
    # model is "moved out of the Appointment column → no more reminders".
    if cand.get("stage") != "APPOINTMENT":
        return {"status": "skipped", "reason": f"stage={cand.get('stage')} (past APPOINTMENT — no reminders)"}
    # If candidate has already been marked attended / no-show'd we don't need
    # the reminder anymore.
    if cand.get("attendance_status") in ("attended_form", "attended_no_form", "no_show"):
        return {"status": "skipped", "reason": f"attendance already {cand.get('attendance_status')}"}
    # They have told us they can't attend this slot. Job cancellation already
    # covers this, but a surviving job must never text "your interview is in 1
    # hour" at someone who apologised for it yesterday.
    if cand.get("appointment_cancelled_at"):
        return {"status": "skipped", "reason": "candidate said they can't attend this slot"}
    # The confirm-chaser only targets candidates who haven't replied Y yet —
    # anyone already confirmed never sees it.
    if template_key == "appointment_confirm_chaser" and cand.get("appointment_sms_confirmed"):
        return {"status": "skipped", "reason": "already SMS-confirmed — chaser not needed"}
    # Interviewer + link render LIVE from the calendar at send time, stamped
    # values as fallback — a morning-of interviewer/room swap is made on the
    # calendar, and the reminder is the message a candidate re-reads on their
    # way in. In-memory overlay only; the stamp on the record is untouched.
    # NAME only — links stay stamped so the reminder can never carry a
    # different link from the confirmation email already in their inbox.
    try:
        from availability_service import resolve_slot_recruiter
        _pipe = await db.pipelines.find_one({"id": cand.get("pipeline_id")}, {"_id": 0}) or {}
        _settings_tz = await settings_for(user_id, cand.get("pipeline_id"))
        _tz = (_settings_tz.get("region_language") or {}).get("timezone") or default_tz_name()
        _live_rec = resolve_slot_recruiter(_pipe, cand["appointment_at"], _tz)
        if _live_rec:
            cand = {**cand, "appointment_recruiter": _live_rec}
    except Exception as e:
        logger.warning(f"live slot detail overlay failed for {candidate_id}: {e}")
    if channel == "email":
        if not cand.get("email"):
            return {"status": "skipped", "reason": "no email"}
        from deps import send_stage_email
        try:
            res = await send_stage_email(user_id, cand, template_key)
            logger.info(f"appt-reminder email {template_key} → {cand.get('email')}: {res.get('status')}")
            return res
        except Exception as e:
            logger.warning(f"appt-reminder email failed: {e}")
            return {"status": "failed", "error": str(e)}
    else:  # sms
        if not cand.get("phone"):
            return {"status": "skipped", "reason": "no phone"}
        try:
            from sms_service import send_candidate_sms
            from email_service import build_template_vars, render_template, get_template_for_key
            settings = await settings_for(user_id, cand.get("pipeline_id"))
            job = None
            if cand.get("job_id"):
                job = await db.jobs.find_one({"id": cand["job_id"]}, {"_id": 0})
            tpl = get_template_for_key(settings, template_key) or {}
            if tpl.get("sms_enabled") is False or tpl.get("enabled") is False:
                return {"status": "skipped", "reason": f"{template_key} SMS channel is off"}
            sms_body_tpl = tpl.get("sms_body") or ""
            if not sms_body_tpl:
                return {"status": "skipped", "reason": f"no sms_body for {template_key}"}
            body = render_template(sms_body_tpl, build_template_vars(cand, settings, job))
            res = await send_candidate_sms(db, settings, cand, body, template_key=template_key)
            logger.info(f"appt-reminder sms {template_key} → {cand.get('phone')}: {res.get('status')}")
            return res
        except Exception as e:
            logger.warning(f"appt-reminder sms failed: {e}")
            return {"status": "failed", "error": str(e)}


def cancel_appointment_reminders(candidate_id: str) -> int:
    """Remove every pending reminder job for this candidate. Returns count removed.
    Walks APScheduler's job list so it cleans up BOTH the built-in reminder kinds
    AND any user-defined custom reminders ('appt-reminder-custom-*') without
    needing to know which templates the recruiter has configured. Safe to call
    when no jobs exist.

    Prefixes are derived from REMINDER_KINDS rather than hardcoded: matching only
    'appt-reminder-' silently spared every 'appt-confirm-chaser-*' job, so a
    candidate who cancelled or was rescheduled still got 'just checking — are you
    still good for your interview?' about the slot they'd already given up."""
    if state._scheduler is None:
        return 0
    removed = 0
    suffix = f":{candidate_id}"
    prefixes = tuple({kind.rsplit("-", 1)[0] + "-" for kind, _t, _c, _l in REMINDER_KINDS} | {"appt-reminder-"})
    try:
        for job in list(state._scheduler.get_jobs()):
            jid = getattr(job, "id", "") or ""
            if not jid.endswith(suffix):
                continue
            if jid.startswith(prefixes):
                try:
                    state._scheduler.remove_job(jid)
                    removed += 1
                except Exception:
                    pass  # already gone
    except Exception:
        pass
    return removed


# ─── Form completion reminder (single, 2h after moving to FORM) ──────────────

FORM_REMINDER_DELAY_MINUTES = 120


def _form_reminder_job_id(candidate_id: str) -> str:
    return f"form-reminder:{candidate_id}"


async def send_form_reminder(user_id: str, candidate_id: str) -> Dict[str, Any]:
    """Fire the 2-hour form-completion nudge. No-op if the candidate has already
    submitted their form or is no longer in FORM stage."""
    if state._db is None:
        return {"status": "failed", "error": "db not initialized"}
    db = state._db
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user_id}, {"_id": 0})
    if not cand:
        return {"status": "skipped", "reason": "candidate not found"}
    if cand.get("stage") != "FORM":
        return {"status": "skipped", "reason": f"stage={cand.get('stage')} (not FORM)"}
    if cand.get("form_submitted_at") or cand.get("form_responses"):
        return {"status": "skipped", "reason": "form already submitted"}
    try:
        from deps import send_stage_comms
        res = await send_stage_comms(user_id, cand, "form_reminder")
        await db.candidates.update_one({"id": candidate_id}, {"$set": {"form_reminder_at": None}})
        logger.info(f"form-reminder sent for {candidate_id}: email={res.get('email', {}).get('status')} sms={res.get('sms', {}).get('status')}")
        return res
    except Exception as e:
        logger.warning(f"form-reminder failed for {candidate_id}: {e}")
        return {"status": "failed", "error": str(e)}


def cancel_form_reminder(candidate_id: str) -> int:
    """Remove the pending form-reminder job for this candidate. Returns 1 if removed, 0 if not found."""
    if state._scheduler is None:
        return 0
    job_id = _form_reminder_job_id(candidate_id)
    removed = 0
    try:
        state._scheduler.remove_job(job_id)
        removed = 1
    except Exception:
        pass
    if state._db is not None:
        import asyncio as _asyncio
        try:
            _asyncio.create_task(state._db.candidates.update_one(
                {"id": candidate_id},
                {"$set": {"form_reminder_at": None}},
            ))
        except Exception:
            pass
    return removed


async def schedule_form_reminder(
    user_id: str, candidate_id: str, delay_minutes: int = FORM_REMINDER_DELAY_MINUTES,
) -> Dict[str, Any]:
    """Schedule a single form-completion reminder (2 hours from now by default;
    the startup re-arm passes a short delay for jobs a deploy orphaned).
    Replaces any existing job for this candidate."""
    if state._scheduler is None:
        return {"status": "failed", "error": "scheduler not started"}
    run_at = now_utc() + timedelta(minutes=delay_minutes)
    job_id = _form_reminder_job_id(candidate_id)
    state._scheduler.add_job(
        send_form_reminder,
        "date",
        run_date=run_at,
        args=[user_id, candidate_id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=600,
    )
    if state._db is not None:
        from .state import now_utc as _now_utc
        import asyncio as _asyncio
        try:
            _asyncio.create_task(state._db.candidates.update_one(
                {"id": candidate_id},
                {"$set": {"form_reminder_at": run_at.isoformat()}},
            ))
        except Exception:
            pass
    logger.info(f"form-reminder scheduled for {candidate_id} at {run_at.isoformat()}")
    return {"status": "scheduled", "run_at": run_at.isoformat()}


async def schedule_appointment_reminders(user_id: str, candidate_id: str, appointment_at: str) -> Dict[str, Any]:
    """Schedule the appointment reminder emails + SMS. Each reminder's lead-time
    is read from the per-template `lead_minutes_before_appointment` (Settings →
    Applicant Comms → reminder template). Falls back to the hardcoded defaults
    (60 / 10 / 10 minutes) when the template doesn't override.

    Replaces existing jobs for the same candidate. If a reminder time is already
    in the past (e.g. booking made <10min before the slot), that one is silently
    skipped. Disabled-templates are also skipped."""
    if state._scheduler is None or state._db is None:
        return {"status": "failed", "error": "scheduler not started"}
    db = state._db
    try:
        appt = datetime.fromisoformat(appointment_at.replace("Z", "+00:00"))
    except Exception as e:
        return {"status": "failed", "error": f"bad appointment_at: {e}"}
    if appt.tzinfo is None:
        # Bookings arrive from six code paths and the field is a naive ISO
        # string in some of them. A naive value used to blow up the aware
        # comparison below — the exception was swallowed by every caller, so
        # the candidate silently got NO reminders at all. Treat naive as UTC:
        # possibly hours off, but a reminder that fires beats one that never
        # schedules, and the stale-fire guard drops any that land out of order.
        appt = appt.replace(tzinfo=timezone.utc)

    # Pull template config once (per-pipeline-resolved) so we can read enable + lead-time
    # for each reminder template.
    cand_for_tz = await db.candidates.find_one({"id": candidate_id, "user_id": user_id}, {"_id": 0}) or {}
    settings = await settings_for(user_id, cand_for_tz.get("pipeline_id"))
    templates = (settings.get("applicant_comms") or {}).get("templates") or {}

    # User-defined extra reminders. Each carries its own lead-time and label.
    # We schedule both an email and (if sms_body present) an SMS for each one.
    custom_kinds: List[Tuple[str, str, str, int]] = []
    for tpl_key, tpl_def in templates.items():
        if not (tpl_def or {}).get("is_custom_reminder"):
            continue
        try:
            lead = int((tpl_def or {}).get("lead_minutes_before_appointment") or 0)
        except (TypeError, ValueError):
            lead = 0
        if lead <= 0:
            continue
        custom_kinds.append((f"appt-reminder-custom-{tpl_key}-email", tpl_key, "email", lead))
        if (tpl_def or {}).get("sms_body"):
            custom_kinds.append((f"appt-reminder-custom-{tpl_key}-sms", tpl_key, "sms", lead))

    now = now_utc()
    tz = _tenant_tz(settings)
    scheduled: list = []
    skipped: list = []
    # Missed reminders (fire-time already passed but appointment still upcoming),
    # tracked per channel so we can rescue candidates who'd otherwise get nothing.
    missed: Dict[str, List[Dict[str, Any]]] = {"email": [], "sms": []}
    future_per_channel: Dict[str, int] = {"email": 0, "sms": 0}

    for kind, template_key, channel, default_lead_min in list(REMINDER_KINDS) + custom_kinds:
        tpl = templates.get(template_key) or {}
        if tpl.get("enabled") is False:
            skipped.append({"kind": kind, "reason": "template disabled"})
            continue
        # Per-channel toggle: if email_enabled=False, skip the email reminder
        # but still schedule the SMS reminder (and vice-versa).
        if channel == "email" and tpl.get("email_enabled") is False:
            skipped.append({"kind": kind, "reason": "email channel off"})
            continue
        if channel == "sms" and tpl.get("sms_enabled") is False:
            skipped.append({"kind": kind, "reason": "sms channel off"})
            continue
        # Per-template override → fall back to default if unset/zero.
        try:
            override = tpl.get("lead_minutes_before_appointment")
            lead_min = int(override) if override is not None and int(override) > 0 else default_lead_min
        except (TypeError, ValueError):
            lead_min = default_lead_min
        run_at = appt - timedelta(minutes=lead_min)
        if template_key == "appointment_confirm_chaser":
            run_at = _shift_chaser_out_of_quiet_hours(run_at, tz)
            if run_at >= appt:  # shifting landed past the appointment — drop
                skipped.append({"kind": kind, "reason": "chaser shift landed after appointment"})
                continue
        if run_at <= now + timedelta(seconds=5):
            missed[channel].append({
                "kind": kind, "template_key": template_key,
                "lead_minutes": lead_min, "run_at": run_at,
            })
            skipped.append({"kind": kind, "reason": f"lead time {lead_min}m already passed"})
            continue
        job_id = _reminder_job_id(kind, candidate_id)
        state._scheduler.add_job(
            send_appointment_reminder,
            "date",
            run_date=run_at,
            args=[user_id, candidate_id, template_key, channel],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=600,
        )
        future_per_channel[channel] += 1
        scheduled.append({"kind": kind, "run_at": run_at.isoformat(), "lead_minutes": lead_min})
        logger.info(f"reminder {kind} scheduled for {candidate_id} at {run_at.isoformat()} (lead={lead_min}m)")

    # ── Fire-instead-of-skip rescue ───────────────────────────────────────────
    # If a channel has NO upcoming reminder (short-lead booking, or a deploy
    # window swallowed the in-memory job), fire the closest missed one now so
    # the candidate isn't left with zero reminders. The communications log is
    # the dedup source of truth — a reminder that actually sent before a
    # restart is never re-sent.
    for channel, entries in missed.items():
        if not entries or future_per_channel[channel] > 0 or appt <= now:
            continue
        # Smallest lead = content closest to "your interview is now-ish".
        entries.sort(key=lambda e: e["lead_minutes"])
        for entry in entries:
            already = await db.communications.find_one({
                "candidate_id": candidate_id,
                "template_key": entry["template_key"],
                "type": channel,
                "created_at": {"$gte": (entry["run_at"] - timedelta(minutes=30)).isoformat()},
            }, {"_id": 1})
            if already:
                continue  # genuinely sent before — nothing missed
            fire_at = _clamp_to_waking_hours(now + timedelta(seconds=60), tz)
            if fire_at >= appt:
                break  # quiet-hours clamp pushed past the appointment — drop
            job_id = _reminder_job_id(entry["kind"], candidate_id)
            state._scheduler.add_job(
                send_appointment_reminder,
                "date",
                run_date=fire_at,
                args=[user_id, candidate_id, entry["template_key"], channel],
                id=job_id,
                replace_existing=True,
                misfire_grace_time=600,
            )
            scheduled.append({"kind": entry["kind"], "run_at": fire_at.isoformat(),
                              "lead_minutes": entry["lead_minutes"], "rescued": True})
            logger.info(f"missed reminder {entry['kind']} rescued for {candidate_id} — firing at {fire_at.isoformat()}")
            break  # one rescue per channel — never stack stale reminders

    return {"status": "scheduled", "scheduled": scheduled, "skipped": skipped}
