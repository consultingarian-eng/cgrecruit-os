"""
Inbound email reply handler for CGRecruit.
SendGrid Inbound Parse POSTs to POST /webhooks/sendgrid/inbound-email.
Candidate is matched by reply+{candidate_id}@<EMAIL_REPLY_DOMAIN> in the To header.
AI (ANTHROPIC_MODEL, see llm_config.py) generates context-aware replies and can take actions:
  - cancel   → candidate expressed disinterest; mark email_reply_status=declined
  - reschedule → candidate wants a new appointment slot; store proposed date
  - none     → just a reply / question answered
"""
import os
import re
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request

from deps import current_user, resolve_settings

logger = logging.getLogger(__name__)
router = APIRouter()

# Domain of the per-candidate reply+{id}@ addresses (EMAIL_REPLY_DOMAIN). Empty
# means reply routing is off.
REPLY_DOMAIN = os.environ.get("EMAIL_REPLY_DOMAIN", "")
# Anthropic key. ANTHROPIC_API_KEY is the name to use; EMERGENT_LLM_KEY is the
# legacy name and still read as a fallback.
EMERGENT_LLM_KEY = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("EMERGENT_LLM_KEY", "")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_candidate_id(header: str) -> Optional[str]:
    """Pull candidate_id out of reply+{id}@<EMAIL_REPLY_DOMAIN> anywhere in the string.
    Reply routing is off (always None) when EMAIL_REPLY_DOMAIN is unset — an
    empty domain would otherwise match reply+<id>@ at ANY domain."""
    if not REPLY_DOMAIN:
        return None
    m = re.search(r"reply\+([^@\s<>]+)@" + re.escape(REPLY_DOMAIN), header or "", re.IGNORECASE)
    return m.group(1) if m else None


def _extract_email_address(raw: str) -> str:
    """Strip display name from 'Display Name <email@domain.com>' → 'email@domain.com'.
    If no angle brackets, returns the raw string stripped."""
    if not raw:
        return ""
    m = re.search(r"<([^>]+)>", raw)
    return m.group(1).strip() if m else raw.strip()


def _strip_reply_quotes(body: str) -> str:
    """Remove quoted previous message from a plain-text email reply."""
    if not body:
        return ""
    lines = body.splitlines()
    clean: List[str] = []
    for line in lines:
        stripped = line.strip()
        if re.match(r"^On .+ wrote:$", stripped):
            break
        if stripped.startswith(">"):
            break
        if re.match(r"^-{3,}.*[Oo]riginal [Mm]essage.*-{3,}", stripped):
            break
        if re.match(r"^From:\s+.+@.+", stripped):
            break
        clean.append(line)
    return "\n".join(clean).strip()


STAGE_GOALS = {
    "APPLICANT": (
        "The candidate has just applied. Answer questions about the role and encourage "
        "them to complete their screening call. If they'd rather chat online, get an instant "
        "callback, or pick a different call time, you can make that happen yourself — see "
        "SCREENING SELF-SERVE OPTIONS below."
    ),
    "SCREENING": (
        "The candidate is in the screening/interview stage. Help with questions about "
        "the interview process, timing, or role. If they'd rather chat online, get an instant "
        "callback, or pick a different call time, you can make that happen yourself — see "
        "SCREENING SELF-SERVE OPTIONS below."
    ),
    "APPOINTMENT": (
        "The candidate has an interview booked. Confirm they're coming, help with "
        "questions. If they want to reschedule, send them the reschedule link from context — "
        "do not suggest specific dates yourself."
    ),
    "FORM": (
        "The candidate needs to complete a form. Encourage them and answer any questions "
        "about it."
    ),
    "CLOSE": (
        "The candidate is in the final assessment stage — their closing call is happening or imminent. "
        "They have NOT yet been booked to start. Answer questions about the role, process, or next "
        "steps. Keep them engaged and positive."
    ),
    "TRAINING": (
        "The candidate has been successfully assessed, booked to start, and given a confirmed start date. "
        "Confirm attendance, handle last-minute concerns, answer logistics questions (location, dress "
        "code, what to bring, schedule). If they cannot attend, the ONLY alternative to offer is the "
        "FOLLOWING Monday at the same start time — no other days or times. "
        "If they decline that too, be empathetic and wish them well."
    ),
}

