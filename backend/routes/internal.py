"""Internal API endpoints — called by CG1 using the shared webhook secret.

These are NOT exposed to the public or to end-users.  All endpoints verify
x-webhook-secret before responding.
"""
import os
import company_profile
import re
from typing import Dict, Optional
from fastapi import APIRouter, Request, HTTPException, Body

from deps import db, logger
from models import new_id, now_iso
from app_tz import app_zone, default_tz_name  # APP_TIMEZONE (noqa: F401)

router = APIRouter()

WEBHOOK_SECRET = os.getenv("CGRECRUIT_WEBHOOK_SECRET", "")


def _verify(request: Request) -> None:
    # Fail closed: with no shared secret configured the internal API is off,
    # never open — these routes read and move candidates.
    if not WEBHOOK_SECRET:
        raise HTTPException(503, "Internal API disabled — set CGRECRUIT_WEBHOOK_SECRET")
    import hmac
    incoming = request.headers.get("x-webhook-secret", "") or ""
    if not hmac.compare_digest(incoming.encode("utf-8"), WEBHOOK_SECRET.encode("utf-8")):
        raise HTTPException(403, "Invalid secret")


def _name_matches(referred_by: str | None, query: str) -> bool:
    if not referred_by or not query:
        return False
    ref = referred_by.lower().strip()
    qry = query.lower().strip()
    if qry in ref or ref in qry:
        return True
    qry_parts = set(qry.split())
    ref_parts = set(ref.split())
    return bool(qry_parts & ref_parts)


@router.get("/internal/pipelines")
async def internal_list_pipelines(request: Request):
    """Return all pipelines (id, name, public_slug, cg1_office_key).
    CG1 uses cg1_office_key to match its own office to a pipeline ID."""
    _verify(request)
    pipes = await db.pipelines.find(
        {}, {"_id": 0, "id": 1, "name": 1, "public_slug": 1, "cg1_office_key": 1}
    ).to_list(100)
    return pipes


@router.get("/internal/aliases")
async def internal_list_aliases(request: Request, pipeline_id: Optional[str] = None, employee_name: Optional[str] = None):
    """List employee email aliases, optionally filtered by pipeline and/or employee name."""
    _verify(request)
    query: dict = {}
    if pipeline_id:
        query["pipeline_id"] = pipeline_id
    if employee_name:
        query["employee_name"] = {"$regex": re.escape(employee_name), "$options": "i"}
    rows = await db.employee_email_aliases.find(query, {"_id": 0}).sort("created_at", 1).to_list(200)
    settings = await db.settings.find_one({"pipeline_id": None}, {"_id": 0, "email_intake": 1}) or {}
    intake = (settings.get("email_intake") or {})
    base_domain = intake.get("inbound_domain") or company_profile.inbound_parse_domain()
    for r in rows:
        r["email"] = f"{r['slug']}@{base_domain}"
    return rows


@router.post("/internal/aliases")
async def internal_create_alias(request: Request, payload: dict = Body(...)):
    """Create a per-employee email alias for a pipeline."""
    _verify(request)
    pipeline_id = (payload.get("pipeline_id") or "").strip()
    employee_name = (payload.get("employee_name") or "").strip()
    slug = re.sub(r"[^a-z0-9-]", "", (payload.get("slug") or "").strip().lower())
    if not (pipeline_id and employee_name and slug):
        raise HTTPException(400, "pipeline_id, employee_name, and slug are required")
    pipe = await db.pipelines.find_one({"id": pipeline_id}, {"_id": 0, "user_id": 1})
    if not pipe:
        raise HTTPException(404, "Pipeline not found")
    existing = await db.employee_email_aliases.find_one({"slug": slug}, {"_id": 0, "id": 1})
    if existing:
        raise HTTPException(409, "That slug is already taken — try a different one")
    doc = {
        "id": new_id(),
        "user_id": pipe["user_id"],
        "pipeline_id": pipeline_id,
        "employee_name": employee_name,
        "slug": slug,
        "created_at": now_iso(),
    }
    await db.employee_email_aliases.insert_one(doc)
    settings = await db.settings.find_one({"pipeline_id": None}, {"_id": 0, "email_intake": 1}) or {}
    intake = (settings.get("email_intake") or {})
    base_domain = intake.get("inbound_domain") or company_profile.inbound_parse_domain()
    doc.pop("_id", None)
    doc["email"] = f"{slug}@{base_domain}"
    return doc


