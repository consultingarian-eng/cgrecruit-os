"""Attendance & reschedule routes.

After an interview, the recruiter marks the candidate as one of:
- attended_form     → auto-advance to FORM stage, fire form email + SMS
- attended_no_form  → auto-advance to FORM stage, fire form email + SMS
- no_show           → keep in APPOINTMENT, fire reschedule email + SMS,
                     candidate uses the public /reschedule/{token} portal.

Also hosts the post-form Hire / Decline + the rebook-watchlist endpoints.
"""
import os
import company_profile
import asyncio
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, HTTPException, Body, Depends

from deps import db, logger, current_user, resolve_settings, send_stage_email, require_mover, candidate_write_access
from models import ATTENDANCE_VALUES, now_iso
from sms_service import send_candidate_sms
from email_service import build_template_vars, render_template, get_template_for_key
from app_tz import app_zone, default_tz_name  # APP_TIMEZONE (noqa: F401)

router = APIRouter()


# Canonical list lives in models so /move can validate against the same one —
# it now accepts an outcome too, and two copies of this tuple would eventually
# disagree about what a valid outcome is.
VALID_ATTENDANCE = ATTENDANCE_VALUES


@router.post("/candidates/{candidate_id}/send-slot-picker", dependencies=[Depends(candidate_write_access)])
async def send_slot_picker(candidate_id: str, user: dict = Depends(current_user)):
    """Manually resend the slot-picker link to a candidate (email + SMS)."""
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")
    result = await _fire_slot_picker_outreach(user["id"], cand)
    await db.candidates.update_one(
        {"id": candidate_id, "user_id": user["id"]},
        {"$set": {"slot_picker_sent_at": now_iso()}},
    )
    return {"ok": True, "result": result}


@router.post("/candidates/{candidate_id}/send-screening-link", dependencies=[Depends(candidate_write_access)])
async def send_screening_link(candidate_id: str, user: dict = Depends(current_user)):
    """Manually resend the finish-your-screening link (email + SMS).

    For candidates whose screening is UNFINISHED. The drawer used to offer
    only the slot-picker here — labelled "Screening passed" over an
    INCOMPLETE badge — which both lied about their state and invited an
    unscreened booking through the very back door the outcome hook exists
    to catch."""
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")
    from deps import send_stage_comms
    result = await send_stage_comms(user["id"], cand, "retry_chat_nudge")
    return {"ok": True, "result": result}