# TRAINING goals for a start date that has already passed (the SMS agent's
# twin lives in training_sms — see _start_is_stale there for the incident).
# The goal above chases attendance for a date that is gone; a lapsed no-show
# emailing back weeks later must be re-engaged, not asked to confirm history.
# Unlike SMS there is no next-Monday booking machinery on this channel, so the
# model names the fresh-start Monday and defers the actual booking to a human.
STAGE_GOAL_TRAINING_STALE_UNCONFIRMED = (
    "This candidate was booked to start, but their scheduled start date has ALREADY PASSED "
    "and they never confirmed it — they most likely did not attend. Do NOT ask them to "
    "confirm attendance for that old date, and never imply it is still ahead. Re-engage: "
    "respond to what they wrote, find out whether they're still interested in the role, and "
    "if they are, let them know the next intake is the NEXT MONDAY date from context and "
    "that a member of the team will confirm the details shortly."
)
STAGE_GOAL_TRAINING_STALE_CONFIRMED = (
    "This candidate confirmed a start date that has now passed — they most likely attended "
    "and may already be working. Do NOT ask them to confirm attendance again. Answer their "
    "questions helpfully; only if they say they missed their start day, mention the NEXT "
    "MONDAY date from context as the only alternative and say the team will confirm details."
)

# Email twin of training_sms.STAGE_GOAL_TRAINING_PRE_WINDOW — before the
# day-of confirmation process begins, answering an early question must not
# turn into an attendance chase.
STAGE_GOAL_TRAINING_PRE_WINDOW = (
    "The candidate is booked to start on the training date in context, which is still ahead. "
    "The attendance-confirmation process has NOT started yet — we ask them to confirm on the "
    "morning of their start day, never before. Answer their questions warmly and completely, "
    "and do NOT ask them to confirm attendance or push for a YES. If they spontaneously say "
    "they'll be there, acknowledge warmly and end with ACTION: none — the formal confirmation "
    "request reaches them on the morning itself."
)

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

async def _get_thread(db, candidate_id: str) -> List[Dict]:
    return await db.email_reply_messages.find(
        {"candidate_id": candidate_id}, {"_id": 0}
    ).sort("sent_at", 1).to_list(200)


async def _fetch_enrichment(db, candidate: Dict) -> Dict:
    enrichment: Dict[str, Any] = {}
    cid = candidate["id"]

    try:
        call = await db.calls.find_one(
            {"candidate_id": cid},
            {"_id": 0, "summary": 1, "transcript": 1, "outcome": 1},
            sort=[("created_at", -1)],
        )
        if call:
            enrichment["screening_summary"] = call.get("summary", "")
            tail = (call.get("transcript") or "")[-2000:]
            enrichment["screening_transcript_tail"] = tail
            enrichment["screening_outcome"] = call.get("outcome", "")
    except Exception:
        pass

    try:
        form = await db.form_responses.find_one({"candidate_id": cid}, {"_id": 0, "responses": 1})
        if form:
            enrichment["form_responses"] = form.get("responses", {})
    except Exception:
        pass

    try:
        if candidate.get("job_id"):
            job = await db.jobs.find_one(
                {"id": candidate["job_id"]},
                {"_id": 0, "title": 1, "description": 1, "city": 1},
            )
            if job:
                enrichment["job_title"] = job.get("title", "")
                enrichment["job_description"] = (job.get("description") or "")[:1500]
                enrichment["job_city"] = job.get("city", "")
    except Exception:
        pass

    try:
        comms = await db.communications.find(
            {"candidate_id": cid},
            {"_id": 0, "type_": 1, "template_key": 1, "subject": 1, "body": 1, "sent_at": 1},
        ).sort("sent_at", -1).to_list(10)
        enrichment["recent_comms"] = list(reversed(comms))
    except Exception:
        pass

    try:
        user_id = candidate.get("user_id") or ""
        pipeline_id = candidate.get("pipeline_id") or ""
        settings_doc = await db.settings.find_one(
            {"user_id": user_id, "pipeline_id": pipeline_id},
            {"_id": 0, "ai_stage_prompts": 1, "starter_template": 1},
        )
        enrichment["settings"] = settings_doc or {}
    except Exception:
        enrichment["settings"] = {}

    return enrichment


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