@router.delete("/internal/aliases/{alias_id}")
async def internal_delete_alias(request: Request, alias_id: str):
    """Delete a per-employee email alias."""
    _verify(request)
    res = await db.employee_email_aliases.delete_one({"id": alias_id})
    if not res.deleted_count:
        raise HTTPException(404, "Alias not found")
    return {"ok": True}


_CANDIDATE_FIELDS = {
    "_id": 0, "id": 1, "first_name": 1, "last_name": 1, "email": 1, "phone": 1,
    "stage": 1, "screening_status": 1, "referred_by": 1,
    "appointment_at": 1, "verdict": 1, "hired": 1, "created_at": 1,
    "attendance_status": 1, "form_submitted_at": 1, "form_responses": 1,
    "training_start_at": 1, "cg1_starter_email_id": 1, "cg1_webhook_status": 1,
    "training_attended": 1, "sms_confirmation_status": 1,
    "sms_reschedule_count": 1, "archived_at": 1, "archived_reason": 1,
}


@router.get("/internal/candidates")
async def internal_list_candidates(
    request: Request,
    referred_by: Optional[str] = None,
    pipeline_id: Optional[str] = None,
    stage: Optional[str] = None,
    include_archived: bool = False,
):
    """Return candidates filtered by referred_by (fuzzy), pipeline_id, and/or stage.
    CG1 uses this to show recruits, to-close list, and training roster.

    By default archived candidates (no-shows swept after 48h, rejected-at-close,
    manual archives) are excluded. Pass include_archived=true when you need the
    full historical cohort — e.g. CG1's recruitment-stats counts no-shows that
    were later archived toward the "Booked" total."""
    _verify(request)
    query: dict = {}
    if pipeline_id:
        query["pipeline_id"] = pipeline_id
    if stage:
        query["stage"] = stage
    if not include_archived:
        query["archived_at"] = None
    # Archived history is unbounded (every past no-show / rejection), so sort
    # newest-first and raise the cap when it's included — otherwise a truncated
    # page could drop the current week's candidates the stats endpoint needs.
    limit = 3000 if include_archived else (1000 if referred_by else 500)
    cursor = db.candidates.find(query, _CANDIDATE_FIELDS)
    if include_archived:
        cursor = cursor.sort("created_at", -1)
    candidates = await cursor.to_list(limit)
    if referred_by:
        candidates = [c for c in candidates if _name_matches(c.get("referred_by"), referred_by)]
    return candidates


