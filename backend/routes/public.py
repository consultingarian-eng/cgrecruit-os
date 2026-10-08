"""Public applicant-facing endpoints: apply, status portal, availability, AI booking, reschedule.

Candidate-facing routes are authenticated by the unguessable per-candidate
`public_token` in the URL (or are read-only, like the apply page and the slot
list). The voice agent's tool routes act on a phone number alone, so they
require the X-CGR-Tool-Secret header that ElevenLabs sends (see
webhook_auth.require_elevenlabs_tool_secret) and refuse every call without it.
"""
import os
import company_profile
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional
from fastapi import APIRouter, HTTPException, Body, Depends, Request

from deps import db, logger, get_or_create_settings, resolve_settings, send_stage_email
from email_service import is_in_call_window
from models import Candidate, now_iso
from auto_dialer import schedule_call_with_window
from webhook_auth import require_elevenlabs_tool_secret
from app_tz import app_zone, default_tz_name  # APP_TIMEZONE (noqa: F401)

router = APIRouter()

_VOWELS = set("aeiouyAEIOUY")


def _looks_like_gibberish_token(token: str) -> bool:
    """Heuristic for a random bot-generated 'word' like 'qctUVIpkKpAMUMXQmCwZaEH'.

    Real names — even long or non-English ones — keep a healthy vowel ratio and
    don't flip letter case several times inside a single word. Bot tokens do both.
    Conservative on purpose (needs length + low vowels + many case flips) so it
    won't reject legitimate names."""
    t = token.strip()
    if len(t) < 12 or not t.isalpha():
        return False
    vowel_ratio = sum(1 for ch in t if ch in _VOWELS) / len(t)
    # Count transitions between lower→upper / upper→lower inside the word.
    case_flips = sum(
        1 for a, b in zip(t, t[1:])
        if a.isalpha() and b.isalpha() and a.islower() != b.islower()
    )
    # Either signal alone is damning for a 12+ char single word: real names
    # (even Slavic/consonant-heavy) keep ~30%+ vowels and never flip case 4+
    # times inside one word.
    return case_flips >= 4 or vowel_ratio < 0.20