def _build_context_block(candidate: Dict, pipeline: Dict, enrichment: Dict) -> str:
    profile = (pipeline.get("recruiter_profile") or {})
    company = profile.get("company_name") or "our company"
    address = profile.get("company_address") or ""
    city = profile.get("city") or enrichment.get("job_city") or ""
    job_title = enrichment.get("job_title") or profile.get("job_role") or "the role"
    stage = candidate.get("stage", "UNKNOWN")
    appt_at = candidate.get("appointment_at") or ""
    training_at = candidate.get("training_start_at") or ""
    base_url = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
    reschedule_link = f"{base_url}/reschedule/{candidate['public_token']}" if candidate.get("public_token") else ""

    lines = [
        f"CANDIDATE: {candidate.get('first_name','')} {candidate.get('last_name','')}",
        f"EMAIL: {candidate.get('email','')}",
        f"STAGE: {stage}",
        f"COMPANY: {company}",
        f"ROLE: {job_title}",
    ]
    if city:
        lines.append(f"LOCATION: {city}")
    if address:
        lines.append(f"ADDRESS: {address}")
    if appt_at:
        lines.append(f"INTERVIEW BOOKED: {appt_at}")
    if training_at:
        from training_sms import _next_monday_iso, _start_is_stale
        if _start_is_stale(candidate):
            lines.append(f"TRAINING START: {training_at} — THIS DATE HAS ALREADY PASSED, do not present it as upcoming")
            lines.append(f"NEXT MONDAY (the only fresh-start date to mention if they're still interested): {_next_monday_iso(None)}")
        else:
            lines.append(f"TRAINING START: {training_at}")
    if reschedule_link and stage == "APPOINTMENT":
        lines.append(f"RESCHEDULE LINK: {reschedule_link}")

    # Pre-screening self-serve options — APPLICANT/SCREENING candidates are
    # exactly the ones in the warmup-to-call window (see routes/retry.py).
    # `pipeline` here is the resolved settings doc, so auto_dialer/region_language
    # are already the right per-pipeline values for computing callback windows.
    if stage in ("APPLICANT", "SCREENING") and base_url and candidate.get("public_token"):
        token = candidate["public_token"]
        lines.append(f"CHAT LINK (send verbatim if they'd rather finish screening online): {base_url}/retry/{token}")
        lines.append(f"PICK-A-TIME LINK (send verbatim if they want to browse every callback option themselves): {base_url}/retry/{token}?tab=schedule")
        try:
            from routes.retry import _compute_call_time_options
            options = _compute_call_time_options(pipeline or {})
            if options:
                lines.append("AVAILABLE CALLBACK TIMES (the ONLY times you can offer for ACTION: schedule_call — copy the datetime EXACTLY):")
                for o in options:
                    lines.append(f"  - {o['label']} [{o['iso']}]")
        except Exception:
            pass

    snap = candidate.get("starter_email_snapshot") or {}
    if snap:
        if snap.get("monday_start"):
            lines.append(f"DAY 1: {snap['monday_start']} – {snap.get('monday_end','')}")
        if snap.get("tuesday_start"):
            lines.append(f"DAY 2: {snap['tuesday_start']} – {snap.get('tuesday_end','')}")
        if snap.get("regular_schedule"):
            lines.append(f"REGULAR SCHEDULE: {snap['regular_schedule']}")
        if snap.get("dress_code"):
            lines.append(f"DRESS CODE: {snap['dress_code']}")
        if snap.get("parking_text"):
            lines.append(f"PARKING: {snap['parking_text']}")

    if enrichment.get("screening_summary"):
        lines.append(f"\nSCREENING CALL SUMMARY:\n{enrichment['screening_summary']}")
    if enrichment.get("form_responses"):
        lines.append(f"\nFORM RESPONSES:\n{enrichment['form_responses']}")
    if enrichment.get("job_description"):
        lines.append(f"\nJOB DESCRIPTION (excerpt):\n{enrichment['job_description'][:800]}")

    comms = enrichment.get("recent_comms") or []
    if comms:
        lines.append("\nRECENT PLATFORM EMAILS TO CANDIDATE:")
        for c in comms[-5:]:
            lines.append(f"  [{(c.get('sent_at') or '')[:10]}] {c.get('template_key','')} — {c.get('subject','')}")

    return "\n".join(lines)


