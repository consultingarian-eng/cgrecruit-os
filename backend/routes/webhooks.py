"""Public webhooks fired by Twilio and ElevenLabs.

Twilio → inbound SMS (STOP/HELP handling + chat-log mirroring).
ElevenLabs → post-call transcription (auto-advances candidate to APPOINTMENT
when the AI verdict is strong/good).
"""
from datetime import datetime, timedelta, timezone
from typing import Dict, Any
import company_profile
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from deps import db, logger
from pubsub import broadcast
from models import now_iso
from ai_service import summarize_call_transcript
from voice_service import fetch_elevenlabs_conversation
from webhook_auth import require_elevenlabs_signature, require_elevenlabs_tool_secret
from app_tz import app_zone, default_tz_name  # APP_TIMEZONE (noqa: F401)

router = APIRouter()


def _last10(phone: str) -> str:
    import re
    d = re.sub(r"\D", "", phone or "")
    return d[-10:] if len(d) >= 10 else d


# Stages that still need screening — kept in lockstep with the auto-dialer's own
# eligibility gate (`dialer._call_helpers._CALLABLE_STAGES`) so a call-back and an
# outbound retry never disagree about who is still un-screened.
_SCREENABLE_STAGES = frozenset({"APPLICANT", "SCREENING", ""})

# screening_status values that mean "not screened yet". `queued` is the important
# one: the moment a screening call goes unanswered the retry scheduler flips the
# candidate from `no_answer` to `queued` (dialer.queue.schedule_call_with_window,
# dialer.retry.schedule_dnd_retry), so that is the state nearly every call-back
# arrives in. Terminal/finished states (approved, rejected, appointment_pending,
# paused, completed) are absent by design.
_UNSCREENED_STATUSES = frozenset({
    "", "new", "pending", "queued", "no_answer", "didnt_connect",
    "incomplete_info", "in_progress",
})


def _should_screen_now(cand: dict) -> bool:
    """True if this caller is a candidate who hasn't been screened yet — so an
    inbound call should run the screening immediately rather than front-desk.
    Excludes anyone already booked, approved, rejected, archived (soft-rejected /
    no-show — a human should handle those), or opted out of auto-dialing."""
    if not cand:
        return False
    if cand.get("appointment_at"):
        return False
    if cand.get("archived_at"):
        return False
    if not cand.get("auto_dial", True):
        return False
    if (cand.get("stage") or "").upper() not in _SCREENABLE_STAGES:
        return False
    return (cand.get("screening_status") or "").lower() in _UNSCREENED_STATUSES


async def _match_inbound_candidate(user_id: str, pipeline_id: str, caller: str):
    """Best candidate for an inbound caller: prefer a booked/active record in
    this office, newest first. Fuzzy-matches on the last 10 digits."""
    import re
    key = _last10(caller)
    if not key:
        return None
    rx = r"\D*".join(re.escape(d) for d in key)
    q = {"phone": {"$regex": rx}}
    if pipeline_id:
        q["pipeline_id"] = pipeline_id
    if user_id:
        q["user_id"] = user_id
    matches = []
    async for c in db.candidates.find(q, {"_id": 0}).limit(50):
        if _last10(c.get("phone")) == key:
            matches.append(c)
    if not matches:
        return None

    # Prefer a booked/active record, newest first.
    matches.sort(key=lambda c: c.get("updated_at") or "", reverse=True)
    matches.sort(key=lambda c: 0 if (c.get("appointment_at") or (c.get("stage") or "").upper() in ("APPOINTMENT", "TRAINING")) else 1)
    return matches[0]


@router.post("/webhooks/elevenlabs/conversation-init",
             dependencies=[Depends(require_elevenlabs_tool_secret)])