def _reject_reason(first_name: str, last_name: str, phone: str) -> Optional[str]:
    """Return a short reason string if this application looks like form spam,
    else None. The precise signal is the gibberish-name check; the phone check
    only rejects essentially non-numeric values (bot tokens have ~0 digits).
    We deliberately do NOT require a full 10 digits — real applicants fat-finger
    their number (8–9 digits, or drop the area code) and we won't turn them away
    over a typo. A bad-but-numeric phone still surfaces to the recruiter."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 7:
        return "phone_not_dialable"
    for tok in f"{first_name} {last_name}".split():
        if _looks_like_gibberish_token(tok):
            return "name_gibberish"
    return None


@router.get("/public/pipelines/{slug}")
async def public_pipeline(slug: str):
    """Public pipeline details by slug (e.g., 'downtown'). Includes active jobs + recruiter company."""
    pipe = await db.pipelines.find_one({"public_slug": slug}, {"_id": 0})
    if not pipe:
        raise HTTPException(404, "Pipeline not found")
    jobs = await db.jobs.find(
        {"pipeline_id": pipe["id"], "is_active": True}, {"_id": 0}
    ).to_list(50)
    settings = await resolve_settings(pipe["user_id"], pipe["id"])
    profile = (settings.get("recruiter_profile") or {})
    custom_form = (settings.get("custom_form") or {}).get("questions", [])
    # The apply page's promises must match the pipeline's actual first touch:
    # offices can run chat-first or voice-first, and the
    # copy hardcoding one mode meant one office always lied about what happens
    # next ("We'll call you" → a text arrives, or vice versa).
    from models import resolve_screening_mode
    sca = settings.get("screen_call_agent") or {}
    # Public page: only what the apply form needs. The pipeline document also
    # holds interview meeting links, per-slot interviewers, agent ids and the
    # owner's user id, none of which belong on an unauthenticated response.
    public_pipe = {k: pipe.get(k) for k in ("id", "name", "description", "public_slug")}
    public_jobs = [
        {k: j.get(k) for k in ("id", "title", "category", "description", "city", "region", "country", "is_active")}
        for j in jobs
    ]
    return {
        "pipeline": public_pipe,
        "jobs": public_jobs,
        "company": {
            "name": profile.get("company_name") or company_profile.company_name(),
            "website": profile.get("website", ""),
            "city": profile.get("city", ""),
        },
        "custom_form": custom_form,
        "screening": {
            "mode": resolve_screening_mode(settings),
            "agent_name": sca.get("agent_name") or "Olivia",
        },
        "phone_country": _phone_country(),
    }


# Each application texts or calls the number it was given. Per client IP, so a
# script can't run up the phone bill or use the company number on strangers.
# Generous enough for a hiring event on one office wifi.
APPLY_LIMIT_PER_IP = 20
APPLY_WINDOW_SECONDS = 15 * 60


@router.post("/public/apply")
async def public_apply(request: Request, payload: Dict[str, Any] = Body(...)):
    """Submit a public application. Creates a candidate and returns the public_token for status tracking.

    De-duplication runs in TWO passes:
      1. **Same pipeline**: if the same email or phone (normalised) already
         exists in this pipeline AND that candidate isn't closed, return the
         EXISTING token instead of spawning a duplicate. This is the original
         behaviour (prevents the "old appointment + new call retry" bug).
      2. **Cross-pipeline (NEW v34.8)**: if the candidate is in a DIFFERENT
         pipeline of the same tenant, we still create a fresh candidate (they
         applied to a different role/office and that's legitimate), BUT we:
           - Notify the recruiter via the bell ("Cross-pipeline duplicate")
           - Auto-disable auto-dial on the new candidate so we don't dial them
             twice while one of the pipelines is mid-screening.
           - Tag the new doc with `duplicate_of` for the merge UI to find.
    """
    import rate_limit
    ip = rate_limit.client_ip(request.headers.get("x-forwarded-for"),
                              request.client.host if request.client else None)
    if not rate_limit.allow(f"apply:{ip}", APPLY_LIMIT_PER_IP, APPLY_WINDOW_SECONDS):
        raise HTTPException(429, "Too many applications from this connection. Please try again in a little while.")
    pipeline_id = payload.get("pipeline_id")
    job_id = payload.get("job_id")
    # Ids go straight into Mongo queries: strings only, never operator objects.
    if not isinstance(pipeline_id, str) or (job_id is not None and not isinstance(job_id, str)):
        raise HTTPException(400, "pipeline_id and job_id must be strings")
    first_name = (payload.get("first_name") or "").strip()
    last_name = (payload.get("last_name") or "").strip()
    email = (payload.get("email") or "").strip().lower()
    phone = (payload.get("phone") or "").strip()
    referred_by = (payload.get("referred_by") or "").strip() or None
    if not (pipeline_id and first_name and email and phone):
        raise HTTPException(400, "pipeline_id, first_name, email, phone are required")
    # Store E.164 whenever the number normalizes confidently — "(617) 555-0134"
    # is how people actually type it, and the dial APIs reject it raw.
    from deps import normalize_phone_e164
    phone = normalize_phone_e164(phone)
    # Spam gate — bots hammer this unauthenticated endpoint with random-string
    # names + phones. Reject before we create a candidate (which would fire
    # warmup email/SMS + schedule an AI screening call). See _reject_reason.
    spam_reason = _reject_reason(first_name, last_name, phone)
    if spam_reason:
        logger.warning(
            f"public_apply: rejected suspected spam ({spam_reason}) "
            f"name={first_name!r} {last_name!r} phone={phone!r} email={email!r} "
            f"pipeline={pipeline_id}"
        )
        raise HTTPException(400, "Please enter a valid name and phone number.")
    pipe = await db.pipelines.find_one({"id": pipeline_id}, {"_id": 0})
    if not pipe:
        raise HTTPException(404, "Pipeline not found")

    # ---- De-duplication ----
    # Phones are stored a few different ways across the codebase (raw, +1-prefixed,
    # spaces, parens). Match by last-10-digit fingerprint to catch all of them.
    digits_only = "".join(ch for ch in phone if ch.isdigit())
    phone_last10 = digits_only[-10:] if len(digits_only) >= 10 else digits_only
    # Pass 1 — same pipeline
    dup_query = {
        "pipeline_id": pipeline_id,
        # Don't match closed/rejected/no-show candidates — those are "done", let
        # the person re-apply for a fresh chance.
        "stage": {"$nin": ["CLOSE"]},
        "archived_at": None,  # also skip soft-rejected — they can re-apply
        "$or": [
            # Case-insensitive: candidates filed from CV parses can carry the
            # address in whatever case the CV used, and an exact match let a
            # same-address duplicate through.
            {"email": {"$regex": f"^{re.escape(email)}$", "$options": "i"}},
        ],
    }
    if phone_last10:
        # Mongo regex on the digits-only phone substring — phone field in the DB
        # is stored as the raw user-entered string, so $regex catches "+15550100199"
        # / "5550100199" / "(555) 010-0199" alike.
        dup_query["$or"].append({"phone": {"$regex": re.escape(phone_last10)}})
    existing = await db.candidates.find_one(dup_query, {"_id": 0})
    if existing:
        logger.info(
            f"public_apply: duplicate application for existing candidate {existing['id']} "
            f"(stage={existing.get('stage')}) in pipeline {pipeline_id}"
        )
        # Never hand back the existing record's portal token: knowing someone's
        # email or phone number is not proof of being them, and the token opens
        # their portal (contact details, chat history, booking). They already
        # received their own link by text/email when they first applied.
        return {
            "duplicate": True,
            "first_name": first_name,
            "message": "You've already applied. Check your texts and email from us for your personal link.",
        }

    # Pass 2 — cross-pipeline (same tenant, ANY other pipeline). Used to flag
    # & soft-link duplicates so we don't double-dial the same person.
    cross_dup_query = {
        "user_id": pipe["user_id"],
        "pipeline_id": {"$ne": pipeline_id},
        "stage": {"$nin": ["CLOSE"]},
        "archived_at": None,
        "$or": [{"email": {"$regex": f"^{re.escape(email)}$", "$options": "i"}}],
    }
    if phone_last10:
        cross_dup_query["$or"].append({"phone": {"$regex": re.escape(phone_last10)}})
    cross_existing = await db.candidates.find_one(cross_dup_query, {"_id": 0})

    cand = Candidate(
        user_id=pipe["user_id"],
        pipeline_id=pipeline_id,
        job_id=job_id,
        first_name=first_name,
        last_name=last_name,
        email=email,
        phone=phone,
        stage="SCREENING",
        referred_by=referred_by,
    )
    # If we found a cross-pipeline duplicate, disable auto-dial on this NEW
    # candidate (the original is already in flight) and stash the link.
    if cross_existing:
        cand.auto_dial = False
        # Stash both directions of the link for the merge UI — neither doc is
        # the "canonical" one yet; the recruiter decides during merge.
        try:
            await db.candidates.update_one(
                {"id": cross_existing["id"]},
                {"$addToSet": {"cross_pipeline_duplicates": cand.id}},
            )
        except Exception as e:
            logger.warning(f"cross-pipeline duplicate link failed: {e}")
        logger.info(
            f"public_apply: cross-pipeline duplicate detected — new {cand.id} in "
            f"{pipe.get('name')} matches existing {cross_existing['id']} in "
            f"pipeline {cross_existing.get('pipeline_id')}; auto_dial disabled."
        )
    settings = await resolve_settings(pipe["user_id"], pipe["id"])
    profile = (settings.get("recruiter_profile") or {})
    company = profile.get("company_name") or company_profile.company_name()
    sca = (settings.get("screen_call_agent") or {})
    agent_name = sca.get("agent_name", "Olivia")
    job = None
    if job_id:
        job = await db.jobs.find_one({"id": job_id}, {"_id": 0})
    role_title = (job or {}).get("title", "")
    role_phrase = f"the {role_title} role" if role_title else "the role"
    location = pipe.get("name", "")
    # The seeded opener shows on the applicant status page and in the recruiter
    # transcript, so it must match what actually happens next: under the chat
    # modes the candidate gets a text, not a call.
    from models import resolve_screening_mode
    if resolve_screening_mode(settings) in ("chat_first", "chat_only"):
        next_step = (
            "We've just texted you a few quick screening questions — you can answer "
            "there, or continue right here. It takes about three minutes."
        )
    else:
        next_step = (
            f"We'll call you shortly for a brief screening — you'll speak with our AI "
            f"assistant {agent_name}, it takes a couple of minutes."
        )
    cand.chat_log = [{
        "role": "agent",
        "text": (
            f"Hi {first_name}, thanks for applying for {role_phrase} at {company} in {location}.\n\n"
            f"{next_step}\n\n"
            f"If suitable we will then book your interview with the Hiring Manager."
        ),
        "at": now_iso(),
    }]
    doc = cand.model_dump()
    await db.candidates.insert_one(doc)
    from screening_start import start_screening

    await start_screening(
        pipe["user_id"], doc, settings,
        allow_dial=(settings.get("auto_dialer") or {}).get("auto_dial_on_apply", True),
        dial_opt_out_reason="dialer_disabled",
    )

    # In-app notifications. Per-application "X applied" notifications were
    # removed (v36) — they drowned out the actionable ones. Duplicates still
    # notify because the recruiter must decide whether to merge.
    try:
        from notifications_service import create_notification
        full_name = f"{first_name} {last_name}".strip() or first_name
        if cross_existing:
            other_pipe = await db.pipelines.find_one({"id": cross_existing.get("pipeline_id")}, {"_id": 0}) or {}
            await create_notification(
                pipe["user_id"], "candidate.duplicate",
                f"⚠️ {full_name} — cross-pipeline duplicate",
                body=(
                    f"Already applied to {other_pipe.get('name', 'another pipeline')} — "
                    "auto-dial disabled on this entry. Open & merge if needed."
                ),
                link=f"/?candidate={cand.id}",
                candidate_id=cand.id, pipeline_id=pipeline_id,
            )
    except Exception as e:
        logger.warning(f"apply notification failed: {e}")

    return {"public_token": cand.public_token, "candidate_id": cand.id, "first_name": first_name}


def _phone_country() -> str:
    from phone_util import default_country
    return default_country()


def _public_view(cand: Dict[str, Any]) -> Dict[str, Any]:
    """Strip a candidate doc to public-safe fields."""
    return {
        "id": cand.get("id"),
        "first_name": cand.get("first_name"),
        "last_name": cand.get("last_name"),
        "email": cand.get("email"),
        "stage": cand.get("stage"),
        "screening_status": cand.get("screening_status"),
        "appointment_at": cand.get("appointment_at"),
        "appointment_link": cand.get("appointment_link"),
        "appointment_recruiter": cand.get("appointment_recruiter"),
        # The portal's phone-confirmation card (FORM/CLOSE) shows the number
        # we'll dial for the 1-on-1 — too many stored numbers turned out to be
        # unreachable, so the candidate confirms or corrects it themselves.
        "phone": cand.get("phone"),
        "phone_confirmed_at": cand.get("phone_confirmed_at"),
        # How the portal formats and checks a typed number (PHONE_DEFAULT_COUNTRY).
        "phone_country": _phone_country(),
        "chat_log": cand.get("chat_log") or [],
        "form_responses": cand.get("form_responses") or {},
        "created_at": cand.get("created_at"),
        "updated_at": cand.get("updated_at"),
    }


@router.get("/public/applicant/{token}")
async def public_applicant(token: str):
    cand = await db.candidates.find_one({"public_token": token}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Application not found")
    pipe = await db.pipelines.find_one({"id": cand.get("pipeline_id")}, {"_id": 0})
    job = None
    if cand.get("job_id"):
        job = await db.jobs.find_one({"id": cand["job_id"]}, {"_id": 0})
    settings = await resolve_settings(cand["user_id"], cand.get("pipeline_id"))
    profile = (settings.get("recruiter_profile") or {})
    # The questionnaire unlocks at FORM stage (see public_submit_form's gate);
    # don't hand the questions to earlier stages either — the portal link now
    # goes out with the booking confirmation, well before the form is open.
    custom_form = (
        (settings.get("custom_form") or {}).get("questions", [])
        if cand.get("stage") in ("FORM", "CLOSE") else []
    )
    sca = (settings.get("screen_call_agent") or {})
    callback_number = (
        (pipe or {}).get("twilio_phone_number")
        or sca.get("custom_caller_id")
        or profile.get("phone")
        or ""
    )
    view = _public_view(cand)
    # The interviewer and meeting link are LIVE-resolved from the calendar at
    # page load, not read from the values stamped at booking time. A
    # last-minute interviewer swap (morning of) is made on
    # the calendar; eight already-booked candidates carrying yesterday's
    # stamp must see today's truth. The stamp remains the fallback for slots
    # with no calendar rule — a bespoke manual booking keeps whatever the
    # recruiter typed.
    # The NAME only — links stay stamped, per how the office actually runs:
    # rooms don't change last-minute, people do, and the link on the portal
    # must never diverge from the one already sitting in their email.
    if cand.get("appointment_at"):
        try:
            from availability_service import resolve_slot_recruiter
            _tz = (settings.get("region_language") or {}).get("timezone") or default_tz_name()
            _live_rec = resolve_slot_recruiter(pipe or {}, cand["appointment_at"], _tz)
            if _live_rec:
                view["appointment_recruiter"] = _live_rec
        except Exception as e:
            logger.warning(f"live slot detail resolution failed for {cand.get('id')}: {e}")
    # What the portal may promise: the mode stamped at intake wins (it is what
    # actually happened for THIS candidate); the pipeline's current mode covers
    # candidates from before the stamp existed. One office may be chat-first and
    # another voice-first — the cards must never claim a text was
    # sent when the office leads with a call, or vice versa.
    from models import resolve_screening_mode
    _mode = cand.get("screening_mode_used") or resolve_screening_mode(settings)
    return {
        "candidate": view,
        "pipeline": {"name": (pipe or {}).get("name", ""), "slug": (pipe or {}).get("public_slug", "")},
        "job": {"title": (job or {}).get("title", ""), "description": (job or {}).get("description", "")},
        "company": {
            "name": profile.get("company_name") or company_profile.company_name(),
            "website": profile.get("website", ""),
            "instagram": ((profile.get("social_links") or {}).get("instagram", "")) or "",
            "phone": profile.get("phone", "") or callback_number,
        },
        "callback_number": callback_number,
        "custom_form": custom_form,
        "screening": {"mode": _mode, "agent_name": sca.get("agent_name") or "Olivia"},
    }


# Outer bound for a self-serve booking. The availability rules are recurring, so
# a hand-crafted date years out would still match a weekday+time and pass the
# capacity check — this stops it landing on the calendar.
_PORTAL_BOOKING_HORIZON_DAYS = 90


async def _validate_portal_slot(
    cand: Dict[str, Any],
    pipeline: Dict[str, Any],
    settings: Dict[str, Any],
    slot_iso: str,
) -> None:
    """Guard a self-serve booking made from the applicant status portal.

    The portal only ever renders slots the availability engine produced, so
    anything failing these checks is a stale page or a hand-crafted POST.
    Mirrors the reschedule portal (routes/attendance.py) — before this, these
    two routes were the only candidate-facing way to write an arbitrary
    appointment_at: past dates, full slots, times nobody is holding."""
    from datetime import datetime, timedelta, timezone
    from availability_service import slot_capacity_remaining

    try:
        slot_dt = datetime.fromisoformat(slot_iso.replace("Z", "+00:00"))
    except Exception:
        raise HTTPException(400, "Invalid appointment_at format.")
    if slot_dt.tzinfo is None:
        raise HTTPException(400, "appointment_at must include a timezone offset.")
    now = datetime.now(timezone.utc)
    if slot_dt < now:
        raise HTTPException(400, "That time has already passed — please pick another slot.")
    if slot_dt > now + timedelta(days=_PORTAL_BOOKING_HORIZON_DAYS):
        raise HTTPException(400, "That date is too far out — please pick a slot from the list.")

    # Count every booking in the pipeline rather than querying for an exact ISO
    # string: slot_capacity_remaining normalises to a UTC instant, so a seat
    # taken as "…Z" still counts against a request formatted as "…+00:00".
    booked = await db.candidates.find(
        {"pipeline_id": pipeline.get("id"), "appointment_at": {"$ne": None},
         "id": {"$ne": cand["id"]}},
        {"_id": 0, "appointment_at": 1},
    ).to_list(2000)
    appt_settings = settings.get("appointments") or {}
    tz_name = (settings.get("region_language") or {}).get("timezone") or default_tz_name()
    remaining = slot_capacity_remaining(
        pipeline,
        [b.get("appointment_at") for b in booked if b.get("appointment_at")],
        slot_iso,
        default_capacity=int(appt_settings.get("applicant_limit") or 50),
        tz_name=tz_name,
    )
    if remaining == -1:
        raise HTTPException(409, "That time is no longer available — please pick another slot.")
    if remaining <= 0:
        raise HTTPException(409, "That slot is full — please pick another.")


@router.post("/public/applicant/{token}/book")
async def public_book_appointment(token: str, payload: Dict[str, Any] = Body(...)):
    """Applicant books an appointment slot."""
    cand = await db.candidates.find_one({"public_token": token}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Application not found")
    appt_at = (payload.get("appointment_at") or "").strip()
    if not appt_at:
        raise HTTPException(400, "appointment_at required")
    settings = await resolve_settings(cand["user_id"], cand.get("pipeline_id"))
    profile = (settings.get("recruiter_profile") or {})
    company = profile.get("company_name") or company_profile.company_name()
    pipeline = await db.pipelines.find_one({"id": cand.get("pipeline_id"), "user_id": cand["user_id"]}, {"_id": 0}) or {}
    await _validate_portal_slot(cand, pipeline, settings, appt_at)
    from availability_service import resolve_slot_recruiter
    from deps import booking_link_or_alert
    _tz = (settings.get("region_language") or {}).get("timezone") or default_tz_name()
    update = {
        "stage": "APPOINTMENT",
        "appointment_at": appt_at,
        # Server-resolved only. This is a public-token endpoint — accepting a
        # link or interviewer name from the request body let anyone with the
        # URL write an arbitrary "Join:" link into their own confirmation.
        # Empty when unconfigured — never a placeholder URL.
        "appointment_link": await booking_link_or_alert(pipeline, appt_at, _tz, cand),
        "appointment_recruiter": resolve_slot_recruiter(pipeline, appt_at, _tz) or pipeline.get("appointment_recruiter") or "Hiring Team",
        "updated_at": now_iso(),
    }
    chat = list(cand.get("chat_log") or [])
    _join_line = f"\nJoin: {update['appointment_link']}" if update["appointment_link"] else ""
    chat.append({
        "role": "agent",
        "text": f"Your appointment with {company} has been booked.\nDate/Time: {appt_at}{_join_line}",
        "at": now_iso(),
    })
    update["chat_log"] = chat
    await db.candidates.update_one({"id": cand["id"]}, {"$set": update})
    refreshed = await db.candidates.find_one({"id": cand["id"]}, {"_id": 0})
    # A candidate can reach this door minutes after applying, before any
    # screening exists (applied 11:08, self-booked 11:15) — telling
    # them "your screening was successful" and then texting "four quick
    # checks ahead of your interview" the next morning reads as a system
    # contradicting itself. The unscreened variant confirms the booking and
    # PRIMES the gate check instead.
    # "incomplete" is a verdict string but NOT a completed screening — a candidate
    # answered one question, self-booked, and the truthy "incomplete" verdict
    # bought them a "your screening was successful" text. Only a real verdict
    # counts.
    _appr_key = (
        "approval"
        if ((refreshed.get("verdict") or "") not in ("", "incomplete") or refreshed.get("call_summary"))
        else "approval_unscreened"
    )
    try:
        await send_stage_email(user_id=cand["user_id"], candidate=refreshed, template_key=_appr_key)
    except Exception as e:
        logger.warning(f"approval email failed: {e}")
    if refreshed.get("phone"):
        try:
            from email_service import build_template_vars, render_template, get_template_for_key
            from sms_service import send_candidate_sms
            live_tpl = get_template_for_key(settings, _appr_key) or {}
            if not (live_tpl.get("sms_enabled") is False or live_tpl.get("enabled") is False):
                sms_body_tpl = live_tpl.get("sms_body") or ""
                if sms_body_tpl:
                    job = await db.jobs.find_one({"id": refreshed["job_id"]}, {"_id": 0}) if refreshed.get("job_id") else None
                    body = render_template(sms_body_tpl, build_template_vars(refreshed, settings, job))
                    await send_candidate_sms(db, settings, refreshed, body, template_key=_appr_key)
        except Exception as e:
            logger.warning(f"approval sms failed: {e}")
    try:
        from auto_dialer import schedule_appointment_reminders, cancel_pending_retry_calls
        await schedule_appointment_reminders(cand["user_id"], cand["id"], appt_at)
        cancel_pending_retry_calls(cand["id"])
    except Exception as e:
        logger.warning(f"reminder scheduling failed: {e}")
    from screening_outcome import ensure_screening_outcome_later
    ensure_screening_outcome_later(db, cand["id"])
    return _public_view(refreshed)


@router.post("/public/applicant/{token}/form")
async def public_submit_form(token: str, payload: Dict[str, Any] = Body(...)):
    """Applicant submits the custom questionnaire."""
    cand = await db.candidates.find_one({"public_token": token}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Application not found")
    # The portal UI only shows the questionnaire at FORM stage, but this
    # endpoint is public-token auth — without a stage gate, a candidate could
    # POST here from any stage and be moved straight to CLOSE, skipping the
    # screening and the interview. FORM is the normal case; CLOSE is allowed
    # so a resubmission (or a late submit after a form-no-response archive)
    # still lands.
    if cand.get("stage") not in ("FORM", "CLOSE"):
        raise HTTPException(409, "The questionnaire opens after your interview.")
    responses = payload.get("responses") or {}
    if not isinstance(responses, dict):
        raise HTTPException(400, "responses must be an object")
    # Enrich each response with the question text so recruiters can read them
    # later even if the questionnaire definition changes.
    settings = await resolve_settings(cand["user_id"], cand.get("pipeline_id"))
    questions_by_id: Dict[str, str] = {}
    for q in (settings.get("custom_form") or {}).get("questions", []):
        if q.get("id"):
            questions_by_id[q["id"]] = q.get("question", "")
    enriched: Dict[str, Any] = {}
    for qid, ans in responses.items():
        enriched[qid] = {
            "question": questions_by_id.get(qid, ""),
            "answer": ans,
        }
    was_archived = bool(cand.get("archived_at"))
    chat = list(cand.get("chat_log") or [])
    chat.append({"role": "applicant", "text": "Submitted questionnaire.", "at": now_iso()})
    _now = now_iso()
    form_update: Dict[str, Any] = {
        "form_responses": enriched,
        "form_submitted_at": _now,
        "stage": "CLOSE",  # "TO CLOSE" — awaiting final 1-on-1
        "chat_log": chat,
        "updated_at": _now,
    }
    if not cand.get("moved_to_close_at"):
        form_update["moved_to_close_at"] = _now
    if was_archived:
        form_update["archived_at"] = None
        form_update["archived_reason"] = None
        logger.info(f"re-engagement: unarchived {cand['id']} via late form submission")
    await db.candidates.update_one(
        {"id": cand["id"]},
        {"$set": form_update},
    )
    try:
        from auto_dialer import cancel_form_reminder
        cancel_form_reminder(cand["id"])
    except Exception as _e:
        logger.warning(f"cancel_form_reminder on submit failed: {_e}")
    refreshed = await db.candidates.find_one({"id": cand["id"]}, {"_id": 0})
    return _public_view(refreshed)


@router.post("/public/applicant/{token}/phone")
async def public_confirm_phone(token: str, payload: Dict[str, Any] = Body(...)):
    """Candidate confirms — or corrects — the number we'll call for the 1-on-1.

    Recruiters were dialling numbers that never connected (mistyped at apply,
    or a number the candidate doesn't actually answer). The questionnaire card
    now shows the number on file and asks the candidate to vouch for it, so by
    the time a card reaches CLOSE the phone on it is one the candidate chose.

    Stage-gated like the questionnaire itself: FORM is the normal case, CLOSE
    covers the candidate who spots a wrong number after submitting. Unlike the
    typo-tolerant apply form, a correction here must normalize to a full E.164
    number (PHONE_DEFAULT_COUNTRY says how a local number is read) — this value
    feeds the dialler directly, and the candidate is
    looking at their own phone while typing it.
    """
    cand = await db.candidates.find_one({"public_token": token}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Application not found")
    if cand.get("stage") not in ("FORM", "CLOSE"):
        raise HTTPException(409, "Phone confirmation opens after your interview.")
    from deps import normalize_phone_e164
    from phone_util import is_e164
    submitted = str(payload.get("phone") or "").strip()
    normalized = normalize_phone_e164(submitted)
    if not is_e164(normalized) or (normalized.startswith("+1") and len(normalized) != 12):
        raise HTTPException(400, "Please enter a full phone number. If it isn't a local "
                                 "number, start with + and the country code.")
    current = normalize_phone_e164(cand.get("phone") or "")
    changed = normalized != current
    _now = now_iso()
    chat = list(cand.get("chat_log") or [])
    chat.append({
        "role": "applicant",
        "text": (
            f"Updated their 1-on-1 callback number: {current or '(none on file)'} → {normalized}."
            if changed else
            f"Confirmed {normalized} as their 1-on-1 callback number."
        ),
        "at": _now,
    })
    update: Dict[str, Any] = {
        "phone": normalized,
        "phone_confirmed_at": _now,
        "chat_log": chat,
        "updated_at": _now,
    }
    await db.candidates.update_one({"id": cand["id"]}, {"$set": update})
    if changed:
        logger.info(f"candidate {cand['id']} corrected callback number via portal")
    refreshed = await db.candidates.find_one({"id": cand["id"]}, {"_id": 0})
    return _public_view(refreshed)


# ===== Public Availability + AI Booking Tool Endpoints =====
@router.get("/public/availability/{slug}")
async def public_availability(slug: str, days: Optional[int] = None):
    """Public endpoint — returns available appointment slots for a pipeline.
    This is what the ElevenLabs `get_available_slots` tool calls during a screening call.

    Returns three arrays:
      • primary_slots  — first slots_primary slots; Olivia offers all of these first
      • fallback_slots — next slots_fallback slots; offered only if all primary rejected
      • slots          — the full computed list, for UIs that render a slot grid
                         (the applicant status portal). The voice agent must keep
                         using primary/fallback; reading `slots` would have it
                         reciting sixty times down the phone.
    """
    from availability_service import compute_available_slots, effective_window_days
    pipe = await db.pipelines.find_one({"public_slug": slug}, {"_id": 0})
    if not pipe:
        raise HTTPException(404, "Pipeline not found")
    settings = await resolve_settings(pipe["user_id"], pipe["id"])
    region = settings.get("region_language") or {}
    tz_name = region.get("timezone") or default_tz_name()
    appt_settings = settings.get("appointments") or {}
    booking_prefs = settings.get("booking_preferences") or {}
    # `appointments.booking_days_offered` is the booking window, and this is the
    # only door that enforces it: the recruiter's own slot picker and the CG1
    # direct-book stay uncapped, because a human overriding a slot is not the
    # behaviour being limited. Candidates booked 8+ days out attend far less often
    # than those booked inside a day, so a slot far enough away is worth less than no slot.
    window_days = int(appt_settings.get("booking_days_offered") or 0)
    if days is None:
        days = window_days if window_days > 0 else 60
    days = max(1, min(int(days), 90))  # `slots` returns the full list — keep the response bounded
    if window_days > 0:
        from datetime import datetime as _dt, timezone as _tz
        from zoneinfo import ZoneInfo as _ZI
        try:
            _now_local = _dt.now(_ZI(tz_name))
        except Exception:
            _now_local = _dt.now(_tz.utc)
        days = min(days, effective_window_days(pipe, window_days, _now_local))
    default_capacity = int(appt_settings.get("applicant_limit") or 50)
    n_primary = int(booking_prefs.get("slots_primary") or 2)
    n_fallback = int(booking_prefs.get("slots_fallback") or 1)
    booked = await db.candidates.find(
        {"pipeline_id": pipe["id"], "appointment_at": {"$ne": None}},
        {"_id": 0, "appointment_at": 1},
    ).to_list(2000)
    booked_isos = [c.get("appointment_at") for c in booked if c.get("appointment_at")]
    all_slots = compute_available_slots(
        pipe, booked_isos, days_ahead=days,
        tz_name=tz_name, default_capacity=default_capacity,
    )
    primary_slots = all_slots[:n_primary]
    fallback_slots = all_slots[n_primary:n_primary + n_fallback]
    return {
        "pipeline": pipe.get("name"),
        "duration_minutes": pipe.get("appointment_duration_minutes", 45),
        "timezone": tz_name,
        "primary_slots": primary_slots,
        "fallback_slots": fallback_slots,
        # Candidate-facing slot grids show the NEXT SIX openings, not a
        # fortnight of rota — sooner books better, and a wall of options
        # invites deferral. The voice/chat agents keep using primary/fallback.
        "slots": all_slots[:6],
    }


async def window_has_slots(pipeline: Dict[str, Any], settings: Dict[str, Any]) -> bool:
    """Is there anything bookable inside this pipeline's booking window right now?

    Shared by the `send_booking_link` door and the retry sweep so the two can
    never disagree about whether it is worth texting somebody a booking page.
    """
    from availability_service import compute_available_slots, effective_window_days
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI

    appt = settings.get("appointments") or {}
    tz_name = (settings.get("region_language") or {}).get("timezone") or default_tz_name()
    window_days = int(appt.get("booking_days_offered") or 0)
    try:
        now_local = _dt.now(_ZI(tz_name))
    except Exception:
        now_local = _dt.now(timezone.utc)
    days = (effective_window_days(pipeline, window_days, now_local)
            if window_days > 0 else 60)
    booked = await db.candidates.find(
        {"pipeline_id": pipeline["id"], "appointment_at": {"$ne": None}},
        {"_id": 0, "appointment_at": 1},
    ).to_list(2000)
    slots = compute_available_slots(
        pipeline, [b.get("appointment_at") for b in booked if b.get("appointment_at")],
        days_ahead=days, tz_name=tz_name,
        default_capacity=int(appt.get("applicant_limit") or 50),
    )
    return bool(slots)


# Voice-agent tool: only ElevenLabs (X-CGR-Tool-Secret) may call it.
@router.post("/public/send-booking-link", dependencies=[Depends(require_elevenlabs_tool_secret)])
async def public_send_booking_link(payload: Dict[str, Any] = Body(...)):
    """Text a candidate their own booking page. This is what the agents'
    `send_booking_link` tool calls when someone won't commit to a slot on the
    call, or when no slots are open yet.

    The prompt has promised this text for a long time and nothing ever sent it —
    `scheduling_link` existed only as prompt text, and `maybe_fire_screening_retry`
    only fires for calls that never connected, not for a candidate who heard the
    slots and declined. Handing those candidates to a human instead would just
    move the gap; this closes it, which is the point of the agents.

    Body: {phone: "+1...", pipeline_slug: "downtown"}
    """
    phone = (payload.get("phone") or "").strip()
    slug = (payload.get("pipeline_slug") or "").strip()
    if not phone:
        return {"ok": False, "sent": False,
                "message": "I don't have a number to send it to — confirm their best mobile."}

    pipe = await db.pipelines.find_one({"public_slug": slug}, {"_id": 0}) if slug else None
    query: Dict[str, Any] = {"phone": phone}
    if pipe:
        query["pipeline_id"] = pipe["id"]
    cand = await db.candidates.find_one(query, {"_id": 0}, sort=[("created_at", -1)])
    if not cand:
        # Same tolerance as the inbound matcher: the number may be stored in a
        # different shape than the one bound to this call.
        digits = "".join(ch for ch in phone if ch.isdigit())[-10:]
        if len(digits) == 10:
            loose: Dict[str, Any] = {"phone": {"$regex": r"\D*".join(digits)}}
            if pipe:
                loose["pipeline_id"] = pipe["id"]
            cand = await db.candidates.find_one(loose, {"_id": 0}, sort=[("created_at", -1)])
    if not cand:
        return {"ok": False, "sent": False,
                "message": "I couldn't find their record to send a link — don't promise the text."}
    if not cand.get("public_token"):
        return {"ok": False, "sent": False,
                "message": "There's no booking page for them yet — don't promise the text."}

    settings = await resolve_settings(cand["user_id"], cand.get("pipeline_id")) or {}

    # A link to a page with nothing on it is worse than no link: the candidate
    # opens it, finds an empty grid, and does not come back. If the window holds
    # nothing, park them until it has rolled far enough forward to contain a slot.
    if pipe and not await window_has_slots(pipe, settings):
        prefs = settings.get("booking_preferences") or {}
        wait_days = max(1, int(prefs.get("booking_retry_days") or 2))
        due = (datetime.now(timezone.utc) + timedelta(days=wait_days)).isoformat()
        await db.candidates.update_one(
            {"id": cand["id"]},
            {"$set": {"booking_retry_at": due, "booking_retry_sent_at": None,
                      "updated_at": now_iso()}},
        )
        logger.info(f"send-booking-link: no slots in window for {cand['id']} — parked until {due}")
        return {"ok": True, "sent": False, "parked": True,
                "message": (f"Nothing is open in the next few days, so I've not sent a link yet — "
                            f"tell them we'll text their booking page in about {wait_days} days, "
                            f"as soon as the next times open up. It will send automatically.")}

    base_url = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
    if not base_url:
        return {"ok": False, "sent": False,
                "message": "I can't build their booking link right now — don't promise the text."}
    link = f"{base_url}/applicant/{cand['public_token']}"

    profile = settings.get("recruiter_profile") or {}
    company = profile.get("company_name") or company_profile.company_name()
    first = cand.get("first_name") or "there"
    body = (f"Hi {first}, it's {company} — here's your booking page, pick whichever "
            f"interview time suits you: {link}")

    from sms_service import send_direct_sms
    res = await send_direct_sms(
        db, settings, cand.get("phone") or phone, body,
        pipeline=pipe, template_key="booking_link",
        user_id=cand.get("user_id", ""), candidate_id=cand.get("id", ""),
    )
    if res.get("status") == "sent":
        return {"ok": True, "sent": True,
                "message": "Sent — their booking link is on the way to this number."}
    if res.get("reason") == "recipient opted out":
        return {"ok": False, "sent": False,
                "message": "This number has opted out of our texts, so no link can go out — say the team will call them instead."}
    logger.warning(f"send-booking-link failed for {phone} ({slug}): {res}")
    return {"ok": False, "sent": False,
            "message": "The text didn't go through — say the team will call them back instead."}


# Voice-agent tool: only ElevenLabs (X-CGR-Tool-Secret) may call it.
@router.post("/public/register-caller", dependencies=[Depends(require_elevenlabs_tool_secret)])
async def public_register_caller(payload: Dict[str, Any] = Body(...)):
    """Put a caller who has never applied onto the pipeline, mid-call. This is
    what the inbound agent's `register_caller` tool calls.

    Someone ringing the office wanting a job used to be the one applicant the
    system had no door for: the front desk could only take a message, because
    every other intake path (portal, Indeed, CV upload) starts with a record and
    this one starts with a voice. Creating the record here is what lets the rest
    of the call behave like any other screening — the agent can screen them, and
    `book_slot` has something to book against.

    Deliberately NOT created on call connect: at that point we know only that a
    number rang us, and half of those are wrong numbers and cold sales. The
    record is created at the moment a human says they want to apply and gives a
    name, which is the same evidence the apply form asks for.

    Body: {phone, pipeline_slug, first_name, last_name?, email?}
    """
    phone      = (payload.get("phone") or "").strip()
    slug       = (payload.get("pipeline_slug") or "").strip()
    first_name = (payload.get("first_name") or "").strip()
    last_name  = (payload.get("last_name") or "").strip()
    email      = (payload.get("email") or "").strip().lower()

    if not (phone and first_name):
        return {"ok": False, "registered": False,
                "message": "I still need their first name and a number before I can register them."}
    pipe = await db.pipelines.find_one({"public_slug": slug}, {"_id": 0}) if slug else None
    if not pipe:
        return {"ok": False, "registered": False,
                "message": "I couldn't reach the system to register them — take their details and tell them the team will call back."}

    # Same guard the portal and every other intake uses, so a caller ringing from
    # a number already on the pipeline resumes their record instead of forking it.
    from deps import find_duplicate_candidate
    dup = await find_duplicate_candidate(pipe["id"], email, phone)
    if dup:
        return {"ok": True, "registered": True, "already_known": True,
                "candidate_id": dup["id"],
                "message": ("They're already on the system — carry on with the screening, "
                            "and you can book them at the end."),
                "screening": "proceed"}

    cand = Candidate(
        user_id=pipe["user_id"],
        pipeline_id=pipe["id"],
        first_name=first_name,
        last_name=last_name,
        email=email,
        phone=phone,
        stage="SCREENING",
        screening_status="in_progress",
    )
    doc = cand.model_dump()
    # Set on the doc, not the model: Candidate is `extra="ignore"`, so passing
    # source= to the constructor drops it silently.
    doc["source"] = "inbound_call"
    doc["chat_log"] = [{
        "role": "agent",
        "text": f"{first_name} called the office line and asked to apply. Screening started on that call.",
        "at": now_iso(),
    }]
    await db.candidates.insert_one(doc)
    logger.info(f"register-caller: created {cand.id} ({first_name} {last_name}) "
                f"from inbound call on {phone} in pipeline {pipe['id']}")

    # No start_screening here on purpose — that queues an outbound call and the
    # warmup comms, and the candidate is on the phone right now. The agent screens
    # them on this call; the post-call handler writes up the transcript and picks
    # up the retry if the line drops.
    return {"ok": True, "registered": True, "already_known": False,
            "candidate_id": cand.id,
            "message": "They're on the system now — go ahead and run the screening questions.",
            "screening": "proceed"}


# Voice-agent tool: only ElevenLabs (X-CGR-Tool-Secret) may call it.
@router.post("/public/send-office-details", dependencies=[Depends(require_elevenlabs_tool_secret)])
async def public_send_office_details(payload: Dict[str, Any] = Body(...)):
    """Text the office address + Google Maps link to a caller. This is what the
    inbound agent's `send_office_details` tool calls.

    Works for a caller with no candidate record — the person most likely to ask
    where the office is has often never applied. Opt-out and the first-message
    STOP clause are still enforced (see `sms_service.send_direct_sms`).

    Deliberately returns 200 on every failure with `sent: false` and a spoken
    `message`. The agent calls this mid-conversation having just told the caller
    the text is on its way; an HTTP error would leave it improvising, whereas a
    sentence it can read out keeps the call honest.

    Body: {phone: "+1...", pipeline_slug: "downtown"}
    """
    phone = (payload.get("phone") or "").strip()
    slug = (payload.get("pipeline_slug") or "").strip()
    if not phone:
        return {"ok": False, "sent": False,
                "message": "I couldn't get a number to text — ask the caller for the best mobile, or read the address out."}

    pipe = await db.pipelines.find_one({"public_slug": slug}, {"_id": 0}) if slug else None
    if not pipe:
        return {"ok": False, "sent": False,
                "message": "I couldn't look up the office to text — read the address out instead."}

    settings = await resolve_settings(pipe["user_id"], pipe["id"]) or {}
    from routes.inbound import _resolve_office_context
    ctx = await _resolve_office_context(pipe["user_id"], pipe, settings)
    address = ctx["office_address"]
    maps_link = ctx["maps_link"]
    if not (address or maps_link):
        return {"ok": False, "sent": False,
                "message": "I don't have the office address on file to text — tell the caller it's in their confirmation email."}

    profile = settings.get("recruiter_profile") or {}
    company = profile.get("company_name") or company_profile.company_name()
    lines = [f"{company} — {pipe.get('name') or 'our office'}"]
    if address:
        lines.append(address)
    if maps_link:
        lines.append(f"Directions: {maps_link}")

    from sms_service import send_direct_sms
    res = await send_direct_sms(
        db, settings, phone, "\n".join(lines),
        pipeline=pipe, template_key="office_details", user_id=pipe["user_id"],
    )
    status = res.get("status")
    if status == "sent":
        return {"ok": True, "sent": True,
                "message": "Sent — the address and Maps link are on their way to this number."}
    if res.get("reason") == "recipient opted out":
        # Not a fault to apologise for, but the agent must not claim it sent.
        return {"ok": False, "sent": False,
                "message": "This number has opted out of our texts, so I can't send it — read the address out instead."}
    logger.warning(f"send-office-details failed for {phone} ({slug}): {res}")
    return {"ok": False, "sent": False,
            "message": "The text didn't go through — read the address out to the caller instead."}


# Voice-agent tool: only ElevenLabs (X-CGR-Tool-Secret) may call it.
@router.post("/public/book-by-phone", dependencies=[Depends(require_elevenlabs_tool_secret)])
async def public_book_by_phone(payload: Dict[str, Any] = Body(...)):
    """Public endpoint — books a slot for a candidate identified by phone number.
    This is what the ElevenLabs `book_slot` tool calls when the candidate confirms a slot mid-call.
    Body: {phone: "+1...", slot_iso: "2026-05-08T13:00:00Z", pipeline_slug: "downtown"}"""
    phone = (payload.get("phone") or "").strip()
    slot_iso = (payload.get("slot_iso") or "").strip()
    slug = (payload.get("pipeline_slug") or "").strip()
    if not (phone and slot_iso):
        raise HTTPException(400, "phone and slot_iso required")
    query: Dict[str, Any] = {"phone": phone}
    if slug:
        pipe = await db.pipelines.find_one({"public_slug": slug}, {"_id": 0})
        if pipe:
            query["pipeline_id"] = pipe["id"]
    cand = await db.candidates.find_one(query, {"_id": 0}, sort=[("created_at", -1)])
    if not cand:
        raise HTTPException(404, f"No candidate found for phone {phone}")
    pipe = await db.pipelines.find_one({"id": cand["pipeline_id"]}, {"_id": 0})
    settings = await resolve_settings(cand["user_id"], cand.get("pipeline_id"))
    profile = (settings.get("recruiter_profile") or {})
    company = profile.get("company_name") or company_profile.company_name()

    # Capacity check: refuse if the slot is already full.
    if pipe:
        from availability_service import slot_capacity_remaining
        appt_settings = settings.get("appointments") or {}
        default_cap = int(appt_settings.get("applicant_limit") or 50)
        booked = await db.candidates.find(
            {"pipeline_id": pipe["id"], "appointment_at": slot_iso, "id": {"$ne": cand["id"]}},
            {"_id": 0, "appointment_at": 1},
        ).to_list(200)
        region = settings.get("region_language") or {}
        tz_name = region.get("timezone") or default_tz_name()
        remaining = slot_capacity_remaining(pipe, [b.get("appointment_at") for b in booked], slot_iso, default_capacity=default_cap, tz_name=tz_name)
        if remaining == -1:
            raise HTTPException(409, f"Slot {slot_iso} is not in the availability schedule")
        if remaining is not None and remaining <= 0:
            raise HTTPException(409, f"Slot {slot_iso} is full")

    from availability_service import resolve_slot_recruiter
    from deps import booking_link_or_alert
    _tz = (settings.get("region_language") or {}).get("timezone") or default_tz_name()
    update = {
        "stage": "APPOINTMENT",
        "screening_status": "approved",
        "appointment_at": slot_iso,
        # Empty when unconfigured — never a placeholder URL (the old
        # made-up meeting-URL fallback sent candidates to an invalid-meeting
        # error at interview minute). booking_link_or_alert bells the recruiter.
        "appointment_link": await booking_link_or_alert(pipe or {}, slot_iso, _tz, cand),
        "appointment_recruiter": resolve_slot_recruiter(pipe or {}, slot_iso, _tz) or (pipe or {}).get("appointment_recruiter") or "Hiring Team",
        "archived_at": None,
        "archived_reason": None,
        "updated_at": now_iso(),
    }
    # Rebooking a no-show / archived candidate: clear the no-show flag and track
    # the missed slot, mirroring the reschedule portal (routes/attendance.py) so
    # the candidate returns to the kanban clean and the confirmation chaser
    # re-fires for the NEW slot.
    prev_at = cand.get("appointment_at")
    if prev_at and prev_at != slot_iso and (cand.get("attendance_status") == "no_show" or cand.get("archived_at")):
        update["previous_appointment_at"] = prev_at
        update["attendance_status"] = None
        update["rescheduled"] = True
        update["appointment_sms_confirmed"] = None
        update["appointment_sms_confirmed_at"] = None
    # Revival-chased candidate converting through any booking path is a
    # revival win — final outcome stops further revival calls.
    if int(cand.get("revival_call_attempts") or 0) > 0 and cand.get("revival_outcome") != "rebooked":
        update["revival_outcome"] = "rebooked"
    chat = list(cand.get("chat_log") or [])
    chat.append({
        "role": "agent",
        "text": f"Booked an interview slot with {company} on {slot_iso}.",
        "at": now_iso(),
    })
    update["chat_log"] = chat
    await db.candidates.update_one({"id": cand["id"]}, {"$set": update})
    refreshed = await db.candidates.find_one({"id": cand["id"]}, {"_id": 0})
    try:
        await send_stage_email(user_id=cand["user_id"], candidate=refreshed, template_key="approval")
    except Exception as e:
        logger.warning(f"approval email failed: {e}")
    if refreshed.get("phone"):
        try:
            from email_service import build_template_vars, render_template, get_template_for_key
            from sms_service import send_candidate_sms
            _settings = await resolve_settings(cand["user_id"], cand.get("pipeline_id"))
            live_tpl = get_template_for_key(_settings, "approval") or {}
            if not (live_tpl.get("sms_enabled") is False or live_tpl.get("enabled") is False):
                sms_body_tpl = live_tpl.get("sms_body") or ""
                if sms_body_tpl:
                    job = await db.jobs.find_one({"id": refreshed["job_id"]}, {"_id": 0}) if refreshed.get("job_id") else None
                    body = render_template(sms_body_tpl, build_template_vars(refreshed, _settings, job))
                    await send_candidate_sms(db, _settings, refreshed, body, template_key="approval")
        except Exception as e:
            logger.warning(f"approval sms (phone booking) failed: {e}")
    # Schedule the 1h email + 10m email + 10m sms reminders.
    try:
        from auto_dialer import schedule_appointment_reminders, cancel_pending_retry_calls
        await schedule_appointment_reminders(cand["user_id"], cand["id"], slot_iso)
        # Cancel any auto-retry calls queued for this candidate — they've now booked.
        cancel_pending_retry_calls(cand["id"])
    except Exception as e:
        logger.warning(f"reminder scheduling failed: {e}")
    from screening_outcome import ensure_screening_outcome_later
    # Delayed: this door fires MID-call (the voice agent books while still
    # talking) — give the call time to end and its transcript time to sync.
    ensure_screening_outcome_later(db, cand["id"], delay_seconds=900)
    return {"ok": True, "candidate_id": cand["id"], "slot_iso": slot_iso, "stage": "APPOINTMENT"}


# Voice-agent tool: only ElevenLabs (X-CGR-Tool-Secret) may call it.
@router.post("/public/reschedule-start-by-phone", dependencies=[Depends(require_elevenlabs_tool_secret)])
async def public_reschedule_start_by_phone(payload: Dict[str, Any] = Body(...)):
    """Change a new starter's TRAINING start date by phone — this is what the
    inbound `reschedule_start_date` voice tool calls. Mirrors the SMS start-date
    reschedule: updates training_start_at (keeping the usual start time), marks it
    rescheduled, and clears the confirmation stamps so the pre-start confirmation
    re-fires for the new date. Reflected on the platform (roster/calendar/drawer).

    Body: {phone, new_start_date: "YYYY-MM-DD", pipeline_slug}"""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    phone = (payload.get("phone") or "").strip()
    new_date = (payload.get("new_start_date") or "").strip()[:10]
    slug = (payload.get("pipeline_slug") or "").strip()
    if not (phone and new_date):
        raise HTTPException(400, "phone and new_start_date required")

    query: Dict[str, Any] = {"phone": phone}
    if slug:
        pipe = await db.pipelines.find_one({"public_slug": slug}, {"_id": 0})
        if pipe:
            query["pipeline_id"] = pipe["id"]
    cand = await db.candidates.find_one(query, {"_id": 0}, sort=[("updated_at", -1)])
    if not cand:
        raise HTTPException(404, f"No candidate found for phone {phone}")

    # Only makes sense for someone booked to start (TRAINING with a start date).
    stage = (cand.get("stage") or "").upper()
    if stage != "TRAINING" or not cand.get("training_start_at"):
        raise HTTPException(409, "This caller isn't booked to start, so there's no start date to change.")

    ET = app_zone()
    # Parse the requested date and keep their existing start time-of-day.
    try:
        d = datetime.strptime(new_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "new_start_date must be YYYY-MM-DD")
    existing = cand.get("training_start_at") or ""
    hh, mm, ss = 9, 0, 0
    try:
        s = existing.replace("Z", "").split("+")[0].split(".")[0]
        et = datetime.fromisoformat(s)
        hh, mm, ss = et.hour, et.minute, et.second
    except Exception:
        pass
    new_dt = d.replace(hour=hh, minute=mm, second=ss)
    # Reject dates in the past (allow today).
    if new_dt.date() < datetime.now(ET).date():
        raise HTTPException(400, "That start date is in the past — please pick an upcoming date.")
    new_iso = new_dt.isoformat()

    await db.candidates.update_one(
        {"id": cand["id"]},
        {
            "$set": {
                "training_start_at": new_iso,
                "sms_confirmation_status": "rescheduled",
                "sms_rescheduled_at": now_iso(),
                "sms_reschedule_count": int(cand.get("sms_reschedule_count") or 0) + 1,
                "updated_at": now_iso(),
            },
            "$unset": {"sms_confirmation_sent_at": "", "sms_confirmed_at": ""},
        },
    )

    # Notify a human that a start date moved (parity with the SMS flow's visibility).
    try:
        from notifications_service import create_notification
        nm = f"{cand.get('first_name', '')} {cand.get('last_name', '')}".strip() or "A starter"
        await create_notification(
            cand["user_id"], "start.rescheduled",
            f"📅 {nm} moved their start date (by phone)",
            body=f"New start date: {new_dt.strftime('%A, %b %-d')}",
            link=f"/inbox?candidate={cand['id']}",
            candidate_id=cand["id"], pipeline_id=cand.get("pipeline_id"),
        )
    except Exception as e:
        logger.warning(f"start-reschedule notification failed: {e}")

    # No CG1 page for reschedules, single or repeat (owner's rule, 2026-08-18):
    # a reschedule is a status change, not trainee correspondence.

    label = new_dt.strftime("%A, %B %-d")
    return {"ok": True, "candidate_id": cand["id"], "new_start_date": new_date,
            "new_start_label": label, "first_name": cand.get("first_name") or ""}


@router.post("/public/applicant/{token}/reschedule")
async def public_reschedule_appointment(token: str, payload: Dict[str, Any] = Body(...)):
    """Applicant changes their existing appointment slot. Same body as /book."""
    cand = await db.candidates.find_one({"public_token": token}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Application not found")
    new_at = (payload.get("appointment_at") or "").strip()
    if not new_at:
        raise HTTPException(400, "appointment_at required")
    settings = await resolve_settings(cand["user_id"], cand.get("pipeline_id"))
    profile = (settings.get("recruiter_profile") or {})
    company = profile.get("company_name") or company_profile.company_name()
    pipeline = await db.pipelines.find_one({"id": cand.get("pipeline_id"), "user_id": cand["user_id"]}, {"_id": 0}) or {}
    await _validate_portal_slot(cand, pipeline, settings, new_at)
    prev_at = cand.get("appointment_at")
    # Slot override first: moving to a slot that declares its own link must swap
    # the candidate onto it — their stored link belongs to the OLD slot.
    from availability_service import resolve_slot_link, resolve_slot_recruiter
    _tz = (settings.get("region_language") or {}).get("timezone") or default_tz_name()
    # NB: never fall back to the candidate's stored link — that is the OLD slot's
    # override and would follow them onto a slot that has none.
    resolved_link = resolve_slot_link(pipeline, new_at, _tz) or pipeline.get("appointment_link") or ""
    was_archived = bool(cand.get("archived_at"))
    update = {
        "stage": "APPOINTMENT",
        "appointment_at": new_at,
        "appointment_link": resolved_link,
        "rescheduled": True,
        "rescheduled_at": now_iso(),
        "updated_at": now_iso(),
        "appointment_sms_confirmed": None,      # reset so chaser fires for the new slot
        "appointment_sms_confirmed_at": None,
        # Clear any "can't make it" flag from the OLD slot — the reminder engine
        # skips every send while it is set, so a cancel-then-rebook through this
        # door was attending with zero reminders (mirrors training_sms.py).
        "appointment_cancelled_at": None,
        "appointment_cancel_reason": None,
    }
    if was_archived:
        update["archived_at"] = None
        update["archived_reason"] = None
        logger.info(f"re-engagement: unarchived {cand['id']} via self-reschedule portal")
    # Rebooking after a no-show: clear the stale flag so the new appointment
    # starts clean (mirrors the /book path). Track the missed slot for audit.
    # preserve_attendance (merged into the write below) keeps the old answer in
    # attendance_history instead of destroying it — the two-strike cap and the
    # outcome reports only see history, and this door was erasing it.
    if cand.get("attendance_status") == "no_show":
        update["attendance_status"] = None
        update["previous_appointment_at"] = prev_at
    # If the revival dialer had been chasing this candidate, the emailed link
    # converting counts as a revival win — final outcome stops further calls.
    if int(cand.get("revival_call_attempts") or 0) > 0 and cand.get("revival_outcome") != "rebooked":
        update["revival_outcome"] = "rebooked"
    chat = list(cand.get("chat_log") or [])
    chat.append({
        "role": "applicant",
        "text": f"Rescheduled appointment from {prev_at or 'unset'} to {new_at}.",
        "at": now_iso(),
    })
    _join_line = f"\nJoin: {resolved_link}" if resolved_link else ""
    chat.append({
        "role": "agent",
        "text": f"Your appointment with {company} has been rescheduled.\nNew Date/Time: {new_at}{_join_line}",
        "at": now_iso(),
    })
    update["chat_log"] = chat
    from screening_outcome import preserve_attendance
    _write: Dict[str, Any] = {"$set": update}
    _keep = preserve_attendance(cand)
    if _keep:
        _write["$push"] = _keep
    await db.candidates.update_one({"id": cand["id"]}, _write)
    refreshed = await db.candidates.find_one({"id": cand["id"]}, {"_id": 0})
    # Pick `appointment_rescheduled` if defined+enabled in this pipeline's
    # templates, else fall back to `approval` (the actively-fired booking
    # template). The dedicated `confirmation` key was retired in v34.5.
    tpl_key = "appointment_rescheduled"
    tpl_def = (settings.get("applicant_comms") or {}).get("templates", {}).get(tpl_key) or {}
    if not tpl_def.get("body") or tpl_def.get("enabled") is False:
        tpl_key = "approval"
    try:
        await send_stage_email(user_id=cand["user_id"], candidate=refreshed, template_key=tpl_key)
    except Exception as e:
        logger.warning(f"reschedule confirmation email failed: {e}")
    # SMS — mirrors the recruiter-side /reschedule + no-show reschedule. Sends
    # from the per-pipeline outbound number if SMS channel is on.
    if refreshed.get("phone"):
        try:
            from email_service import build_template_vars, render_template, get_template_for_key
            from sms_service import send_candidate_sms
            live_tpl = get_template_for_key(settings, tpl_key) or {}
            if not (live_tpl.get("sms_enabled") is False or live_tpl.get("enabled") is False):
                sms_body_tpl = live_tpl.get("sms_body") or ""
                if sms_body_tpl:
                    job = None
                    if refreshed.get("job_id"):
                        job = await db.jobs.find_one({"id": refreshed["job_id"]}, {"_id": 0})
                    body = render_template(sms_body_tpl, build_template_vars(refreshed, settings, job))
                    await send_candidate_sms(db, settings, refreshed, body, template_key=tpl_key)
        except Exception as e:
            logger.warning(f"reschedule confirmation sms failed: {e}")
    # Cancel old reminders, schedule fresh ones for the new slot. Also drop any
    # queued retry calls — same as the /book sibling; a candidate who just moved
    # their own interview should not still get dialled about booking one.
    try:
        from auto_dialer import (
            cancel_appointment_reminders, schedule_appointment_reminders,
            cancel_pending_retry_calls,
        )
        cancel_appointment_reminders(cand["id"])
        await schedule_appointment_reminders(cand["user_id"], cand["id"], new_at)
        cancel_pending_retry_calls(cand["id"])
    except Exception as e:
        logger.warning(f"reminder reschedule failed: {e}")
    return _public_view(refreshed)