def _build_system_prompt(candidate: Dict, pipeline: Dict, enrichment: Dict) -> str:
    stage = candidate.get("stage", "TRAINING")
    # Per-pipeline override; fall back to hardcoded default if blank.
    # APPLICANT and SCREENING share the same prompt.
    _ai_prompts = (enrichment.get("settings") or {}).get("ai_stage_prompts") or {}
    _prompt_stage = "SCREENING" if stage == "APPLICANT" else stage
    _email_override = (_ai_prompts.get(_prompt_stage) or {}).get("email") or ""
    stage_goal = _email_override.strip() if _email_override.strip() else STAGE_GOALS.get(stage, "Help the candidate.")
    # A passed start date invalidates the TRAINING goal — including a
    # recruiter-written override, which assumes the start is still ahead.
    if stage == "TRAINING":
        from training_sms import _confirm_process_open, _start_is_stale
        if _start_is_stale(candidate):
            stage_goal = (
                STAGE_GOAL_TRAINING_STALE_CONFIRMED
                if candidate.get("sms_confirmation_status") == "confirmed"
                else STAGE_GOAL_TRAINING_STALE_UNCONFIRMED
            )
        elif not _confirm_process_open(candidate):
            # Same swap ahead of the confirmation window: the goal above (and
            # any recruiter override) opens with "Confirm attendance", which
            # is only true from the morning of the start day.
            stage_goal = STAGE_GOAL_TRAINING_PRE_WINDOW
    profile = (pipeline.get("recruiter_profile") or {})
    company = profile.get("company_name") or "our company"
    recruiter_name = profile.get("recruiter_name") or "The Hiring Team"
    custom_instructions = (pipeline.get("starter_template") or {}).get("confirmation_sms_instructions") or ""
    base_url = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")

    ctx = _build_context_block(candidate, pipeline, enrichment)

    prompt = f"""You are a recruitment assistant replying to emails from job candidates on behalf of {company}.

CURRENT STAGE GOAL: {stage_goal}

CANDIDATE CONTEXT:
{ctx}

REPLY GUIDELINES:
- Be warm, professional, and concise (2–4 sentences max unless the question requires more)
- Always address them by first name
- If they want to withdraw or are not interested: acknowledge gracefully, say we've noted it. End your reply with a blank line then: ACTION: cancel
- If they want to reschedule their interview or training: send them the reschedule link from the context if available, otherwise say you've noted it and a rebooking link will follow by text and email. Do NOT suggest specific dates, and do NOT promise that a person will contact them. End with ACTION: none
- If they confirm they are attending their interview or training (e.g. replying YES to our confirmation request): acknowledge warmly ("Brilliant — see you then!") and end with a blank line then: ACTION: confirm
- If they ask a question that you've answered: end with ACTION: none
- Answer role/location/process questions directly from the context above
- If you don't know something specific, say so honestly and suggest it as a great question for the interview — never promise that a person will follow up separately
- Sign off as "{recruiter_name}" from {company}
- Write plain text only — no HTML, no subject line, no quoted original message"""

    if stage in ("APPLICANT", "SCREENING"):
        prompt += """

SCREENING SELF-SERVE OPTIONS (you can act on these yourself):
- If they'd rather finish their screening online than by phone, send them the CHAT LINK from context verbatim. End with ACTION: none
- If they ask to be called back right now / ASAP, say something like "On it — expect a call shortly!" and end with a blank line then: ACTION: request_callback
- If they ask to be called at a specific time, and that request is a clear match for one of the AVAILABLE CALLBACK TIMES in context, confirm it warmly and end with a blank line then: ACTION: schedule_call [PROPOSED_DATE: <datetime>] — copying the bracketed datetime for that option EXACTLY as printed. NEVER invent or compute a datetime yourself.
- If the time they want isn't a clear match for any AVAILABLE CALLBACK TIME, don't guess — offer the closest 1-2 listed options in plain words, or send the PICK-A-TIME LINK from context so they can browse every option themselves. End with ACTION: none"""

    if custom_instructions:
        prompt += f"\n\nADDITIONAL PIPELINE CONTEXT:\n{custom_instructions}"

    return prompt