async def elevenlabs_conversation_init(request: Request):
    """Called by ElevenLabs at the START of an inbound call. Given the caller's
    number + the answering agent, we look up who's calling and return dynamic
    variables (name, booked slot, stored phone for booking, office slug/address)
    so the inbound agent can greet them and reschedule. Unknown callers still get
    the office context so the agent can help + take a message.

    Requires the X-CGR-Tool-Secret header (ELEVENLABS_TOOL_SECRET): the answer
    names a candidate and their appointment for any phone number posted here."""
    from deps import resolve_settings
    from email_service import office_key_from_pipeline, office_maps_link
    try:
        body = await request.json()
    except Exception:
        body = {}

    caller = (body.get("caller_id") or body.get("system__caller_id")
              or body.get("from") or body.get("From") or "").strip()
    agent_id = (body.get("agent_id") or body.get("agent") or "").strip()
    called = (body.get("called_number") or body.get("to") or body.get("To") or "").strip()
    call_sid = (body.get("call_sid") or body.get("CallSid") or "").strip()

    # Resolve the office: by inbound_agent_id first, else by the called number.
    pipe = None
    if agent_id:
        pipe = await db.pipelines.find_one({"inbound_agent_id": agent_id}, {"_id": 0})
    if not pipe and called:
        import re
        ck = _last10(called)
        async for p in db.pipelines.find({"twilio_phone_number": {"$nin": [None, ""]}}, {"_id": 0}):
            if _last10(p.get("twilio_phone_number")) == ck:
                pipe = p
                break
    if not pipe:
        # Nothing to personalise with — hand back an empty payload; the agent's
        # baked prompt still covers the office it belongs to.
        return {"type": "conversation_initiation_client_data", "dynamic_variables": {}}

    user_id = pipe.get("user_id") or ""
    settings = await resolve_settings(user_id, pipe["id"]) or {}
    sca = settings.get("screen_call_agent") or {}
    profile = settings.get("recruiter_profile") or {}
    okey = office_key_from_pipeline(pipe)
    office_address = (
        (pipe.get("office_address") or "").strip()
        or ((settings.get("starter_template") or {}).get("office_address") or "").strip()
        or company_profile.office_address(okey)
    )
    maps_link = office_maps_link(okey)
    company = profile.get("company_name") or sca.get("company_name") or company_profile.company_name()
    city = profile.get("city") or ""

    dyn = {
        "company": company,
        "agent_name": sca.get("agent_name") or "Olivia",
        "city": city,
        "pipeline_slug": pipe.get("public_slug") or "",
        "office_address": office_address,
        "maps_note": (f"We're on Google Maps here: {maps_link}" if maps_link else ""),
        "phone": caller,          # overwritten with the stored phone if matched
        # The booking gate in the inbound prompt keys off this and nothing else.
        # `first_name` used to carry the same signal implicitly, which let a
        # caller who simply told the agent a name look identified enough to be
        # walked through the slot menu. An unmatched caller has no record to book
        # against, so the fact must be explicit and separate from anything they
        # can say down the phone.
        "caller_known": "false",
        "first_name": "",
        "full_name": "",
        "appointment_line": "",
        "start_date_line": "",
        "candidate_id": "",
        "pipeline_id": pipe["id"],
        "user_id": user_id,
        "call_kind": "inbound",
    }

    cand = await _match_inbound_candidate(user_id, pipe["id"], caller)
    if cand:
        dyn["caller_known"] = "true"
        dyn["first_name"] = cand.get("first_name") or ""
        dyn["full_name"] = f"{cand.get('first_name', '')} {cand.get('last_name', '')}".strip()
        dyn["phone"] = cand.get("phone") or caller  # stored format → book-by-phone exact match
        dyn["candidate_id"] = cand.get("id") or ""
        from zoneinfo import ZoneInfo
        from datetime import datetime as _dt
        tz = ZoneInfo((settings.get("region_language") or {}).get("timezone") or default_tz_name())

        def _fmt(iso):
            try:
                s = iso.replace("Z", "+00:00") if ("Z" in iso or "+" in iso) else iso
                d = _dt.fromisoformat(s)
                if d.tzinfo:
                    d = d.astimezone(tz)
                return d.strftime("%A, %b %-d at %-I:%M %p")
            except Exception:
                return ""

        stage = (cand.get("stage") or "").upper()
        if stage == "TRAINING" and cand.get("training_start_at"):
            lbl = _fmt(cand["training_start_at"])
            if lbl:
                dyn["start_date_line"] = f"You're scheduled to start on {lbl}."
        elif cand.get("appointment_at"):
            lbl = _fmt(cand["appointment_at"])
            if lbl:
                dyn["appointment_line"] = f"You're currently booked for {lbl}."

    resp: Dict[str, Any] = {"type": "conversation_initiation_client_data", "dynamic_variables": dyn}

    # If a caller who has NOT been screened yet rings the line, run the screening
    # right there instead of front-desk — we hand back a per-call config override
    # that swaps this one call into the existing screening flow (same prompt +
    # get_available_slots/book_slot tools already on the agent). Booked / rejected
    # / approved callers keep the front-desk experience (no override).
    if cand and _should_screen_now(cand):
        try:
            from voice_service import build_agent_system_prompt, _convert_placeholders_to_elevenlabs
            booking_prefs = settings.get("booking_preferences") or {}
            screening_prompt = build_agent_system_prompt(sca, booking_prefs=booking_prefs)
            role = profile.get("job_role") or ""
            if cand.get("job_id"):
                job = await db.jobs.find_one({"id": cand["job_id"]}, {"_id": 0})
                role = (job or {}).get("title") or role
            dyn["role"] = role or "the role"
            dyn["previous_context"] = cand.get("call_summary") or ""
            dyn["is_dnd_retry"] = "false"
            dyn["call_kind"] = "inbound_screening"
            first_msg = _convert_placeholders_to_elevenlabs(
                "Hey {{first_name}}, thanks for calling {{company}} back! I'm {{agent_name}} — "
                "if you've got about five minutes I can run you through a few quick screening "
                "questions right now and get you booked straight in. Is now a good time?"
            )
            resp["conversation_config_override"] = {
                "agent": {"first_message": first_msg, "prompt": {"prompt": screening_prompt}},
            }
        except Exception as e:
            logger.warning(f"conversation-init screening override failed: {e}")

    await _remember_inbound_init(call_sid, caller, agent_id, dyn)
    return resp