@router.post("/candidates/{candidate_id}/attendance", dependencies=[Depends(candidate_write_access)])
async def set_attendance(
    candidate_id: str,
    payload: Dict[str, Any] = Body(...),
    user: dict = Depends(current_user),
):
    """Recruiter records the outcome of an interview appointment.

    Body: { status: "attended_form" | "attended_no_form" | "no_show" }
    """
    require_mover(user)
    status = (payload.get("status") or "").strip()
    if status not in VALID_ATTENDANCE:
        raise HTTPException(400, f"status must be one of {VALID_ATTENDANCE}")
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")

    # Idempotency: if attendance is already set to the same value, skip comms
    # so a double-click or duplicate request doesn't re-send emails/SMS.
    already_set = cand.get("attendance_status") == status

    update: Dict[str, Any] = {
        "attendance_status": status,
        "updated_at": now_iso(),
        # Who answered and when. Without this there is no way to tell a session
        # that was marked up promptly from one reconstructed from memory a week
        # later, which matters while we are trying to establish whether the
        # office-to-office attendance gap is real or a recording artefact.
        "attendance_recorded_at": now_iso(),
        "attendance_recorded_by": user.get("id"),
    }
    # attended_form auto-advances to FORM and triggers the form email/SMS.
    # attended_no_form is tracking-only — candidate attended but we are not
    # progressing their application (didn't qualify, withdrew, etc.). No stage
    # change, no comms, no form reminder.
    # no_show stays in APPOINTMENT for rescheduling.
    if status == "attended_form":
        update["stage"] = "FORM"
        update["rescheduled"] = False
    elif status == "no_show":
        try:
            from auto_dialer import cancel_appointment_reminders
            cancel_appointment_reminders(candidate_id)
        except Exception as e:
            logger.warning(f"cancel reminders on no_show failed: {e}")
        update["previous_appointment_at"] = cand.get("appointment_at")
        update["rescheduled"] = False
        update["no_show_at"] = now_iso()
    elif status == "attended_no_form":
        update["rescheduled"] = False
        # No stage change — stays in APPOINTMENT as a tracking record

    await db.candidates.update_one(
        {"id": candidate_id, "user_id": user["id"]},
        {"$set": update},
    )
    fresh = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})

    # Attendance move always takes the candidate out of the screening queue —
    # cancel any pending call or pre-call SMS so they don't receive the warmup.
    try:
        from auto_dialer import cancel_pending_retry_calls
        cancel_pending_retry_calls(candidate_id)
    except Exception as _e:
        logger.warning(f"cancel pending calls on attendance failed: {_e}")

    # Fire outreach based on attendance outcome.
    # Skip if status hasn't changed — guards against double-clicks / duplicate requests.
    notification: Optional[Dict[str, Any]] = None
    if already_set:
        notification = {"status": "skipped", "reason": "attendance already set to same value"}
    elif status == "attended_form":
        try:
            from deps import send_stage_comms
            notification = await send_stage_comms(user["id"], fresh, "form")
        except Exception as e:
            logger.warning(f"form comms failed for {candidate_id}: {e}")
            notification = {"email": {"status": "failed", "error": str(e)}}
        try:
            from auto_dialer import schedule_form_reminder
            await schedule_form_reminder(user["id"], candidate_id)
        except Exception as e:
            logger.warning(f"schedule_form_reminder failed for {candidate_id}: {e}")
    elif status == "no_show":
        # Two strikes. Today's 9:15 was six candidates, every one a recycled
        # prior no-show, zero SMS-confirmed — the rebook loop (no-show →
        # "pick a new time" → next morning's soonest slot → no-show again)
        # had no cap, so ghosts cycled through sessions indefinitely while
        # the interviewer sat in an empty room. One miss earns the rebook
        # invite; a second archives them quietly — no invite, no revival
        # (revival only targets 'no_show_not_rescheduled'). They can still
        # come back through a re-apply or by replying, but we stop spending
        # slots on them.
        prior_no_shows = sum(
            1 for h in (cand.get("attendance_history") or [])
            if h.get("status") == "no_show"
        )
        if prior_no_shows >= 1:
            await db.candidates.update_one(
                {"id": candidate_id, "user_id": user["id"]},
                {"$set": {"archived_at": now_iso(), "archived_reason": "repeat_no_show",
                          "auto_dial": False, "next_call_at": None, "updated_at": now_iso()}},
            )
            logger.info(f"{candidate_id} archived after no-show #{prior_no_shows + 1} — no rebook invite")
            notification = {"status": "skipped",
                            "reason": f"no-show #{prior_no_shows + 1} — archived, rebook invite withheld"}
        else:
            notification = await _fire_no_show_outreach(user["id"], fresh)

    return {"ok": True, "candidate": fresh, "notification": notification}


async def _fire_slot_picker_outreach(
    user_id: str, cand: Dict[str, Any], template_key: str = "slot_picker",
) -> Dict[str, Any]:
    """Send a 'pick your interview slot' email + SMS pointing at the existing
    /reschedule/{public_token} page (slot grid + 3-day watchlist).

    `template_key` selects the framing. The default congratulates them on
    passing screening, which is right for someone who never got as far as a
    slot — and wrong for someone who booked one and then told us they couldn't
    make it, hence `appointment_cancelled_rebook`."""
    res: Dict[str, Any] = {"email": None, "sms": None}
    try:
        res["email"] = await send_stage_email(user_id, cand, template_key)
    except Exception as e:
        logger.warning(f"{template_key} email failed for {cand.get('id')}: {e}")
        res["email"] = {"status": "failed", "error": str(e)}
    if cand.get("phone"):
        try:
            settings = await resolve_settings(user_id, cand.get("pipeline_id"))
            public_token = cand.get("public_token", "")
            from company_profile import public_app_url
            base_url = public_app_url()
            reschedule_url = f"{base_url}/reschedule/{public_token}" if (public_token and base_url) else ""
            tpl = get_template_for_key(settings, template_key) or {}
            sms_body_tpl = tpl.get("sms_body") or ""
            if sms_body_tpl:
                from email_service import build_template_vars, render_template
                job = None
                if cand.get("job_id"):
                    job = await db.jobs.find_one({"id": cand["job_id"]}, {"_id": 0})
                extra_vars = {"reschedule_url": reschedule_url}
                sms_body = render_template(sms_body_tpl, {**build_template_vars(cand, settings, job), **extra_vars})
            elif reschedule_url:
                first = cand.get("first_name", "")
                if template_key == "appointment_cancelled_rebook":
                    sms_body = (
                        f"Hi {first}, no problem about the interview — thanks for letting us know. "
                        f"Whenever you're ready, pick a new time here: {reschedule_url}"
                    )
                else:
                    sms_body = (
                        f"Hi {first}! Great chat — just need to lock in your interview time. "
                        f"Pick a slot here: {reschedule_url} "
                        f"(If none suit you, tap 'Notify me in 3 days' for a fresh set.)"
                    )
            else:
                sms_body = ""
            if sms_body:
                res["sms"] = await send_candidate_sms(db, settings, cand, sms_body, template_key=template_key)
        except Exception as e:
            logger.warning(f"{template_key} sms failed for {cand.get('id')}: {e}")
            res["sms"] = {"status": "failed", "error": str(e)}
    return res