async def _generate_reply(db, candidate: Dict, pipeline: Dict, history: List[Dict], new_body: str) -> Dict:
    enrichment = await _fetch_enrichment(db, candidate)
    system = _build_system_prompt(candidate, pipeline, enrichment)

    messages: List[Dict] = []
    for msg in history[-10:]:
        role = "user" if msg.get("direction") == "inbound" else "assistant"
        messages.append({"role": role, "content": msg.get("body", "")})
    messages.append({"role": "user", "content": new_body})

    # Merge consecutive same-role turns (API requirement)
    merged: List[Dict] = []
    for m in messages:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] += "\n\n" + m["content"]
        else:
            merged.append(dict(m))

    if not EMERGENT_LLM_KEY:
        return {"reply": "", "action": "none", "proposed_date": None}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": EMERGENT_LLM_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    # ANTHROPIC_MODEL (llm_config.py). max_tokens leaves room
                    # for any thinking before the ~500-token reply.
                    "model": __import__("llm_config").primary_model(),
                    "max_tokens": 4000,
                    "system": system,
                    "messages": merged,
                },
            )
            r.raise_for_status()
            raw = __import__("llm_config").response_text(r.json()).strip()
    except Exception as e:
        logger.exception(f"email_replies LLM call failed: {e}")
        return {"reply": "", "action": "none", "proposed_date": None}

    action = "none"
    proposed_date = None
    reply_text = raw

    m = re.search(r"\nACTION:\s*(\w+)(?:\s+\[PROPOSED_DATE:\s*([^\]]+)\])?", raw)
    if m:
        action = m.group(1).lower()
        proposed_date = (m.group(2) or "").strip() or None
        reply_text = raw[: m.start()].strip()

    return {"reply": reply_text, "action": action, "proposed_date": proposed_date}


async def _take_action(db, candidate: Dict, action: str, proposed_date: Optional[str]) -> bool:
    """Returns False only for request_callback/schedule_call failures — the
    caller then swaps in an apology reply instead of promising something that
    didn't happen. Every other action is fire-and-forget (True)."""
    cid = candidate["id"]
    now = datetime.now(timezone.utc).isoformat()
    if action == "request_callback":
        if not candidate.get("phone") or (candidate.get("screening_status") or "") in ("approved", "rejected"):
            return False
        try:
            from dialer.place_call import place_call_now
            result = await place_call_now(candidate.get("user_id", ""), cid, candidate_initiated=True)
            logger.info(f"email_replies: callback requested by {cid}: {result.get('status')}")
            return True
        except Exception as e:
            logger.warning(f"email_replies: callback request failed for {cid}: {e}")
            return False
    elif action == "schedule_call":
        if not proposed_date:
            return False
        if not candidate.get("phone") or (candidate.get("screening_status") or "") in ("approved", "rejected"):
            return False
        try:
            run_at = datetime.fromisoformat(proposed_date.replace("Z", "+00:00"))
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=timezone.utc)
        except Exception:
            return False
        now_dt = datetime.now(timezone.utc)
        if run_at < now_dt + timedelta(minutes=2) or run_at > now_dt + timedelta(days=14):
            return False
        try:
            from dialer.queue import schedule_call_with_window
            delay_minutes = (run_at - now_dt).total_seconds() / 60
            await schedule_call_with_window(candidate.get("user_id", ""), cid, base_delay_minutes=delay_minutes, force=True)
            logger.info(f"email_replies: {cid} scheduled callback for {run_at.isoformat()} via email")
            return True
        except Exception as e:
            logger.warning(f"email_replies: schedule-call failed for {cid}: {e}")
            return False
    elif action == "confirm":
        # The chaser email literally asks "reply YES to this email to confirm" —
        # but until this branch existed no email YES was ever recorded: the
        # zero-confirm session alarm and the attendance queue read
        # appointment_sms_confirmed (de-facto "candidate confirmed", whatever
        # the channel), so recruiters saw "NONE confirmed" over sessions that
        # actually had confirmations.
        if candidate.get("appointment_at"):
            await db.candidates.update_one(
                {"id": cid},
                {"$set": {
                    "appointment_sms_confirmed": True,
                    "appointment_sms_confirmed_at": now,
                    "email_reply_actioned_at": now,
                    "updated_at": now,
                }},
            )
            logger.info(f"email_replies: {cid} confirmed attendance by email")
    elif action == "cancel":
        await db.candidates.update_one(
            {"id": cid},
            {"$set": {
                "email_reply_status": "declined",
                "email_reply_actioned_at": now,
                "screening_status": "rejected",
                "verdict": "withdrawn",
                "archived_at": now,
                "updated_at": now,
            }},
        )
        try:
            from auto_dialer import cancel_pending_retry_calls, cancel_appointment_reminders, cancel_form_reminder
            cancel_pending_retry_calls(cid)
            cancel_appointment_reminders(cid)
            cancel_form_reminder(cid)
        except Exception as e:
            import logging as _logging
            _logging.getLogger("email_replies").warning(f"withdrawn cancel jobs failed for {cid}: {e}")
    elif action == "reschedule" and proposed_date:
        await db.candidates.update_one(
            {"id": cid},
            {"$set": {"email_reschedule_requested": proposed_date, "email_reply_actioned_at": now}},
        )
        # Surface it on the bell. Without this the request only ever appeared
        # inside that candidate's drawer — a recruiter had to already be looking
        # at the right person to find out they want to move their interview.
        try:
            from notifications_service import create_notification
            nm = f"{candidate.get('first_name', '')} {candidate.get('last_name', '')}".strip() or "A candidate"
            await create_notification(
                candidate.get("user_id", ""), "appointment.reschedule_requested",
                f"📅 {nm} asked to reschedule (by email)",
                body=f"Proposed: {proposed_date}. They were sent the slot picker automatically.",
                link=f"/?candidate={cid}",
                candidate_id=cid, pipeline_id=candidate.get("pipeline_id"),
            )
        except Exception as e:
            import logging as _logging
            _logging.getLogger("email_replies").warning(f"reschedule notification failed for {cid}: {e}")
    return True