# What the initiation webhook handed ElevenLabs for an inbound phone call, kept
# server-side. The post-call webhook echoes the call's dynamic variables back,
# but those can't be trusted: whoever opens a conversation with the agent can
# choose them. So post-call identity comes from this record (matched on the
# Twilio call SID and the caller's number from ElevenLabs' phone-call metadata),
# never from the echo.
_INBOUND_INIT_TTL = timedelta(hours=6)


async def _remember_inbound_init(call_sid: str, caller: str, agent_id: str, dyn: dict) -> None:
    try:
        now = datetime.now(timezone.utc)
        await db.inbound_call_inits.insert_one({
            "call_sid": call_sid or None,
            "caller_last10": _last10(caller),
            "agent_id": agent_id or None,
            "dynamic_variables": dict(dyn or {}),
            "created_at": now.isoformat(),
        })
        await db.inbound_call_inits.delete_many(
            {"created_at": {"$lt": (now - _INBOUND_INIT_TTL).isoformat()}})
    except Exception as e:
        logger.warning(f"inbound init record failed: {e}")


async def _trusted_inbound_init(data: dict) -> Dict[str, Any]:
    """The dynamic variables for a finished inbound phone call, from our own
    records. Empty when the conversation was not a phone call (a browser or
    widget session) or can't be tied to an office, so nothing is processed."""
    meta = data.get("metadata") or {}
    phone_call = meta.get("phone_call") if isinstance(meta, dict) else None
    if not isinstance(phone_call, dict):
        return {}
    external = str(phone_call.get("external_number") or "").strip()
    if not _last10(external):
        return {}
    call_sid = str(phone_call.get("call_sid") or "").strip()
    since = (datetime.now(timezone.utc) - _INBOUND_INIT_TTL).isoformat()

    rec = None
    if call_sid:
        rec = await db.inbound_call_inits.find_one({"call_sid": call_sid}, {"_id": 0})
    if not rec:
        rec = await db.inbound_call_inits.find_one(
            {"caller_last10": _last10(external), "created_at": {"$gte": since}},
            {"_id": 0}, sort=[("created_at", -1)],
        )
    if rec and rec.get("caller_last10") == _last10(external):
        init = dict(rec.get("dynamic_variables") or {})
        if init.get("user_id") and init.get("pipeline_id"):
            return init

    # No record (the initiation webhook isn't set up, or failed): rebuild the
    # minimum from facts ElevenLabs reports about the call itself. Never runs
    # screening processing: a front-desk record and a notification only.
    agent_id = str(data.get("agent_id") or "").strip()
    pipe = None
    if agent_id:
        pipe = await db.pipelines.find_one({"inbound_agent_id": agent_id}, {"_id": 0})
    called = _last10(str(phone_call.get("agent_number") or ""))
    if not pipe and called:
        async for p in db.pipelines.find({"twilio_phone_number": {"$nin": [None, ""]}}, {"_id": 0}):
            if _last10(p.get("twilio_phone_number")) == called:
                pipe = p
                break
    if not pipe:
        return {}
    init = {"user_id": pipe.get("user_id") or "", "pipeline_id": pipe["id"],
            "phone": external, "call_kind": "inbound", "candidate_id": "",
            "first_name": "", "full_name": ""}
    cand = await _match_inbound_candidate(init["user_id"], pipe["id"], external)
    if cand:
        init["candidate_id"] = cand.get("id") or ""
        init["first_name"] = cand.get("first_name") or ""
        init["full_name"] = f"{cand.get('first_name', '')} {cand.get('last_name', '')}".strip()
    return init if init["user_id"] else {}