@router.post("/internal/candidates/{candidate_id}/move-to-training")
async def internal_move_to_training(candidate_id: str, request: Request, payload: dict = Body(...)):
    """Move a CLOSE-stage candidate to TRAINING and fire the CG1 new-starter webhook.

    Body: {
      training_start_at  — ISO datetime for Day 1 start (required),
      monday_start, monday_end, tuesday_start, tuesday_end  — optional time overrides
    }
    """
    _verify(request)
    training_start_at = (payload.get("training_start_at") or "").strip()
    if not training_start_at:
        raise HTTPException(400, "training_start_at is required")

    cand = await db.candidates.find_one({"id": candidate_id}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")
    if cand.get("archived_at"):
        raise HTTPException(400, "Candidate is archived")

    prev_stage = cand.get("stage", "")
    update = {
        "stage": "TRAINING",
        "training_start_at": training_start_at,
        "updated_at": now_iso(),
    }
    await db.candidates.update_one({"id": candidate_id}, {"$set": update})

    # The stage flip above is the only state change the caller needs to wait on.
    # The starter email, CG1 roster webhook and Google Sheets append are all slow
    # network round-trips — run them in the background so the endpoint returns
    # immediately (the CG1 "Closed" button was freezing on this whole chain).
    if prev_stage != "TRAINING":
        import asyncio as _asyncio
        _asyncio.create_task(_run_training_comms(
            candidate_id=candidate_id,
            user_id=cand.get("user_id", ""),
            training_start_at=training_start_at,
            cand=cand,
            monday_start=payload.get("monday_start") or "",
            monday_end=payload.get("monday_end") or "",
            tuesday_start=payload.get("tuesday_start") or "",
            tuesday_end=payload.get("tuesday_end") or "",
        ))

    return {"ok": True, "stage": "TRAINING", "cg1_starter_email_id": None}


async def _run_training_comms(
    *,
    candidate_id: str,
    user_id: str,
    training_start_at: str,
    cand: dict,
    monday_start: str = "",
    monday_end: str = "",
    tuesday_start: str = "",
    tuesday_end: str = "",
    send_email: bool = True,
    append_sheet: bool = True,
    interviewer: Optional[str] = None,
) -> None:
    """The training comms trio — exactly what the kanban TRAINING move fires.

    Shared by internal_move_to_training and internal_create_in_training so the
    two paths can never drift. The CG1 new-starter webhook fires ALWAYS (roster
    sync is independent of notification preferences); the starter email and the
    NEW HIRES sheet append are gated by the caller (send_comms /
    skip_notifications). `interviewer` overrides the sheet's interviewer column;
    when omitted it falls back to the pipeline owner's name/email, then "CG1".
    """
    import asyncio as _asyncio

    extra_tpl = {
        k: v for k, v in (
            ("monday_start", monday_start),
            ("monday_end", monday_end),
            ("tuesday_start", tuesday_start),
            ("tuesday_end", tuesday_end),
        ) if v
    }

    # 1. Send the starter confirmation email from CGRecruit (same path as Kanban move).
    if send_email:
        try:
            from server import _send_starter_email_direct
            await _send_starter_email_direct(
                user_id=user_id,
                candidate_id=candidate_id,
                training_start_at=training_start_at,
                extra_template=extra_tpl or None,
            )
        except Exception as e:
            logger.warning(f"internal training comms: starter email failed: {e}")

    # 2. Fire CG1 new-starter webhook for roster sync — ALWAYS.
    try:
        await _fire_cg1_webhook_inline(
            candidate_id=candidate_id,
            user_id=user_id,
            training_start_at=training_start_at,
            monday_start=monday_start,
            monday_end=monday_end,
            tuesday_start=tuesday_start,
            tuesday_end=tuesday_end,
        )
    except Exception as e:
        logger.warning(f"internal training comms: cg1 webhook failed: {e}")

    # 3. Append to NEW HIRES Google Sheet.
    if append_sheet:
        try:
            from sheets_service import append_new_hire
            pipe = await db.pipelines.find_one({"id": cand.get("pipeline_id"), "user_id": user_id}, {"_id": 0}) or {}
            office_key = company_profile.office_key_for_pipeline(pipe)
            if office_key:
                if not interviewer:
                    user_doc = await db.users.find_one({"id": user_id}, {"_id": 0}) or {}
                    interviewer = user_doc.get("name") or user_doc.get("email") or "CG1"
                await _asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: append_new_hire(
                        office_key=office_key,
                        name=f"{cand.get('first_name', '')} {cand.get('last_name', '')}".strip(),
                        email=cand.get("email") or "",
                        phone=cand.get("phone") or "",
                        training_start_at=training_start_at,
                        interviewer=interviewer,
                    )
                )
        except Exception as e:
            logger.warning(f"internal training comms: sheets append failed (non-fatal): {e}")