def _ensure_reschedule_link(candidate: Dict, reply_text: str) -> str:
    """Guarantee the slot-picker link ships with a reschedule reply.

    The APPOINTMENT stage goal tells the model to include it and the context
    block provides it, but the model is free to answer "the team will be in
    touch" instead — and nothing else in this flow sends a link, so the request
    dead-ends. Appending it is cheap; a duplicate link is not a failure mode."""
    base_url = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
    token = candidate.get("public_token")
    if not (base_url and token and reply_text):
        return reply_text
    link = f"{base_url}/reschedule/{token}"
    if link in reply_text:
        return reply_text
    return f"{reply_text}\n\nYou can pick a new time here: {link}"


# ---------------------------------------------------------------------------
# Core handler (called from route)
# ---------------------------------------------------------------------------

_DKIM_PASS = re.compile(r"@([A-Za-z0-9.-]+)\s*:\s*pass\b", re.I)


def sendgrid_sender_verdict(spf: Optional[str], dkim: Optional[str], from_email: str) -> Optional[bool]:
    """SendGrid's own SPF and DKIM verdicts on an inbound message.

    The From address alone proves nothing: anyone can write a candidate's
    address there, and the AI would then cancel or move that candidate's
    interview. Returns True when SPF passed (it checks the envelope sender,
    which is where from_email comes from) or a DKIM signature for the sender's
    domain passed. Returns False when SendGrid reported results and neither
    passed. Returns None when it reported nothing: a direct post, which already
    needed SENDGRID_INBOUND_SECRET, or a provider that doesn't send these fields."""
    if spf is None and dkim is None:
        return None
    if (spf or "").strip().lower() == "pass":
        return True
    domain = from_email.rsplit("@", 1)[-1].strip().lower() if "@" in (from_email or "") else ""
    for d in _DKIM_PASS.findall(dkim or ""):
        d = d.lower().rstrip(".")
        if domain and (domain == d or domain.endswith("." + d)):
            return True
    return False