@router.post("/webhooks/twilio/inbound-sms")
async def twilio_inbound_sms(request: Request):
    """Twilio sends a POST here when someone replies to one of our SMS.
    Delegates to training_sms.handle_inbound which handles STOP/YES/AI replies
    for all pipeline stages (SCREENING, APPOINTMENT, TRAINING, etc.)."""
    from training_sms import handle_inbound
    form = await request.form()
    return await handle_inbound(
        db,
        From=form.get("From", ""),
        Body=form.get("Body", ""),
        To=form.get("To"),
        MessageSid=form.get("MessageSid"),
        request=request,
    )


async def _handle_inbound_post_call(conv_id: str, data: dict, init: dict) -> dict:
    """Post-call handling for a call the inbound agent answered.

    Two shapes arrive here. A front-desk call gets logged and a human notified
    (always, so nothing slips), escalating louder when the agent had to hand off
    an unanswered question. A call-back that the initiation webhook swapped into
    the screening flow (`call_kind == "inbound_screening"`) is handed to the same
    post-call processing an outbound screening call gets — see
    `_handle_inbound_screening_post_call`."""
    from notifications_service import create_notification

    user_id = init.get("user_id") or ""
    pipeline_id = init.get("pipeline_id") or ""
    candidate_id = init.get("candidate_id") or ""
    caller = init.get("phone") or ""
    name = (init.get("full_name") or init.get("first_name") or "").strip()

    # A caller the agent registered mid-call (`register_caller`) had no record
    # when these variables were minted, so `candidate_id` is empty and their
    # screening would be written up against nobody. Re-resolve by the number that
    # rang us — the record exists by now.
    registered_mid_call = False
    if not candidate_id and caller and user_id:
        late = await _match_inbound_candidate(user_id, pipeline_id, caller)
        if late:
            candidate_id = late.get("id") or ""
            name = name or f"{late.get('first_name', '')} {late.get('last_name', '')}".strip()
            registered_mid_call = True
            logger.info(f"inbound post-call: matched {candidate_id} registered during {conv_id}")

    raw = data.get("transcript") or []
    transcript = [{"role": x.get("role", ""), "text": x.get("message", "") or x.get("text", "")} for x in raw]
    joined = " ".join((t.get("text") or "").lower() for t in transcript)

    # Did the agent hand off? (matches the escalation phrase in the inbound prompt)
    escalated = "someone from the team follow up" in joined or "have someone" in joined and "follow up" in joined
    booked = "book_slot" in str(data).lower() or "you're now booked" in joined or "booked for" in joined
    screening = (init.get("call_kind") or "") == "inbound_screening"

    # Persist a lightweight record of the inbound call.
    try:
        await db.inbound_calls.insert_one({
            "conversation_id": conv_id,
            "user_id": user_id,
            "pipeline_id": pipeline_id,
            "candidate_id": candidate_id or None,
            "caller": caller,
            "name": name or None,
            "transcript": transcript,
            "escalated": bool(escalated),
            "rebooked": bool(booked),
            "screening": screening,
            "created_at": now_iso(),
        })
    except Exception as e:
        logger.warning(f"inbound_calls insert failed: {e}")

    if screening or registered_mid_call:
        # A mid-call registration IS a screening — it just wasn't one when the
        # call connected, so run it through the same write-up (transcript,
        # summary, verdict, booking honoured) an outbound screening gets.
        res = await _handle_inbound_screening_post_call(
            conv_id, data, {**init, "candidate_id": candidate_id},
        )
        if res is not None:
            return res
        # Couldn't identify the candidate — fall through to the front-desk notify
        # so the call still reaches a human.

    if user_id:
        who = name or caller or "an inbound caller"
        if escalated:
            title = f"📞 {who} called — needs a human follow-up"
            body_txt = "The front-desk agent couldn't fully answer and asked the team to follow up."
        elif booked:
            title = f"📞 {who} called and rescheduled"
            body_txt = "Interview time was updated on the call."
        else:
            title = f"📞 Inbound call from {who}"
            body_txt = "Handled by the front-desk agent."
        link = f"/inbox?candidate={candidate_id}" if candidate_id else (f"/inbox?phone={caller}" if caller else None)
        try:
            await create_notification(
                user_id, "call.inbound", title, body=body_txt, link=link,
                candidate_id=candidate_id or None, pipeline_id=pipeline_id or None,
            )
        except Exception as e:
            logger.warning(f"inbound-call notification failed: {e}")

    return {"ok": True, "inbound": True, "escalated": bool(escalated), "rebooked": bool(booked)}