async def _fire_no_show_outreach(user_id: str, cand: Dict[str, Any]) -> Dict[str, Any]:
    """Send the no-show reschedule email + SMS. Best-effort; failures are logged."""
    res: Dict[str, Any] = {"email": None, "sms": None}
    try:
        res["email"] = await send_stage_email(user_id, cand, "appointment_no_show")
    except Exception as e:
        logger.warning(f"no-show email failed for {cand.get('id')}: {e}")
        res["email"] = {"status": "failed", "error": str(e)}
    if cand.get("phone"):
        try:
            settings = await resolve_settings(user_id, cand.get("pipeline_id"))
            job = None
            if cand.get("job_id"):
                job = await db.jobs.find_one({"id": cand["job_id"]}, {"_id": 0})
            tpl = get_template_for_key(settings, "appointment_no_show") or {}
            sms_disabled = tpl.get("sms_enabled") is False or tpl.get("enabled") is False
            sms_body_tpl = tpl.get("sms_body") or ""
            if sms_disabled:
                res["sms"] = {"status": "skipped", "reason": "appointment_no_show SMS channel is off"}
            elif sms_body_tpl:
                body = render_template(sms_body_tpl, build_template_vars(cand, settings, job))
                res["sms"] = await send_candidate_sms(db, settings, cand, body, template_key="appointment_no_show")
        except Exception as e:
            logger.warning(f"no-show sms failed: {e}")
            res["sms"] = {"status": "failed", "error": str(e)}
    return res


# ===== Public reschedule portal =====