async def _fire_cg1_webhook_inline(
    candidate_id: str,
    user_id: str,
    training_start_at: str,
    monday_start: str = "",
    monday_end: str = "",
    tuesday_start: str = "",
    tuesday_end: str = "",
) -> Optional[str]:
    """Fire the CG1 new-starter webhook when moving to TRAINING from the internal API."""
    import os
    import httpx as _httpx
    from datetime import datetime as _dt

    cg1_url = os.getenv("CG1_BACKEND_URL", "").rstrip("/")
    secret = os.getenv("CG1_WEBHOOK_SECRET", "")
    if not cg1_url:
        return None
    try:
        cand = await db.candidates.find_one({"id": candidate_id}, {"_id": 0})
        if not cand:
            return None
        pipe = await db.pipelines.find_one({"id": cand.get("pipeline_id"), "user_id": user_id}, {"_id": 0}) or {}
        office_key = company_profile.office_key_for_pipeline(pipe)
        if not office_key:
            logger.warning(f"CG1 webhook (internal): no office_key for pipeline {pipe.get('id')}")
            return None

        start_date, start_time = "", ""
        if training_start_at:
            try:
                from server import _parse_training_ts
                dt = _parse_training_ts(training_start_at)
                start_date = dt.strftime("%Y-%m-%d")
                start_time = dt.strftime("%-I:%M %p")
            except Exception:
                start_date = training_start_at[:10]

        backend_base = company_profile.backend_api_url()
        webhook_payload = {
            "name": f"{cand.get('first_name', '')} {cand.get('last_name', '')}".strip(),
            "email": cand.get("email") or "",
            "phone": cand.get("phone") or "",
            "start_date": start_date,
            "start_time": start_time,
            "office": office_key,
            "cgrecruit_candidate_id": candidate_id,
            "cgrecruit_callback_url": f"{backend_base.rstrip('/')}/webhooks/cg1/attended",
        }
        if monday_start:
            webhook_payload["monday_start"] = monday_start
        if monday_end:
            webhook_payload["monday_end"] = monday_end
        if tuesday_start:
            webhook_payload["tuesday_start"] = tuesday_start
        if tuesday_end:
            webhook_payload["tuesday_end"] = tuesday_end

        async with _httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{cg1_url}/api/webhooks/cgrecruit/new-starter",
                json=webhook_payload,
                headers={"x-webhook-secret": secret},
            )
            r.raise_for_status()
            cg1_id = r.json().get("starter_email_id")
        if cg1_id:
            await db.candidates.update_one(
                {"id": candidate_id},
                {"$set": {"cg1_starter_email_id": cg1_id, "cg1_webhook_status": "sent", "updated_at": now_iso()}},
            )
        return cg1_id
    except Exception as e:
        logger.warning(f"CG1 webhook (internal) failed for {candidate_id}: {e}")
        await db.candidates.update_one(
            {"id": candidate_id},
            {"$set": {"cg1_webhook_status": f"failed:{e}", "updated_at": now_iso()}},
        )
        return None