async def _handle_inbound_screening_post_call(conv_id: str, data: dict, init: dict):
    """A not-yet-screened candidate rang back and the initiation webhook swapped
    that call into the screening flow. Outbound calls create their `conversations`
    row at dial time; this call never had one, so we materialise it here and hand
    off to the shared screening processing — verdict, stage move, rejection /
    slot-picker comms and retry scheduling then behave exactly as they would after
    an outbound screening call.

    Returns None when the caller can't be tied to a candidate, so the front-desk
    notification still fires."""
    from models import Conversation
    from notifications_service import create_notification

    candidate_id = (init.get("candidate_id") or "").strip()
    user_id = (init.get("user_id") or "").strip()
    pipeline_id = (init.get("pipeline_id") or "").strip()
    if not (candidate_id and user_id):
        return None

    conv = await db.conversations.find_one({"elevenlabs_conversation_id": conv_id}, {"_id": 0})
    if not conv:
        # Insert as "initiated" with no transcript on purpose: if the processing
        # below dies, `dialer.scheduler._auto_sync_recent_calls` picks the row up
        # 45s later and syncs it like any other stuck conversation.
        conv = Conversation(
            candidate_id=candidate_id, user_id=user_id,
            elevenlabs_conversation_id=conv_id,
            status="initiated",
            dynamic_variables=dict(init or {}),
        ).model_dump()
        await db.conversations.insert_one(conv)

    # No DND-bypass redial on this path: the candidate rang US, so there is no
    # voicemail to ring through — auto-calling them back 25s after they hang up
    # would be plain wrong.
    res = await _process_screening_post_call(conv, conv_id, data, allow_dnd_retry=False)

    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user_id}, {"_id": 0}) or {}
    status = (cand.get("screening_status") or "").lower()

    # They just did the screening on the phone — drop the retry call the missed
    # outbound attempt left queued. Booking already does this inside
    # /public/book-by-phone; this covers the rejected / no-slot-agreed outcomes.
    if status in ("approved", "rejected", "appointment_pending"):
        try:
            from auto_dialer import cancel_pending_retry_calls
            cancel_pending_retry_calls(candidate_id)
        except Exception as e:
            logger.warning(f"cancel queued retries after inbound screening failed: {e}")

    who = (init.get("full_name") or init.get("first_name") or init.get("phone") or "A candidate").strip()
    if status == "approved" or cand.get("appointment_at"):
        title = f"📞 {who} called back, screened and booked"
        body_txt = "Completed the screening on their call-back and took an interview slot."
    elif status == "rejected":
        title = f"📞 {who} called back and screened — did not qualify"
        body_txt = cand.get("disqualification_reason") or "Failed a hard screening gate on the call."
    elif status == "appointment_pending":
        title = f"📞 {who} called back and screened — no slot agreed"
        body_txt = "Screening completed on the call-back; the slot-picker link has been sent."
    else:
        title = f"📞 {who} called back — screening incomplete"
        body_txt = "The call-back screening didn't finish; the usual retry cadence continues."
    try:
        await create_notification(
            user_id, "call.inbound", title, body=body_txt,
            link=f"/inbox?candidate={candidate_id}",
            candidate_id=candidate_id, pipeline_id=pipeline_id or None,
        )
    except Exception as e:
        logger.warning(f"inbound-screening notification failed: {e}")

    return {**res, "inbound": True, "screening": True}