async def handle_inbound(db, to: str, from_email: str, subject: str, text: str, envelope: str = "",
                         sender_authenticated: Optional[bool] = None) -> Dict:
    candidate_id = _extract_candidate_id(to) or _extract_candidate_id(envelope)
    if not candidate_id:
        logger.info(f"email_replies: no candidate_id in to={to!r} envelope={envelope!r}")
        return {"status": "ignored", "reason": "no candidate_id in address"}

    candidate = await db.candidates.find_one({"id": candidate_id}, {"_id": 0})
    if not candidate:
        logger.warning(f"email_replies: candidate {candidate_id!r} not found")
        return {"status": "ignored", "reason": "candidate not found"}

    clean = _strip_reply_quotes(text or "")
    if not clean:
        return {"status": "ignored", "reason": "empty body after stripping quotes"}

    # Only the candidate's own address may drive the AI. A message from any
    # other sender is filed for a human and gets no automated reply or action:
    # the reply carries their details (and a reschedule link), and the actions
    # cancel or move a real interview.
    # The address must also be genuine: SendGrid's SPF/DKIM verdicts, when it
    # sent them, must vouch for the sender's domain (see sendgrid_sender_verdict).
    on_file = (candidate.get("email") or "").strip().lower()
    address_matches = bool(on_file) and (from_email or "").strip().lower() == on_file
    if not address_matches or sender_authenticated is False:
        why = ("sender is not the candidate's address on file" if not address_matches
               else "sender's address failed SPF and DKIM checks")
        await db.email_reply_messages.insert_one({
            "candidate_id": candidate_id,
            "direction": "inbound",
            "from": from_email,
            "to": to,
            "subject": subject,
            "body": clean,
            "ai_generated": False,
            "unverified_sender": True,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        })
        try:
            from notifications_service import create_notification
            nm = f"{candidate.get('first_name', '')} {candidate.get('last_name', '')}".strip() or "a candidate"
            title = (f"Email about {nm} from an address not on file" if not address_matches
                     else f"Email from {nm}'s address that failed sender checks")
            await create_notification(
                candidate.get("user_id", ""), "email.unverified_reply",
                title,
                body=f"From {from_email or 'unknown sender'}; no automatic reply was sent. Check it before acting.",
                link=f"/inbox?candidate={candidate_id}",
                candidate_id=candidate_id, pipeline_id=candidate.get("pipeline_id"),
            )
        except Exception as e:
            logger.warning(f"email_replies: unverified-sender notification failed for {candidate_id}: {e}")
        logger.info(f"email_replies: {why} for {candidate_id}; held for a human")
        return {"status": "held", "reason": why}

    # A training-lapsed candidate emailing back is the rebook case the archive
    # sweep planned for — restore them to the board at their old stage with a
    # fresh 4-day hold before the reply is generated. (The AI's cancel action
    # re-archives them if the email turns out to be a withdrawal.)
    try:
        from dialer.training_archive_sweep import unarchive_on_contact
        await unarchive_on_contact(db, candidate)
    except Exception as e:
        logger.warning(f"email_replies: training-lapsed unarchive failed for {candidate_id}: {e}")

    now = datetime.now(timezone.utc).isoformat()

    await db.email_reply_messages.insert_one({
        "candidate_id": candidate_id,
        "direction": "inbound",
        "from": from_email,
        "to": to,
        "subject": subject,
        "body": clean,
        "ai_generated": False,
        "sent_at": now,
    })

    history = await _get_thread(db, candidate_id)
    user_id = candidate.get("user_id", "")
    pipeline = await resolve_settings(user_id, candidate.get("pipeline_id")) or {}

    result = await _generate_reply(db, candidate, pipeline, history[:-1], clean)
    reply_text = result["reply"]
    action = result["action"]
    proposed_date = result["proposed_date"]

    if action in ("cancel", "reschedule", "request_callback", "schedule_call"):
        ok = await _take_action(db, candidate, action, proposed_date)
        if action == "request_callback" and not ok:
            reply_text = "Sorry — I couldn't get a call going right now. Reply here and we'll sort it out."
        elif action == "schedule_call" and not ok:
            reply_text = "Sorry — that time didn't go through. Reply with another time and I'll get it booked."

    # Only append when the portal would actually open for them — it 409s on a
    # candidate who has already attended, and a dead link is worse than none.
    if action == "reschedule" and candidate.get("attendance_status") in (None, "no_show"):
        reply_text = _ensure_reschedule_link(candidate, reply_text)

    # Trainee correspondence pages CG1 (owner's rule, 2026-08-18): admins hear
    # genuine questions/messages from TRAINING-stage new hires, whatever the
    # channel. A withdrawal (cancel) or bare confirm is a status signal, not
    # correspondence, and stays silent.
    if (candidate.get("stage") or "").upper() == "TRAINING" and action not in ("cancel", "confirm"):
        try:
            from cg1_alerts import spawn_cg1_admin_alert
            nm = f"{candidate.get('first_name', '')} {candidate.get('last_name', '')}".strip() or "A starter"
            spawn_cg1_admin_alert(
                "starter.message",
                f"📧 New-hire email — {nm}",
                f'"{clean[:300]}"',
                candidate=candidate,
            )
        except Exception as e:
            logger.warning(f"email_replies: trainee-correspondence CG1 alert failed for {candidate_id}: {e}")

    if reply_text:
        from email_service import send_email_via_sendgrid
        reply_to = f"reply+{candidate_id}@{REPLY_DOMAIN}" if REPLY_DOMAIN else None
        reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        await send_email_via_sendgrid(
            to_email=from_email,
            subject=reply_subject,
            body=reply_text,
            candidate=candidate,
            settings=pipeline,
            reply_to=reply_to,
        )
        await db.email_reply_messages.insert_one({
            "candidate_id": candidate_id,
            "direction": "outbound",
            "from": os.environ.get("SENDGRID_FROM_EMAIL", ""),
            "to": from_email,
            "subject": reply_subject,
            "body": reply_text,
            "ai_generated": True,
            "action": action,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        })

    logger.info(f"email_replies: handled {candidate_id} action={action}")
    return {"status": "handled", "action": action, "candidate_id": candidate_id}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/webhooks/sendgrid/inbound-email")