@router.post("/internal/candidates/{candidate_id}/reject")
async def internal_reject_candidate(candidate_id: str, request: Request):
    """Archive a CLOSE-stage candidate as rejected. Called by CG1 'To Close' screen."""
    _verify(request)
    cand = await db.candidates.find_one({"id": candidate_id}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")
    await db.candidates.update_one(
        {"id": candidate_id},
        {"$set": {"archived_at": now_iso(), "archived_reason": "rejected_at_close", "updated_at": now_iso()}},
    )
    return {"ok": True}


@router.post("/internal/candidates/{candidate_id}/reschedule-training")
async def internal_reschedule_training(candidate_id: str, request: Request, payload: dict = Body(...)):
    """Update training_start_at when CG1 reschedules the starter from its own UI."""
    _verify(request)
    training_start_at = (payload.get("training_start_at") or "").strip()
    if not training_start_at:
        raise HTTPException(400, "training_start_at is required")
    cand = await db.candidates.find_one({"id": candidate_id}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")
    await db.candidates.update_one(
        {"id": candidate_id},
        {"$set": {"training_start_at": training_start_at, "updated_at": now_iso()}},
    )
    return {"ok": True, "training_start_at": training_start_at}


@router.get("/internal/pipelines/{pipeline_id}/slots")
async def internal_pipeline_slots(pipeline_id: str, request: Request, days: int = 21):
    """Available interview slots for a pipeline. Called by CG1 direct-booking UI."""
    _verify(request)
    pipe = await db.pipelines.find_one({"id": pipeline_id}, {"_id": 0})
    if not pipe:
        raise HTTPException(404, "Pipeline not found")
    user_id = pipe["user_id"]
    settings = await db.settings.find_one({"user_id": user_id, "pipeline_id": pipeline_id}, {"_id": 0}) or {}
    if not settings:
        settings = await db.settings.find_one({"user_id": user_id, "pipeline_id": None}, {"_id": 0}) or {}
    booked_docs = await db.candidates.find(
        {"pipeline_id": pipeline_id, "appointment_at": {"$ne": None}},
        {"_id": 0, "appointment_at": 1},
    ).to_list(1000)
    booked_isos = [b.get("appointment_at") for b in booked_docs if b.get("appointment_at")]
    from availability_service import compute_available_slots
    appt_settings = settings.get("appointments") or {}
    default_cap = int(appt_settings.get("applicant_limit") or 50)
    tz_name = pipe.get("timezone") or (settings.get("region_language") or {}).get("timezone") or default_tz_name()
    return {
        "pipeline_id": pipeline_id,
        "pipeline_name": pipe.get("name", ""),
        "timezone": tz_name,
        "slots": compute_available_slots(
            pipe, booked_isos, days_ahead=max(1, min(days, 90)),
            tz_name=tz_name, default_capacity=default_cap,
        ),
    }


@router.post("/internal/candidates/direct-book")
async def internal_direct_book(request: Request, payload: dict = Body(...)):
    """Create a candidate directly at APPOINTMENT stage (bypass AI screening).
    Called by CG1 when a leader books someone straight into an interview slot.

    Body: { pipeline_id, first_name, last_name?, email?, phone?, appointment_at,
            job_id?, referred_by?, send_confirmation? }
    """
    _verify(request)
    pipeline_id   = (payload.get("pipeline_id") or "").strip()
    first_name    = (payload.get("first_name") or "").strip()
    last_name     = (payload.get("last_name") or "").strip()
    email         = (payload.get("email") or "").strip().lower()
    phone         = (payload.get("phone") or "").strip()
    appointment_at = (payload.get("appointment_at") or "").strip()
    job_id        = (payload.get("job_id") or "").strip() or None
    referred_by   = (payload.get("referred_by") or "").strip() or None
    send_conf     = payload.get("send_confirmation", True)

    if not (pipeline_id and first_name and appointment_at):
        raise HTTPException(400, "pipeline_id, first_name, appointment_at required")

    pipe = await db.pipelines.find_one({"id": pipeline_id}, {"_id": 0})
    if not pipe:
        raise HTTPException(404, "Pipeline not found")
    user_id = pipe["user_id"]

    from deps import find_duplicate_candidate
    dup = await find_duplicate_candidate(pipeline_id, email, phone)
    if dup:
        return {"ok": True, "duplicate": True, "existing_id": dup["id"],
                "message": "A candidate with this email or phone is already in this pipeline"}

    from models import Candidate
    cand = Candidate(
        user_id=user_id, pipeline_id=pipeline_id, job_id=job_id,
        first_name=first_name, last_name=last_name,
        email=email, phone=phone,
        stage="APPOINTMENT", appointment_at=appointment_at,
        appointment_link=pipe.get("appointment_link") or None,
        referred_by=referred_by,
    )
    doc = cand.model_dump()
    await db.candidates.insert_one(doc)

    import asyncio as _asyncio

    async def _background_comms():
        try:
            from auto_dialer import schedule_appointment_reminders
            await schedule_appointment_reminders(user_id, cand.id, appointment_at)
        except Exception as e:
            logger.warning(f"direct-book reminders failed: {e}")
        if send_conf and email:
            try:
                from deps import send_stage_comms
                await send_stage_comms(user_id, doc, "approval")
            except Exception as e:
                logger.warning(f"direct-book approval comms failed: {e}")

    _asyncio.create_task(_background_comms())

    return {"ok": True, "candidate_id": cand.id, "duplicate": False}


@router.post("/internal/candidates/create-in-training")
async def internal_create_in_training(request: Request, payload: dict = Body(...)):
    """Create a candidate directly in TRAINING — the kanban "+" on the TRAINING
    column, callable by CG1 with an office key instead of a pipeline id.

    Body: { office_key (a key from company_profile.json offices), first_name, last_name?, email?,
            phone?, start_date ("YYYY-MM-DD"), start_time ("HH:MM" 24h),
            monday_start?, monday_end?, tuesday_start?, tuesday_end?,
            send_comms? (default true), booked_by? }

    Effects (identical to the kanban "+" → move-to-TRAINING chain):
    stores naive-ET training_start_at, sends the starter confirmation email and
    appends the NEW HIRES sheet row (both skipped when send_comms=false), fires
    the CG1 new-starter webhook ALWAYS, and leaves sms_confirmation_sent_at
    ABSENT so the 5-min sweep arms the confirmation SMS on its own. No
    screening, warmup, or dialer jobs ever start for these candidates.
    """
    _verify(request)
    office_key = (payload.get("office_key") or "").strip().lower()
    first_name = (payload.get("first_name") or "").strip()
    last_name  = (payload.get("last_name") or "").strip()
    email      = (payload.get("email") or "").strip().lower()
    phone      = (payload.get("phone") or "").strip()
    start_date = (payload.get("start_date") or "").strip()
    start_time = (payload.get("start_time") or "").strip()
    send_comms = bool(payload.get("send_comms", True))
    booked_by  = (payload.get("booked_by") or "").strip()

    if not (office_key and first_name and start_date and start_time):
        raise HTTPException(400, "office_key, first_name, start_date, start_time required")

    from datetime import datetime as _dt
    try:
        _dt.strptime(start_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "start_date must be YYYY-MM-DD")
    try:
        _dt.strptime(start_time, "%H:%M")
    except ValueError:
        raise HTTPException(400, "start_time must be HH:MM (24h)")

    pipe = await db.pipelines.find_one({"cg1_office_key": office_key}, {"_id": 0})
    if not pipe:
        raise HTTPException(404, f"No pipeline for office_key '{office_key}'")
    user_id = pipe["user_id"]
    pipeline_id = pipe["id"]

    from deps import find_duplicate_candidate
    dup = await find_duplicate_candidate(pipeline_id, email, phone)
    if dup:
        return {"ok": True, "duplicate": True, "existing_id": dup["id"],
                "existing_stage": dup.get("stage")}

    # Naive ET ISO — exactly what QuickAddTrainingModal sends the /move endpoint.
    training_start_at = f"{start_date}T{start_time}:00"

    from models import Candidate
    cand = Candidate(
        user_id=user_id, pipeline_id=pipeline_id,
        first_name=first_name, last_name=last_name,
        email=email, phone=phone,
        stage="TRAINING",
        training_start_at=training_start_at,
        moved_to_training_at=now_iso(),
    )
    doc = cand.model_dump()
    # Kanban parity: move_candidate stamps email_sent = skip_notifications.
    doc["email_sent"] = not send_comms
    # The 5-min confirmation-SMS sweep keys on sms_confirmation_sent_at with
    # $exists: False — the field must never be written here, not even as null.
    doc.pop("sms_confirmation_sent_at", None)
    await db.candidates.insert_one(doc)

    import asyncio as _asyncio
    _asyncio.create_task(_run_training_comms(
        candidate_id=cand.id,
        user_id=user_id,
        training_start_at=training_start_at,
        cand=doc,
        monday_start=(payload.get("monday_start") or ""),
        monday_end=(payload.get("monday_end") or ""),
        tuesday_start=(payload.get("tuesday_start") or ""),
        tuesday_end=(payload.get("tuesday_end") or ""),
        send_email=send_comms,
        append_sheet=send_comms,
        interviewer=(booked_by or "CG1"),
    ))

    return {"ok": True, "candidate_id": cand.id, "duplicate": False}


@router.post("/internal/candidates/{candidate_id}/attendance")
async def internal_set_attendance(candidate_id: str, request: Request, payload: dict = Body(...)):
    """Set attendance outcome for a booked interview. Called by CG1 Booked Interviews screen."""
    _verify(request)
    status = (payload.get("status") or "").strip()
    if status not in ("attended_form", "attended_no_form", "no_show"):
        raise HTTPException(400, "status must be attended_form | attended_no_form | no_show")
    cand = await db.candidates.find_one({"id": candidate_id}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")

    update: dict = {"attendance_status": status, "updated_at": now_iso()}
    if status == "attended_form":
        update["stage"] = "FORM"
        update["rescheduled"] = False
    elif status == "no_show":
        try:
            from auto_dialer import cancel_appointment_reminders
            cancel_appointment_reminders(candidate_id)
        except Exception as e:
            logger.warning(f"cancel reminders on no_show: {e}")
        update["previous_appointment_at"] = cand.get("appointment_at")
        update["rescheduled"] = False
        update["no_show_at"] = now_iso()
    elif status == "attended_no_form":
        update["rescheduled"] = False

    await db.candidates.update_one({"id": candidate_id}, {"$set": update})
    fresh = await db.candidates.find_one({"id": candidate_id}, {"_id": 0})

    comms_result = None
    if status == "attended_form" and fresh:
        try:
            from deps import send_stage_comms
            comms_result = await send_stage_comms(fresh["user_id"], fresh, "form")
            logger.info(f"form comms for {candidate_id}: {comms_result}")
        except Exception as e:
            logger.warning(f"form comms via internal failed: {e}")
            comms_result = {"email": {"status": "failed", "error": str(e)}}
        try:
            from auto_dialer import schedule_form_reminder
            await schedule_form_reminder(fresh["user_id"], candidate_id)
        except Exception as e:
            logger.warning(f"schedule_form_reminder via internal failed: {e}")
    elif status == "no_show" and fresh:
        try:
            from routes.attendance import _fire_no_show_outreach
            await _fire_no_show_outreach(fresh["user_id"], fresh)
        except Exception as e:
            logger.warning(f"no_show outreach via internal failed: {e}")

    return {"ok": True, "attendance_status": status, "stage": (fresh or {}).get("stage"), "comms": comms_result}


@router.post("/internal/run-archive-sweep")
async def run_archive_sweep(request: Request):
    """Manually trigger the no-show auto-archive sweep. Useful when the hourly
    cron hasn't fired yet or to immediately clear stale APPOINTMENT candidates.

    Delegates to the scheduled sweep rather than re-implementing it. The copy
    that used to live here filed every silently-lapsed appointment as a no-show,
    so triggering it by hand undid the distinction the cron draws between a real
    no-show, a candidate who cancelled in advance, and an outcome nobody
    recorded."""
    _verify(request)
    from datetime import datetime, timezone, timedelta
    from dialer.scheduler import _auto_archive_stale_no_shows

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    started_at = now_iso()
    await _auto_archive_stale_no_shows()

    fresh = await db.candidates.find(
        {"archived_at": {"$gte": started_at}},
        {"_id": 0, "first_name": 1, "last_name": 1, "archived_reason": 1},
    ).to_list(500)
    by_reason: Dict[str, int] = {}
    for c in fresh:
        by_reason[c.get("archived_reason") or "unknown"] = by_reason.get(c.get("archived_reason") or "unknown", 0) + 1

    return {
        "ok": True,
        "archived_count": len(fresh),
        "archived": [f"{c.get('first_name', '')} {c.get('last_name', '')}".strip() for c in fresh],
        "cutoff": cutoff,
        "debug": {"by_reason": by_reason},
    }