@router.post("/webhooks/elevenlabs/post-call")
async def elevenlabs_post_call_webhook(request: Request):
    """Public webhook ElevenLabs hits after a conversation completes.
    Auto-advances candidate to APPOINTMENT when verdict ∈ (strong, good).

    NOTE: this decorator used to sit on `_handle_inbound_post_call` (a helper
    taking `conv_id, data, init`), so FastAPI required a `conv_id` query param and
    every ElevenLabs delivery 422'd — which is why `dialer.scheduler`'s auto-sync
    sweep had to become the primary post-call path. The sweep still runs as the
    safety net; this endpoint now handles what it was written to handle.

    Authenticated by the ElevenLabs-Signature HMAC (ELEVENLABS_WEBHOOK_SECRET).
    Fails closed: a forged body here could write a verdict, reject a candidate
    and fire their rejection texts."""
    import json as _json
    raw = await require_elevenlabs_signature(request)
    try:
        body = _json.loads(raw or b"{}")
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    event_type = body.get("type") or body.get("event") or ""
    _ = event_type
    data = body.get("data") or body
    conv_id = data.get("conversation_id") or body.get("conversation_id") or ""
    if not conv_id:
        raise HTTPException(400, "conversation_id missing")

    conv = await db.conversations.find_one({"elevenlabs_conversation_id": conv_id}, {"_id": 0})
    if not conv:
        # Inbound receptionist calls have no pre-created conversation doc. They
        # are identified ONLY by our own record of the initiation webhook and by
        # ElevenLabs' phone-call metadata — never by the dynamic variables echoed
        # in this body (`conversation_initiation_client_data`), which the person
        # who opened the conversation can set to anything, including someone
        # else's phone number. A conversation with no phone-call metadata (a
        # browser or widget session) is not processed at all.
        init = await _trusted_inbound_init(data)
        if init and (init.get("call_kind") or "").startswith("inbound"):
            return await _handle_inbound_post_call(conv_id, data, init)
        logger.warning(f"post-call webhook: unknown conversation_id {conv_id}")
        return {"ok": False, "reason": "unknown conversation_id"}

    return await _process_screening_post_call(conv, conv_id, data, root=body)