@router.get("/public/reschedule/{token}")
async def public_reschedule_view(token: str):
    """Candidate opens the reschedule portal — returns candidate context + 4-day slot list."""
    cand = await db.candidates.find_one({"public_token": token}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Application not found")
    if cand.get("attendance_status") not in (None, "no_show") and cand.get("screening_status") != "appointment_pending":
        # Already attended — no rescheduling needed.
        raise HTTPException(409, "This appointment has already been completed.")
    pipeline = await db.pipelines.find_one({"id": cand["pipeline_id"]}, {"_id": 0}) or {}
    settings = await resolve_settings(cand["user_id"], cand.get("pipeline_id"))
    profile = (settings.get("recruiter_profile") or {})

    # Build the slot window starting from "now" — window size from booking prefs.
    from availability_service import compute_available_slots
    booking_prefs = settings.get("booking_preferences") or {}
    days_ahead = int(booking_prefs.get("reschedule_link_days_ahead") or 4)
    booked_docs = await db.candidates.find(
        {"pipeline_id": pipeline.get("id"), "appointment_at": {"$ne": None}},
        {"_id": 0, "appointment_at": 1},
    ).to_list(500)
    booked_isos = [b.get("appointment_at") for b in booked_docs if b.get("appointment_at")]
    appt_settings = settings.get("appointments") or {}
    default_cap = int(appt_settings.get("applicant_limit") or 50)
    tz_name = pipeline.get("timezone") or (settings.get("region_language") or {}).get("timezone") or default_tz_name()
    slots = compute_available_slots(
        pipeline,
        booked_isos,
        days_ahead=days_ahead,
        now=datetime.now(timezone.utc),
        tz_name=tz_name,
        default_capacity=default_cap,
    )

    return {
        "candidate": {
            "id": cand["id"],
            "first_name": cand.get("first_name"),
            "last_name": cand.get("last_name"),
            "email": cand.get("email"),
            "phone": cand.get("phone"),
            "previous_appointment_at": cand.get("previous_appointment_at") or cand.get("appointment_at"),
            "attendance_status": cand.get("attendance_status"),
            "appointment_link": cand.get("appointment_link") or pipeline.get("appointment_link") or "",
        },
        "company": {
            "name": profile.get("company_name") or company_profile.company_name(),
            "city": profile.get("city", ""),
        },
        # Next six openings only — same rule as the portal picker: sooner
        # books better, and two weeks of options invites deferral.
        "slots": slots[:6],
        "timezone": tz_name,
    }


@router.post("/public/reschedule/{token}/book")
async def public_reschedule_book(token: str, payload: Dict[str, Any] = Body(...)):
    """Candidate picks a new slot through the no-show reschedule portal."""
    cand = await db.candidates.find_one({"public_token": token}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Application not found")
    new_at = (payload.get("appointment_at") or "").strip()
    if not new_at:
        raise HTTPException(400, "appointment_at required")

    # Capacity check on the new slot.
    pipeline = await db.pipelines.find_one({"id": cand["pipeline_id"]}, {"_id": 0}) or {}
    settings = await resolve_settings(cand["user_id"], cand.get("pipeline_id"))
    from availability_service import slot_capacity_remaining, resolve_slot_recruiter
    from deps import booking_link_or_alert
    appt_settings = settings.get("appointments") or {}
    default_cap = int(appt_settings.get("applicant_limit") or 50)
    other_booked = await db.candidates.find(
        {"pipeline_id": pipeline.get("id"), "appointment_at": new_at, "id": {"$ne": cand["id"]}},
        {"_id": 0, "appointment_at": 1},
    ).to_list(200)
    # tz_name is required here: rules are authored in the recruiter timezone, and
    # slot_capacity_remaining matches on exact local HH:MM. Without it the check
    # ran in UTC and rejected every valid ET slot with a 409.
    _tz = (settings.get("region_language") or {}).get("timezone") or default_tz_name()
    remaining = slot_capacity_remaining(
        pipeline,
        [b.get("appointment_at") for b in other_booked],
        new_at,
        default_capacity=default_cap,
        tz_name=_tz,
    )
    if remaining == -1:
        raise HTTPException(409, "That time is no longer available — please pick another slot.")
    if remaining is not None and remaining <= 0:
        raise HTTPException(409, "That slot is full — please pick another.")

    # Window enforcement: refuse slots past the configured days_ahead limit.
    booking_prefs = settings.get("booking_preferences") or {}
    days_limit = int(booking_prefs.get("reschedule_link_days_ahead") or 4)
    try:
        new_dt = datetime.fromisoformat(new_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if new_dt < now:
            raise HTTPException(400, "Cannot book a slot in the past.")
        if new_dt > now + timedelta(days=days_limit, hours=12):  # 12h grace for tz edge
            raise HTTPException(400, f"You can only book within the next {days_limit} days.")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Invalid appointment_at format.")

    prev_at = cand.get("appointment_at")
    was_archived = bool(cand.get("archived_at"))
    update = {
        "stage": "APPOINTMENT",
        "appointment_at": new_at,
        "attendance_status": None,
        "appointment_sms_confirmed": None,      # reset so chaser fires for the new slot
        "appointment_sms_confirmed_at": None,
        # A candidate who texted "can't make it" and then picked a new time must
        # get the NEW slot's reminders. The reminder engine skips every send
        # while appointment_cancelled_at is set, and only the SMS rebook path
        # cleared it — rebooking through this door left the reminders muted and
        # the 48h sweep re-archiving attendees as "cancelled".
        "appointment_cancelled_at": None,
        "appointment_cancel_reason": None,
        "previous_appointment_at": prev_at,
        "rescheduled": True,
        # Slot override first, then the pipeline default. Never the candidate's
        # stored link — that belongs to the OLD slot and would follow them onto a
        # slot with no override (put one office's candidates in another's room).
        # Empty when unconfigured — never a placeholder URL.
        "appointment_link": await booking_link_or_alert(pipeline, new_at, _tz, cand),
        # Slot's interviewer first, for the same reason as the link — the stored
        # name belongs to the OLD slot.
        "appointment_recruiter": resolve_slot_recruiter(pipeline, new_at, _tz) or pipeline.get("appointment_recruiter") or "Hiring Team",
        "updated_at": now_iso(),
    }
    # "approved" only when a screening actually stands behind this candidate —
    # the unconditional stamp painted unscreened self-serve bookers as
    # screening-passed, muddying who in the room was ever gated.
    if (cand.get("verdict") or "") not in ("", "incomplete") or cand.get("call_summary"):
        update["screening_status"] = "approved"
    if was_archived:
        update["archived_at"] = None
        update["archived_reason"] = None
        logger.info(f"re-engagement: unarchived {cand['id']} via no-show reschedule portal")
    chat = list(cand.get("chat_log") or [])
    chat.append({
        "role": "applicant",
        "channel": "reschedule",
        "text": f"Rescheduled from {prev_at or 'unset'} to {new_at}.",
        "at": now_iso(),
    })
    update["chat_log"] = chat

    # Keep the outcome being cleared. Rescheduling rightly starts the new
    # appointment clean, but destroying the old answer hid real no-shows.
    from screening_outcome import preserve_attendance
    _write = {"$set": update}
    _keep = preserve_attendance(cand)
    if _keep:
        _write["$push"] = _keep
    await db.candidates.update_one({"id": cand["id"]}, _write)
    fresh = await db.candidates.find_one({"id": cand["id"]}, {"_id": 0})

    # Template by history. This page is two doors in one: the reschedule link
    # for people who had a slot, and the slot-picker for screened candidates
    # booking their FIRST slot — who used to be told "your interview has been
    # rescheduled" about a booking that never existed. First bookings get the
    # booking confirmation, verdict-aware like the portal door (never "your
    # screening was successful" to someone never screened).
    if prev_at:
        _tpl_key = "appointment_rescheduled"
        _tpl_def = get_template_for_key(settings, _tpl_key) or {}
        if not _tpl_def.get("body") or _tpl_def.get("enabled") is False:
            _tpl_key = "approval"
    else:
        _tpl_key = (
            "approval"
            if ((fresh.get("verdict") or "") not in ("", "incomplete") or fresh.get("call_summary"))
            else "approval_unscreened"
        )
    try:
        await send_stage_email(cand["user_id"], fresh, _tpl_key)
    except Exception as e:
        logger.warning(f"reschedule confirmation email failed: {e}")
    if fresh.get("phone"):
        try:
            tpl = get_template_for_key(settings, _tpl_key) or {}
            if tpl.get("sms_enabled") is False or tpl.get("enabled") is False:
                pass  # SMS channel off
            else:
                sms_body_tpl = tpl.get("sms_body") or ""
                if sms_body_tpl:
                    job = None
                    if fresh.get("job_id"):
                        job = await db.jobs.find_one({"id": fresh["job_id"]}, {"_id": 0})
                    body = render_template(sms_body_tpl, build_template_vars(fresh, settings, job))
                    await send_candidate_sms(db, settings, fresh, body, template_key=_tpl_key)
        except Exception as e:
            logger.warning(f"reschedule confirmation sms failed: {e}")
    try:
        from auto_dialer import cancel_appointment_reminders, schedule_appointment_reminders, cancel_pending_retry_calls
        cancel_appointment_reminders(cand["id"])
        await schedule_appointment_reminders(cand["user_id"], cand["id"], new_at)
        cancel_pending_retry_calls(cand["id"])
    except Exception as e:
        logger.warning(f"reschedule reminder scheduling failed: {e}")

    from screening_outcome import ensure_screening_outcome_later
    ensure_screening_outcome_later(db, cand["id"])

    return {
        "ok": True,
        "appointment_at": new_at,
        "appointment_link": fresh.get("appointment_link"),
    }



# ===== Post-form Hire / Decline =====

async def _fire_cg1_webhook(user_id: str, candidate: Dict[str, Any]) -> Dict[str, Any]:
    """POST the hired candidate to the configured CG1 outbound webhook.
    Returns {status, status_code, body, error?}. Non-fatal on failure — the
    hire still succeeds; we just record the webhook outcome on the candidate.

    CG1 integration config is tenant-wide (not per-pipeline), so this always
    reads from the global settings doc."""
    settings = await db.settings.find_one({"user_id": user_id, "pipeline_id": None}, {"_id": 0}) or {}
    integrations = settings.get("integrations") or {}
    if not integrations.get("cg1_enabled"):
        return {"status": "skipped", "reason": "cg1 webhook disabled"}
    url = (integrations.get("cg1_webhook_url") or "").strip()
    if not url:
        return {"status": "skipped", "reason": "no cg1_webhook_url configured"}
    secret = integrations.get("cg1_webhook_secret") or ""
    payload = {
        "candidate_id": candidate.get("id"),
        "first_name": candidate.get("first_name") or "",
        "last_name": candidate.get("last_name") or "",
        "email": candidate.get("email") or "",
        "phone": candidate.get("phone") or "",
        "appointment_at": candidate.get("appointment_at"),
        "hired_at": candidate.get("hired_at"),
        "company": (settings.get("recruiter_profile") or {}).get("company_name", ""),
    }
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Webhook-Secret"] = secret
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=payload, headers=headers)
        ok = r.status_code < 400
        return {
            "status": "sent" if ok else "failed",
            "status_code": r.status_code,
            "body": (r.text or "")[:500],
        }
    except Exception as e:
        logger.warning(f"cg1 webhook to {url} failed: {e}")
        return {"status": "failed", "error": str(e)}


@router.post("/candidates/{candidate_id}/hire", dependencies=[Depends(candidate_write_access)])
async def hire_candidate(
    candidate_id: str,
    payload: Dict[str, Any] = Body(default={}),
    user: dict = Depends(current_user),
):
    """Recruiter clicks Hire on the form-responses tab. Moves candidate to
    TRAINING, fires the training email/SMS (with start date/time), and pushes
    the new-starter payload to CG1. Idempotent — re-hiring re-fires the webhook
    but doesn't double-send the email.

    Body (all optional): { training_start_at: "<ISO 8601>" }
    """
    require_mover(user)
    from server import _fire_cg1_training_webhook  # avoid circular at module level
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")
    already_hired = bool(cand.get("hired"))
    training_start_at = (payload.get("training_start_at") or "").strip()

    _hired_at = cand.get("hired_at") or now_iso()
    update: Dict[str, Any] = {
        "stage": "TRAINING",
        "hired": True,
        "hired_at": _hired_at,
        "screening_status": "approved",
        "updated_at": now_iso(),
    }
    if not cand.get("moved_to_training_at"):
        update["moved_to_training_at"] = _hired_at
    if training_start_at:
        update["training_start_at"] = training_start_at

    await db.candidates.update_one(
        {"id": candidate_id, "user_id": user["id"]},
        {"$set": update},
    )
    fresh = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})

    # Cancel any pending screening call / pre-call SMS — candidate is hired.
    try:
        from auto_dialer import cancel_pending_retry_calls
        cancel_pending_retry_calls(candidate_id)
    except Exception as _e:
        logger.warning(f"cancel pending calls on hire failed: {_e}")

    # CG1 sends its own hiring confirmation from its own address, so we
    # skip the redundant CGRecruit email. close_success still fires if the
    # recruiter explicitly triggers it from the comms tab.
    email_res = {"status": "skipped", "reason": "CG1 sends training confirmation directly"}
    sms_res = None

    # Fire new-starter webhook to CG1 (creates the new-hire record with date/time).
    cg1_res = await _fire_cg1_training_webhook(
        user["id"], candidate_id, training_start_at=training_start_at or fresh.get("training_start_at") or ""
    )
    # Also fire the legacy integration webhook if configured (different endpoint).
    webhook_res = await _fire_cg1_webhook(user["id"], fresh)
    await db.candidates.update_one(
        {"id": candidate_id, "user_id": user["id"]},
        {"$set": {"cg1_webhook_status": "sent" if cg1_res else webhook_res.get("status")}},
    )
    fresh["cg1_webhook_status"] = "sent" if cg1_res else webhook_res.get("status")

    return {
        "ok": True,
        "candidate": fresh,
        "email": email_res,
        "sms": sms_res,
        "webhook": webhook_res,
        "cg1_new_starter_id": cg1_res,
    }


# ===== Reschedule watchlist (re-notify when slots open) =====

@router.post("/public/reschedule/{token}/watchlist")
async def add_to_rebook_watchlist(token: str):
    """Candidate clicks 'None of these work' on the reschedule portal. We add
    them to the watchlist; the cron-style sweep below emails them again 3
    days later with whatever slots are open then."""
    cand = await db.candidates.find_one({"public_token": token}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Application not found")
    await db.candidates.update_one(
        {"id": cand["id"]},
        {"$set": {
            "rebook_watchlist": True,
            "rebook_watchlist_at": now_iso(),
            "rebook_watchlist_notified_at": None,
            "updated_at": now_iso(),
        }},
    )
    return {"ok": True}