async def sendgrid_inbound_email(request: Request):
    """SendGrid Inbound Parse webhook. Configure in SendGrid → Settings → Inbound Parse,
    with ?key=<SENDGRID_INBOUND_SECRET> on the destination URL (fails closed)."""
    from webhook_auth import require_sendgrid_inbound_secret
    require_sendgrid_inbound_secret(request)
    form = await request.form()
    # `from` is a Python keyword so we can't use Form(alias="from") — read directly
    to = form.get("to", "") or ""
    from_raw = form.get("from", "") or form.get("sender", "") or ""
    subject = form.get("subject", "") or ""
    text = form.get("text", "") or ""
    envelope = form.get("envelope", "") or ""
    # Try to get a clean from_email from the envelope JSON first (more reliable),
    # then fall back to stripping display name from the From header.
    from_email = ""
    if envelope:
        try:
            import json as _json
            env = _json.loads(envelope)
            from_email = _extract_email_address(env.get("from", "") or "")
        except Exception:
            pass
    if not from_email:
        from_email = _extract_email_address(from_raw)
    from deps import db
    authentic = sendgrid_sender_verdict(form.get("SPF"), form.get("dkim"), from_email)
    result = await handle_inbound(db, to=to, from_email=from_email, subject=subject, text=text, envelope=envelope,
                                  sender_authenticated=authentic)
    return result


@router.post("/webhooks/sendgrid/inbound-email/test")
async def test_inbound(payload: Dict[str, Any] = None, user: dict = Depends(current_user)):
    """Dev helper — POST {to, from, subject, text} to simulate an inbound email.
    Super-admins only, and only for a candidate in their own account: it runs
    the real reply flow, which emails the candidate."""
    if payload is None:
        payload = {}
    from deps import db, require_super_admin
    require_super_admin(user)
    cid = _extract_candidate_id(payload.get("to", ""))
    if not cid or not await db.candidates.find_one({"id": cid, "user_id": user["id"]}, {"_id": 1}):
        raise HTTPException(404, "Candidate not found")
    return await handle_inbound(
        db,
        to=payload.get("to", ""),
        from_email=payload.get("from", ""),
        subject=payload.get("subject", ""),
        text=payload.get("text", ""),
    )


@router.get("/candidates/{candidate_id}/email-thread")
async def get_email_thread(candidate_id: str, user: dict = Depends(current_user)):
    """Fetch the full email thread for a candidate — outbound emails we sent (from
    communications collection) merged with inbound replies + AI responses (from
    email_reply_messages collection), sorted chronologically."""
    from deps import db, assert_candidate_access

    cand = await db.candidates.find_one(
        {"id": candidate_id, "user_id": user["id"]}, {"_id": 0}
    )
    if not cand:
        raise HTTPException(404, "Candidate not found")
    await assert_candidate_access(user, cand)

    # Outbound emails we sent (warmup, form, reminders, slot-picker, etc.)
    sent_emails = await db.communications.find(
        {"candidate_id": candidate_id, "user_id": user["id"], "type": "email"},
        {"_id": 0, "subject": 1, "body": 1, "template_key": 1, "status": 1, "created_at": 1},
    ).sort("created_at", 1).to_list(200)
    sent_normalised = [
        {
            "direction": "outbound",
            "body": m.get("body", ""),
            "subject": m.get("subject", ""),
            "template_key": m.get("template_key", ""),
            "ai_generated": False,
            "sent_at": m.get("created_at", ""),
            "source": "sent",
        }
        for m in sent_emails
        if m.get("status") not in ("failed", "skipped")
    ]

    # Inbound replies from candidate + AI responses
    reply_messages = await _get_thread(db, candidate_id)
    for m in reply_messages:
        m["source"] = "reply"

    # Merge and sort chronologically
    all_messages = sorted(
        sent_normalised + reply_messages,
        key=lambda m: m.get("sent_at") or "",
    )

    return {
        "candidate_id": candidate_id,
        "name": f"{cand.get('first_name','')} {cand.get('last_name','')}".strip(),
        "email": cand.get("email", ""),
        "stage": cand.get("stage", ""),
        "email_reply_status": cand.get("email_reply_status"),
        "email_reschedule_requested": cand.get("email_reschedule_requested"),
        "messages": all_messages,
    }