async def _process_screening_post_call(
    conv: dict,
    conv_id: str,
    data: dict,
    allow_dnd_retry: bool = True,
    root: dict = None,
) -> dict:
    """Everything that happens to a candidate after a screening call: transcript
    + summary persisted, verdict applied, hard-gate rejections mailed, slot-picker
    link sent when no slot was agreed, retries queued.

    Shared by the outbound post-call webhook and the inbound call-back screening
    (`_handle_inbound_screening_post_call`), which passes `allow_dnd_retry=False`.
    `root` is the raw webhook body when `data` was nested under `data`."""
    user_id = conv["user_id"]
    candidate_id = conv["candidate_id"]

    raw_transcript = data.get("transcript") or []
    if not raw_transcript:
        fetched = fetch_elevenlabs_conversation(conv_id) or {}
        raw_transcript = fetched.get("transcript") or []
        data = {**fetched, **data}
    transcript = [{"role": x.get("role", ""), "text": x.get("message", "") or x.get("text", "")} for x in raw_transcript]
    metadata = data.get("metadata") or {}
    duration = metadata.get("call_duration_secs") or data.get("duration_seconds")
    # ElevenLabs may send termination_reason in metadata OR at the data root.
    termination_reason = (
        metadata.get("termination_reason")
        or data.get("termination_reason")
        or (root or {}).get("termination_reason")
        or ""
    ).lower()
    voicemail_by_platform = "voicemail" in termination_reason

    # Phrase-scan fallback + misfire veto (shared with the auto-sync sweep and
    # manual Sync). ElevenLabs' voicemail_detection can fire mid-call on a live
    # human — typically during the silent pause while get_available_slots loads —
    # so the flag is ignored when the transcript shows a real back-and-forth.
    # Must run BEFORE the DND redial below, or we'd instantly re-call the person
    # who was just speaking to us.
    from call_classification import resolve_voicemail
    voicemail_by_platform, vm_misfired = resolve_voicemail(voicemail_by_platform, transcript)
    if vm_misfired:
        logger.warning(f"post-call {conv_id}: voicemail_detection misfired on a live conversation — flag ignored")

    # A call with real talk-time and still no transcript — not from the webhook
    # payload and not from the fetch above — is ElevenLabs mid-transcription,
    # not a call nobody answered. Concluding here is what filed Cosmo's five-minute
    # screening as an empty "no_answer": the status put the row outside the
    # auto-sync sweep's filter, so nothing ever looked at it again and a full
    # screening became invisible for days.
    #
    # Writing NOTHING is the whole point: the row stays as the dialler left it,
    # which is exactly the shape the auto-sync sweep looks for. It re-polls
    # every 60 seconds and concludes at its own 30-minute cutoff, so this
    # defers the decision rather than dropping it.
    if not transcript and (duration or 0) >= 20 and not voicemail_by_platform:
        logger.warning(
            f"post-call {conv_id}: {duration}s of call with no transcript yet — "
            "leaving it for the auto-sync sweep rather than filing it as no_answer"
        )
        return {"ok": True, "deferred": "transcript_not_ready", "duration_seconds": duration}

    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user_id}, {"_id": 0}) or {}

    # No-show revival calls get fully separate post-call handling — no DND
    # retry, no screening verdict, no retry scheduling. Must branch BEFORE the
    # DND block below or a revival voicemail would trigger a screening-style
    # redial against an archived candidate.
    if (conv.get("call_type") or "") == "no_show_revival":
        from dialer.revival import handle_revival_post_call
        return await handle_revival_post_call(db, conv, cand, transcript, duration, voicemail_by_platform)

    # Fire the DND-bypass redial FIRST — before the LLM summarization call below,
    # which can take several seconds and would otherwise eat into the 3-minute
    # window iOS/Android need to let a repeated call ring through Do Not Disturb.
    dnd_res: Dict[str, Any] = {}
    if voicemail_by_platform and allow_dnd_retry:
        try:
            from dialer.retry import schedule_dnd_retry
            dnd_res = await schedule_dnd_retry(user_id, candidate_id)
            logger.info(f"DND retry: {dnd_res}")
        except Exception as e:
            logger.warning(f"DND retry schedule failed: {e}")

    job = None
    if cand.get("job_id"):
        job = await db.jobs.find_one({"id": cand["job_id"], "user_id": user_id}, {"_id": 0})
    summary = await summarize_call_transcript(transcript, cand.get("first_name", ""), (job or {}).get("title", ""), duration_seconds=duration)
    candidate_spoke = any(t.get("role") in ("user", "human") for t in transcript)
    from call_classification import is_complete_call, screening_status_after_call
    is_complete = is_complete_call(candidate_spoke, duration, voicemail_by_platform)
    new_status = "completed" if is_complete else "no_answer"
    await db.conversations.update_one(
        {"id": conv["id"], "user_id": user_id},
        {"$set": {
            "transcript": transcript,
            "summary": summary.get("summary", ""),
            "suitability_score": summary.get("suitability_score"),
            "duration_seconds": duration,
            "status": new_status,
        }},
    )
    from call_classification import DISQ_LABELS as _DISQ_LABELS
    verdict = summary.get("verdict")
    raw_disq = summary.get("disqualification_reason")
    disq_label = _DISQ_LABELS.get(raw_disq, raw_disq) if raw_disq else None

    is_voicemail = voicemail_by_platform or (
        not is_complete and "voicemail" in (summary.get("summary") or "").lower()
    )
    cand_update: Dict[str, Any] = {
        "last_call_status": new_status,
        "last_call_voicemail": is_voicemail,
        "updated_at": now_iso(),
    }
    if verdict and is_complete:
        cand_update["verdict"] = verdict
        cand_update["call_summary"] = summary.get("summary", "")
        cand_update["suitability_score"] = summary.get("suitability_score")

    # One ladder for every post-call processor — see screening_status_after_call.
    next_status = screening_status_after_call(
        is_complete=is_complete, verdict=verdict, disq_reason=raw_disq,
        has_appointment=bool(cand.get("appointment_at")),
    )
    # The DND bypass above already wrote screening_status='queued' alongside a
    # next_call_at 25s out. This patch was built from the pre-DND snapshot and
    # carries no next_call_at, so writing 'no_answer' over it would leave the row
    # with an imminent timestamp and a status that matches none of the Call Queue
    # buckets — invisible while the dialer is seconds from ringing.
    if not (next_status == "no_answer" and dnd_res.get("status") == "scheduled"):
        cand_update["screening_status"] = next_status
    if next_status == "rejected":
        cand_update["disqualification_reason"] = disq_label
        # Archive like every other rejection path (server.py twiml, text
        # screening). Without this the candidate wore a red pill in the
        # Screening column and — because the apply dedupe only readmits
        # archived candidates — silently blocked their own re-application.
        # Archiving is also what lets them come back.
        cand_update["archived_at"] = now_iso()
        cand_update["archived_reason"] = "withdrawn" if raw_disq == "withdrawn" else "rejected"

    # A recruiter's pause lives in this same screening_status field, and this is
    # the busiest post-call writer in the system — a blind $set here silently
    # undoes "stop calling this person" and drops them out of the Paused list, so
    # nobody learns it happened. Same conditional-write shape the auto-sync sweep
    # uses (dialer/scheduler.py): take everything on the paused path EXCEPT the
    # status, so a verdict or a rejection archive still lands.
    res = await db.candidates.update_one(
        {"id": candidate_id, "user_id": user_id, "screening_status": {"$ne": "paused"}},
        {"$set": cand_update},
    )
    if not res.matched_count:
        preserved = {k: v for k, v in cand_update.items() if k != "screening_status"}
        if preserved:
            await db.candidates.update_one(
                {"id": candidate_id, "user_id": user_id},
                {"$set": preserved},
            )

    # Hard gate rejection → fire rejection email + SMS immediately.
    # Skip for "withdrawn" — candidate asked not to be contacted; don't send comms.
    if disq_label and raw_disq != "withdrawn":
        try:
            from deps import send_stage_comms
            refreshed = await db.candidates.find_one({"id": candidate_id}, {"_id": 0}) or cand
            await send_stage_comms(user_id=user_id, candidate=refreshed, template_key="rejection")
        except Exception as e:
            logger.warning(f"rejection comms failed: {e}")

    # Screening done but no slot agreed → send slot-picker link
    if next_status == "appointment_pending":
        try:
            from routes.attendance import _fire_slot_picker_outreach
            refreshed = await db.candidates.find_one({"id": candidate_id}, {"_id": 0}) or cand
            slot_res = await _fire_slot_picker_outreach(user_id, refreshed)
            # Mark sent so the hourly unbooked-screened sweep doesn't send a duplicate.
            await db.candidates.update_one(
                {"id": candidate_id, "user_id": user_id},
                {"$set": {"slot_picker_sent_at": now_iso()}},
            )
            logger.info(f"slot_picker outreach: {slot_res}")
        except Exception as e:
            logger.warning(f"slot_picker outreach failed: {e}")

    # Screening not done (voicemail or incomplete) → send self-screening link +
    # queue the next set per the per-attempt retry cadence (retry_delays).
    if next_status in ("no_answer", "incomplete_info"):
        # Held back only while the 25s DND bypass redial is pending — texting
        # and then immediately re-ringing reads as spam, and whoever answers
        # that redial never needed the text. This used to be implicit: the DND
        # scheduler had written screening_status='queued' and the sender
        # re-read it, so it skipped by accident rather than by decision. Now
        # the outcome is passed in, so the guard has to be stated.
        if dnd_res.get("status") != "scheduled":
            try:
                from deps import maybe_fire_screening_retry
                retry_res = await maybe_fire_screening_retry(
                    user_id, candidate_id, outcome=next_status)
                logger.info(f"screening retry: {retry_res}")
            except Exception as e:
                logger.warning(f"screening retry trigger failed: {e}")
        try:
            from auto_dialer import maybe_schedule_incomplete_retry
            call_res = await maybe_schedule_incomplete_retry(user_id, candidate_id)
            logger.info(f"incomplete retry call: {call_res}")
        except Exception as e:
            logger.warning(f"incomplete retry call schedule failed: {e}")

    await broadcast(user_id)
    return {"ok": True, "candidate_id": candidate_id, "stage": cand_update.get("stage"), "verdict": verdict, "disqualification_reason": disq_label}
