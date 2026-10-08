"""Public retry-screening endpoints — when a phone screening call fails (voicemail,
no-answer, interrupted, or no time agreed), the candidate is auto-emailed/SMSed a
link to /retry/{token}. They can finish via:
  • text chat — `POST /api/public/retry/{token}/chat` streams a Claude reply
  • a call back — `POST /api/public/retry/{token}/callback` or `/schedule-call`.
The chat uses the same screening prompt the phone agent uses. (There is no
browser voice session: a public route that minted ElevenLabs sessions let the
caller choose the agent's dynamic variables, so it was removed.)
"""
import os
import company_profile
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List
import pytz
from fastapi import APIRouter, HTTPException, Body

from deps import db, logger, get_or_create_settings, resolve_settings, send_stage_email
from models import now_iso
from dialer.state import now_utc
from dialer.window import parse_hhmm, next_in_window
from app_tz import app_zone, default_tz_name  # APP_TIMEZONE (noqa: F401)

router = APIRouter()


def _office_map(pipeline: dict):
    """{label, link, embed} for the pipeline's office, or None. The embed is a
    keyless Google Maps iframe of the office address (company profile)."""
    from urllib.parse import quote_plus
    okey = company_profile.office_key_for_pipeline(pipeline)
    office = company_profile.office(okey)
    address = office.get("address") or ""
    link = office.get("google_maps_link") or ""
    if not (address or link):
        return None
    return {
        "label": f"Our {office.get('label') or okey} office",
        "link": link or f"https://www.google.com/maps/search/?api=1&query={quote_plus(address)}",
        "embed": f"https://maps.google.com/maps?q={quote_plus(address)}&output=embed" if address else "",
    }


def _compute_call_time_options(settings: Dict[str, Any]) -> List[Dict[str, str]]:
    """Candidate-facing preset callback times ('Later today' / 'Tomorrow morning' / ...),
    clamped into the pipeline's actual call window via the same next_in_window math the
    dialer itself uses. Kept to a short flat list so the frontend picker stays dumb —
    render label, POST the iso back verbatim."""
    ad = (settings or {}).get("auto_dialer") or {}
    region = (settings or {}).get("region_language") or {}
    tz_name = region.get("timezone") or default_tz_name()
    start_hhmm = ad.get("call_window_start", "09:00")
    end_hhmm = ad.get("call_window_end", "19:00")
    days = ad.get("call_window_days") or [0, 1, 2, 3, 4]
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.UTC
    now_local = now_utc().astimezone(tz)
    sh, sm = parse_hhmm(start_hhmm)
    eh, _em = parse_hhmm(end_hhmm)

    def _mk(local_dt: datetime, label: str) -> Dict[str, str]:
        clamped = next_in_window(local_dt, tz_name, start_hhmm, end_hhmm, days)
        return {"label": label, "iso": clamped.isoformat()}

    tomorrow = now_local + timedelta(days=1)
    candidates = [
        _mk(now_local + timedelta(hours=2), "Later today"),
        _mk(tomorrow.replace(hour=max(sh, 9), minute=sm, second=0, microsecond=0), "Tomorrow morning"),
        _mk(tomorrow.replace(hour=13, minute=0, second=0, microsecond=0), "Tomorrow afternoon"),
        _mk(tomorrow.replace(hour=max(min(eh - 1, 18), sh), minute=0, second=0, microsecond=0), "Tomorrow evening"),
    ]
    seen: set = set()
    options: List[Dict[str, str]] = []
    for opt in candidates:
        if opt["iso"] in seen:
            continue
        seen.add(opt["iso"])
        options.append(opt)
    return options


async def _screening_thread_for(cand: Dict[str, Any]) -> list:
    """Web chat + SMS turns as one conversation (see transcript_service)."""
    from transcript_service import screening_thread
    sms_rows = await db.training_sms_messages.find(
        {"candidate_id": cand.get("id")},
        {"_id": 0, "direction": 1, "body": 1, "timestamp": 1},
    ).sort("timestamp", 1).to_list(200)
    return screening_thread(cand.get("chat_log"), sms_rows)


def _public_retry_view(cand: Dict[str, Any], pipeline: Dict[str, Any], company: str, settings: Optional[Dict[str, Any]] = None, thread: Optional[list] = None) -> Dict[str, Any]:
    """Strip a candidate doc to the fields safe for the public retry page."""
    phone = cand.get("phone") or ""
    masked_phone = f"***-***-{phone[-4:]}" if len(phone) >= 4 else "your phone"
    return {
        "candidate": {
            "id": cand.get("id"),
            "first_name": cand.get("first_name"),
            "last_name": cand.get("last_name"),
            "email": cand.get("email"),
            "screening_status": cand.get("screening_status"),
            "appointment_at": cand.get("appointment_at"),
            "screening_attempts": cand.get("screening_attempts", 0),
            "masked_phone": masked_phone,
            # The WHOLE screening conversation — web chat AND SMS — so someone
            # who started by text lands mid-conversation here, not at a fresh
            # greeting re-asking question 1.
            "retry_chat": thread if thread is not None else [
                m for m in (cand.get("chat_log") or [])
                if (m.get("channel") or "") == "retry"
            ],
        },
        "pipeline": {"name": pipeline.get("name", ""), "slug": pipeline.get("public_slug", "")},
        "company": company,
        # Map card shown under the commute question, from the company profile.
        "office_map": _office_map(pipeline),
        "max_attempts": 3,
        "call_time_options": _compute_call_time_options(settings or {}),
    }


@router.get("/public/retry/{token}")
async def get_retry_session(token: str):
    """Land on the retry page — returns candidate name, attempt count, and chat history."""
    cand = await db.candidates.find_one({"public_token": token}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Application not found")
    pipeline = await db.pipelines.find_one({"id": cand.get("pipeline_id")}, {"_id": 0}) or {}
    settings = await resolve_settings(cand["user_id"], cand.get("pipeline_id"))
    company = (settings.get("recruiter_profile") or {}).get("company_name") or company_profile.company_name()
    thread = await _screening_thread_for(cand)
    return _public_retry_view(cand, pipeline, company, settings, thread=thread)


def _build_retry_chat_system_prompt(sca: Dict[str, Any], pipeline: Dict[str, Any], cand: Dict[str, Any], job_title: str, company: str, slug: str) -> str:
    """Adapt the voice agent's screening prompt for a text-message conversation."""
    questions = sca.get("screening_questions") or []
    purpose = (sca.get("purpose") or "").strip()
    extra = (sca.get("additional_context") or "").strip()
    pipe_extra = (pipeline.get("additional_context_override") or "").strip()
    # Same date anchor as the SMS agent — without it the model can't relate
    # slot datetimes to now, and "today"/"tomorrow" becomes a coin flip.
    _now_et = datetime.now(pytz.timezone(default_tz_name()))
    parts = [
        f"CURRENT DATE & TIME: {_now_et.strftime('%A, %B %-d, %Y, %-I:%M %p %Z')}. "
        "Relative words like 'today' or 'tomorrow' must be computed from this timestamp — "
        "when unsure, name the day and date instead ('Thursday, Jul 30').",
        "",
        f"You are {sca.get('agent_name','Olivia')}, a friendly text-chat screening assistant for {company}.",
        f"You are screening {cand.get('first_name','the candidate')} for the {job_title or 'open'} role at the {pipeline.get('name','')} office.",
        "",
        "# THIS IS A TEXT CHAT, NOT A PHONE CALL (read before anything below)",
        "- There is no audio here. You are not calling, dialing, or speaking — you are exchanging text messages.",
        "- The Purpose/Context sections below were written for the PHONE screening agent and may reference being "
        "on a call, being recorded, or speaking on the phone. Those references do NOT apply to this conversation "
        "— ignore them completely, including any instruction to disclose 'this call is recorded' or similar. "
        "Never say or imply this chat is a phone call or that it is being recorded.",
        "- Everything else — the actual questions, what to listen for, tone, screening logic — still applies "
        "exactly as written, just delivered as short text messages instead of spoken sentences.",
        "",
        "# Tone",
        "- Warm, upbeat, concise. Vary your affirmations ('amazing', 'great', 'perfect', 'love that') "
        "— never the same opener twice in a row.",
        "- Emoji: at most one every few messages, never two messages in a row, never the same emoji "
        "twice in the conversation. A smiley on every reply reads as a bot.",
        "- Plain text ONLY — no markdown. No **bold**, no bullets, no headings: the candidate sees "
        "the asterisks as literal symbols.",
        "- One question per message. Keep replies short (1-2 sentences) — this is a text chat, not a phone call.",
        "- Open your FIRST message with a one-line hello and then the first screening question, together. "
        "Never ask whether now is a good time, whether they have a couple of minutes, or say how long "
        "this takes — time-checks belong to phone calls; here they're already reading and can answer whenever.",
        "- No intro speech and no role pitch before the questions — the role is explained at the interview. "
        "If they ask about the role or whether you're an AI, answer honestly in one short sentence and move on.",
        "- The candidate may have already answered some questions on a previous phone call attempt. Recap briefly if you have context, then continue.",
        "",
        "# Purpose",
        purpose or f"Screen the candidate's interest and fit for the {job_title or 'open'} role and book an interview slot.",
    ]
    if questions:
        parts.append("\n# Screening Questions (ask these in order, one at a time)")
        for i, q in enumerate(questions, 1):
            tag = (" — if the answer is NO/disqualifying, politely thank them and end the "
                   "conversation with the literal token [END:reason], where reason is exactly one of: "
                   "age, work_authorization, schedule_availability, commute — whichever gate they failed."
                   ) if q.get("auto_screen") else ""
            parts.append(f"{i}. {q.get('question','')}{tag}")
    if pipe_extra:
        parts.append("\n# Office-Specific Context\n" + pipe_extra)
    if extra:
        parts.append("\n# Additional Context\n" + extra)
    parts.extend([
        "",
        "# Booking",
        "When the candidate has answered all the screening questions:",
        "1. Tell them you'd like to book the interview.",
        "2. The system will inject a list of available slots at the start of your context. Offer the FIRST TWO entries: 'I have <Day> at <Time> or <Day> at <Time> — which works best?'.",
        "3. If neither works, offer the next two — and stop there: never show more than four times in total. A wall of times reads like a rota, not an invitation.",
        "4. EXCEPTION — if they name a specific day or date, offer up to two times from the list on that day; if that day has none, say so and offer the closest day that does.",
        "5. If none of that lands, send them the SLOT PICKER LINK from the context below — it shows "
        "the next openings and lets them ask to be notified when new times open. (Do NOT point them "
        "at the 'Pick a time' tab on this page — that schedules a phone call, not the interview.)",
        "6. When the candidate picks a slot, reply with EXACTLY the literal token [BOOK:<ISO datetime>] on its own line — do NOT include any other text on that line. Example: [BOOK:2026-05-08T13:15:00+00:00]",
        "7. After booking, send one final friendly confirmation message and end with [END].",
        "",
        "# Closing",
        "When you've finished (rejected, booked, or no questions left), end your last message with [END].",
        "",
        f"Candidate phone: {cand.get('phone','')}",
        f"Pipeline slug: {slug}",
    ])
    base_url = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
    if base_url and cand.get("public_token"):
        parts.append(f"SLOT PICKER LINK: {base_url}/reschedule/{cand['public_token']}")
    return "\n".join(parts)


async def _fetch_slots_for_chat(pipeline: Dict[str, Any], settings: Dict[str, Any]) -> list:
    """Compute the same slots the voice agent's get_available_slots tool returns."""
    from availability_service import compute_available_slots, candidate_window_days
    region = settings.get("region_language") or {}
    tz_name = region.get("timezone") or default_tz_name()
    appt = settings.get("appointments") or {}
    # Honour the pipeline's booking window. This door used to look 60 days
    # ahead regardless, so when near days were full the chat quietly booked
    # exactly the far-out slots the window exists to forbid.
    days = candidate_window_days(pipeline, appt, tz_name)
    default_cap = int(appt.get("applicant_limit") or 50)
    booked = await db.candidates.find(
        {"pipeline_id": pipeline.get("id"), "appointment_at": {"$ne": None}},
        {"_id": 0, "appointment_at": 1},
    ).to_list(2000)
    booked_isos = [c.get("appointment_at") for c in booked if c.get("appointment_at")]
    return compute_available_slots(pipeline, booked_isos, days_ahead=days, tz_name=tz_name, default_capacity=default_cap)


RETRY_CHAT_MAX_CHARS = 2000
RETRY_CHAT_TURNS_PER_HOUR = 60
RETRY_CHAT_MAX_LOG = 600


def _retry_chat_over_limit(chat_log, now=None) -> bool:
    """True when this candidate's web chat has had too many of their own turns
    in the last hour, or the stored log is already as long as we keep."""
    if len(chat_log) >= RETRY_CHAT_MAX_LOG:
        return True
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=1)
    recent = 0
    for m in chat_log:
        if m.get("role") != "applicant" or m.get("channel") != "retry":
            continue
        try:
            at = datetime.fromisoformat(str(m.get("at") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if at >= cutoff:
            recent += 1
    return recent >= RETRY_CHAT_TURNS_PER_HOUR


@router.post("/public/retry/{token}/chat")
async def retry_chat_turn(token: str, payload: Dict[str, Any] = Body(...)):
    """Single chat turn — append the candidate's message, ask Claude for a reply
    (using the screening system prompt), persist both turns to candidate.chat_log,
    and detect [BOOK:<iso>] / [END] sentinels to side-effect bookings + completion."""
    text = payload.get("text") or ""
    if not isinstance(text, str):
        raise HTTPException(400, "text must be a string")
    # The token comes with every application, and each turn sends the whole
    # chat to the LLM and grows the candidate's record. Bound both: the message
    # size, the turns per hour, and the length of the log.
    text = text.strip()[:RETRY_CHAT_MAX_CHARS]
    if not text:
        raise HTTPException(400, "text is required")
    cand = await db.candidates.find_one({"public_token": token}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Application not found")
    if _retry_chat_over_limit(cand.get("chat_log") or []):
        return {
            "reply": "That's a lot of messages in a short time. Take a break and come back "
                     "a little later, and we'll pick up where we left off.",
            "booked_at": None, "ended": False,
        }

    # A booked candidate is past screening — this chat is closed. Appointment
    # questions live on their portal and in the SMS thread (which has the
    # confirmation/reminder context this page doesn't).
    if cand.get("appointment_at"):
        return {
            "reply": "You're all booked in — this screening chat is closed. "
                     "Everything about your interview is on your status page.",
            "booked_at": cand.get("appointment_at"), "ended": True,
        }

    # This is a public, unauthenticated endpoint — the same guard the SMS path
    # runs. A candidate typing the agent's own control markers ([BOOK:], [END])
    # or trying to reprogram the assistant is caught here, before the model is
    # asked, rather than being handed to the model to interpret. Tone/abuse is
    # left to the model's own [FLAG] since a regex cannot judge it.
    from conversation_guard import evaluate_inbound
    if not evaluate_inbound(text).should_reply:
        return {
            # No human queue exists — never promise follow-up the system won't
            # deliver. Point them back at the channels that do work.
            "reply": "Thanks — I've noted that. If you have a question about the role "
                     "or your interview, just ask it here and I'll help.",
            "booked_at": None, "ended": False,
        }
    if int(cand.get("screening_attempts") or 0) > 3 and cand.get("screening_status") not in (
        "approved", "incomplete_info", "no_answer", "didnt_connect",
    ):
        # Allow chat to continue even if max retries hit, but log it.
        logger.info(f"retry chat continuing past attempt cap for {cand['id']}")

    pipeline = await db.pipelines.find_one({"id": cand.get("pipeline_id")}, {"_id": 0}) or {}
    settings = await resolve_settings(cand["user_id"], cand.get("pipeline_id"))
    sca = settings.get("screen_call_agent") or {}
    profile = settings.get("recruiter_profile") or {}
    company = profile.get("company_name") or company_profile.company_name()
    job = await db.jobs.find_one({"id": cand.get("job_id")}, {"_id": 0}) if cand.get("job_id") else {}
    job_title = (job or {}).get("title", "")

    # Persist candidate's message immediately. retry_chat_last_at drives the
    # abandoned-chat nudge sweep (dialer/retry_chat_nudge_sweep.py) and is reset
    # here so a candidate resuming an old chat clears any stale nudge eligibility.
    cand_msg = {"role": "applicant", "channel": "retry", "text": text, "at": now_iso()}
    await db.candidates.update_one(
        {"id": cand["id"]},
        {
            "$push": {"chat_log": cand_msg},
            # retry_chat_nudge_count resets alongside the stamp: a candidate who
            # comes back and engages has earned a fresh sequence rather than
            # inheriting "we already chased this one three times".
            "$set": {"updated_at": now_iso(), "retry_chat_last_at": now_iso(),
                     "retry_chat_nudge_sent_at": None, "retry_chat_nudge_count": 0},
        },
    )
    # Speed to contact: the first thing the candidate says back, on whichever
    # channel they say it. Set once — the filter on first_reply_at makes this a
    # no-op on every later message, so it stays the *first* reply.
    if not cand.get("first_reply_at"):
        await db.candidates.update_one(
            {"id": cand["id"], "first_reply_at": None},
            {"$set": {"first_reply_at": now_iso(), "first_reply_channel": "chat"}},
        )
    # An actively-chatting candidate must not be interrupted by the chat_first
    # fallback call — each web-chat turn pushes it out, same as an SMS reply.
    try:
        from training_sms import _defer_chat_first_call
        await _defer_chat_first_call(db, cand)
    except Exception as e:
        logger.warning(f"chat_first call deferral failed for {cand.get('id')}: {e}")

    # Conversation history across BOTH channels — someone who answered three
    # gates by SMS and then tapped into the web chat is mid-conversation, and
    # the model must see those SMS answers or it restarts from question 1.
    history = await _screening_thread_for(cand)
    history.append(cand_msg)

    # Feed available slots into the system prompt as a JSON block (cheap RAG).
    slots = await _fetch_slots_for_chat(pipeline, settings)
    system_prompt = _build_retry_chat_system_prompt(
        sca, pipeline, cand, job_title, company, pipeline.get("public_slug", ""),
    )
    # Resume context: prior phone-call transcript + earlier retry-chat turns.
    resume_ctx = await _build_resume_context(cand)
    if resume_ctx:
        system_prompt += "\n\n# Resume / Continuation\nThe candidate has already started this screening on a prior attempt. Acknowledge it warmly with something like 'Hey {first}, thanks for jumping back in! Let's pick up where we left off.' Then SKIP any questions they've already answered, and only ask what's still missing.\n\nPrior attempts:\n" + resume_ctx
    if slots:
        slot_lines = [f"- {s['label']} ({s['datetime']})" for s in slots[:10]]
        system_prompt += "\n\n# Available Slots (today's snapshot — recruiter timezone)\n" + "\n".join(slot_lines)
    else:
        system_prompt += "\n\n# Available Slots\nNo slots returned by availability engine."

    # Call Claude (ANTHROPIC_API_KEY; EMERGENT_LLM_KEY is the legacy name).
    api_key = __import__("llm_config").api_key()
    if not api_key:
        raise HTTPException(503, "The AI assistant isn't configured yet")
    from anthropic import AsyncAnthropic
    if len(history) > 1:
        _agent_label = sca.get("agent_name") or "Aria"
        prior = "\n".join(
            f"{'Candidate' if m['role']=='applicant' else _agent_label}: {m['text']}"
            for m in history[:-1]
        )
        user_text = f"# Conversation so far (do not greet again, continue):\n{prior}\n\n# Latest candidate message\n{text}"
    else:
        user_text = text
    # Speed with common sense (the owner's words): asking whether someone is 18
    # doesn't need a frontier model, and the Sonnet 5 trial brought the
    # rate-limit lag that made the SMS agent apologise all night. Both
    # candidate-facing agents run the fast, proven pair. (Sonnet 4.5 retires
    # 2026-11-30 — the fallback is now Haiku 4.5, as in training_sms.)
    # ANTHROPIC_MODEL, then ANTHROPIC_FAST_MODEL as the fallback (llm_config.py).
    import llm_config
    _MODELS = llm_config.chat_models()
    reply = ""
    try:
        client = AsyncAnthropic(api_key=api_key)
        for _i, _model in enumerate(_MODELS):
            try:
                resp = await client.messages.create(
                    model=_model,
                    max_tokens=2048,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_text}],
                    **llm_config.low_latency_params(_model),
                )
                reply = llm_config.response_text(resp)
                break
            except Exception as e:
                if _i == len(_MODELS) - 1:
                    raise
                logger.warning(f"retry chat: {_model} failed, falling back: {str(e)[:140]}")
    except Exception as e:
        logger.exception(f"retry chat LLM failed: {e}")
        raise HTTPException(502, "AI assistant unavailable — please try again shortly")
    reply_text = (reply or "").strip()

    # Detect [BOOK:<iso>] sentinel — atomically schedule the candidate.
    booked_iso: Optional[str] = None
    final_text = reply_text
    if "[BOOK:" in reply_text:
        try:
            start = reply_text.index("[BOOK:") + len("[BOOK:")
            end = reply_text.index("]", start)
            booked_iso = reply_text[start:end].strip()
            final_text = (reply_text[:reply_text.index("[BOOK:")] + reply_text[end + 1:]).strip()
        except Exception:
            booked_iso = None
    if booked_iso:
        # Two-layer validation, and on ANY refusal the model's reply is
        # REPLACED — its text says "you're booked", and sending that while
        # writing nothing promises an interview that does not exist (the SMS
        # path has said "that time just went" since day one; this page
        # silently confirmed phantoms instead).
        #   1. Membership of the snapshot list — rejects hallucinated times.
        #   2. book_screening_slot — the shared booking door (same one SMS
        #      uses), which re-checks capacity at write time so two candidates
        #      chatting at once can't both take the last seat. It also sends
        #      approval comms, schedules reminders, cancels pending retry
        #      calls and leaves the Comms-tab trace.
        valid_isos = {s["datetime"] for s in slots}
        refused = None
        if booked_iso not in valid_isos:
            logger.warning(f"retry chat: AI emitted [BOOK:{booked_iso}] which is not in available slots; refusing")
            refused = "hallucinated"
        else:
            from screening_outcome import book_screening_slot
            res = await book_screening_slot(db, cand, pipeline, booked_iso)
            if not res.get("ok"):
                logger.warning(f"retry chat: booking {booked_iso} refused for {cand['id']}: {res.get('reason')}")
                refused = res.get("reason")
        if refused:
            booked_iso = None
            base_url = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
            picker = f" You can also pick any open time here: {base_url}/reschedule/{cand.get('public_token')}" \
                if (base_url and cand.get("public_token")) else ""
            final_text = (
                "Ah — that time just went. Tell me another that suits you and "
                "I'll get it locked in." + picker
            )

    # Detect [END] / [END:reason]
    # Belt-and-braces on the prompt's no-markdown rule: this renders in plain
    # bubbles, where **bold** arrives as literal asterisks around every date.
    final_text = final_text.replace("**", "")

    # The screener states WHY it ended ([END:age]) so the outcome doesn't
    # depend on the summariser re-deriving it. A candidate said "No" to 18-or-over,
    # the chat ended correctly — and the summariser filed the two-turn thread
    # as "incomplete", so nothing was rejected and her queued retry CALL
    # stayed armed. The agent that made the decision now declares it.
    _end_m = re.search(r"\[END(?::([a-z_]+))?\]", final_text)
    ended = bool(_end_m)
    end_reason = (_end_m.group(1) or None) if _end_m else None
    if ended:
        final_text = re.sub(r"\[END(?::[a-z_]+)?\]", "", final_text).strip()

    # Persist Aria's reply. When the chat legitimately concluded ([END] — booked
    # or disqualified), clear retry_chat_last_at so the abandoned-chat nudge sweep
    # (which ranges on that field) doesn't mistake a finished conversation for one
    # the candidate walked away from mid-way.
    aria_msg = {"role": "agent", "channel": "retry", "text": final_text, "at": now_iso()}
    end_update: Dict[str, Any] = {"updated_at": now_iso()}
    if ended or booked_iso:
        end_update["retry_chat_last_at"] = None
    await db.candidates.update_one(
        {"id": cand["id"]},
        {"$push": {"chat_log": aria_msg}, "$set": end_update},
    )

    # The conversation is over — now say what it concluded.
    #
    # [END] is emitted both when a candidate books and when they fail a hard
    # gate, and until now the two were indistinguishable to everything
    # downstream: no verdict, no score, no summary, no rejection, no
    # notification. A 17-year-old finished the chat and then sat in the pipeline
    # being phoned by the dialler, because nothing had marked them rejected.
    outcome = None
    # `or booked_iso`: the prompt says book-confirm-[END] in one message, but a
    # model that books without [END] used to leave the booking unconcluded
    # forever — the next turn hits the booked-candidate guard at the top and
    # returns before any of this runs, so no verdict was ever written.
    if ended or booked_iso:
        try:
            fresh = await db.candidates.find_one({"id": cand["id"]}, {"_id": 0}) or cand
            from screening_outcome import conclude_text_screening
            outcome = await conclude_text_screening(
                db, fresh, channel="retry", booked=bool(booked_iso),
                disq_hint=end_reason,
            )
        except Exception as e:
            # The candidate has already had their reply written; failing to
            # score it must not turn into a 500 in their browser.
            logger.warning(f"retry chat: could not conclude screening for {cand['id']}: {e}")

    return {
        "reply": final_text,
        "booked_at": booked_iso,
        "ended": ended,
        "verdict": (outcome or {}).get("verdict"),
    }


async def _build_resume_context(cand: Dict[str, Any]) -> str:
    """Build a brief plain-text summary of the candidate's prior screening attempts
    so Aria can pick up where they left off. Combines:
      • the latest phone call's transcript (if any)
      • any previous retry-chat turns

    Returns an empty string if there's nothing to resume from."""
    user_id = cand.get("user_id")
    candidate_id = cand.get("id")
    if not (user_id and candidate_id):
        return ""

    bits: list = []

    # Phone call transcript (most recent completed call).
    conv = await db.conversations.find_one(
        {"candidate_id": candidate_id, "user_id": user_id, "transcript": {"$exists": True, "$ne": []}},
        {"_id": 0, "transcript": 1, "summary": 1, "created_at": 1},
        sort=[("created_at", -1)],
    )
    if conv:
        if conv.get("summary"):
            bits.append(f"Earlier phone call summary: {conv['summary'].strip()}")
        else:
            turns = (conv.get("transcript") or [])[-12:]  # last 12 turns is enough context
            if turns:
                bits.append("Earlier phone call transcript (most recent turns):")
                for t in turns:
                    role = "Olivia" if t.get("role") in ("agent", "assistant") else "Candidate"
                    text = (t.get("text") or t.get("message") or "").strip()
                    if text:
                        bits.append(f"- {role}: {text}")

    # Prior retry-chat history (already on the candidate doc).
    retry_msgs = [m for m in (cand.get("chat_log") or []) if (m.get("channel") or "") == "retry"]
    if retry_msgs:
        bits.append("\nEarlier retry chat:")
        for m in retry_msgs[-20:]:
            role = "Olivia" if m.get("role") == "agent" else "Candidate"
            bits.append(f"- {role}: {(m.get('text') or '').strip()}")

    if cand.get("appointment_at"):
        bits.append(f"\nNote: candidate already has an appointment booked at {cand['appointment_at']}.")

    return "\n".join(bits).strip()


_CALLBACK_COOLDOWN_MINUTES = 3


@router.post("/public/retry/{token}/callback")
async def request_callback(token: str):
    """Candidate-triggered instant callback — places an outbound AI screening call
    to the candidate's phone immediately (same flow as the auto-dialer).
    Builds resume context from prior transcripts so the agent picks up where it left off."""
    cand = await db.candidates.find_one({"public_token": token}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Application not found")
    if not cand.get("phone"):
        raise HTTPException(400, "No phone number on file")
    if cand.get("screening_status") in ("approved", "rejected"):
        raise HTTPException(400, "Screening already complete")
    # One call per few minutes: the link is a bearer token, and every POST
    # places a real, billable call.
    last = cand.get("callback_requested_at")
    if last:
        try:
            last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=pytz.UTC)
            if now_utc() - last_dt < timedelta(minutes=_CALLBACK_COOLDOWN_MINUTES):
                raise HTTPException(429, "We're already calling you — give it a couple of minutes.")
        except HTTPException:
            raise
        except Exception:
            pass
    await db.candidates.update_one({"id": cand["id"]}, {"$set": {"callback_requested_at": now_iso()}})
    try:
        # Store resume context on the candidate doc so build_dynamic_variables picks it up.
        resume_ctx = await _build_resume_context(cand)
        if resume_ctx:
            await db.candidates.update_one(
                {"id": cand["id"]},
                {"$set": {"previous_context": resume_ctx, "updated_at": now_iso()}},
            )
        from dialer.place_call import place_call_now
        result = await place_call_now(cand["user_id"], cand["id"], candidate_initiated=True)
        logger.info(f"callback requested by candidate {cand['id']}: {result.get('status')}")
        return {"ok": True, "status": result.get("status")}
    except Exception as e:
        logger.exception(f"callback failed for {cand['id']}: {e}")
        raise HTTPException(502, "Could not place call — please try again shortly")


@router.post("/public/retry/{token}/schedule-call")
async def schedule_call_at_time(token: str, payload: Dict[str, Any] = Body(...)):
    """Candidate-chosen callback time — reschedules their pending screening call to a
    specific instant they picked (one of the `call_time_options` from GET /public/retry/{token},
    or any other iso timestamp within the allowed range). Reuses `schedule_call_with_window`
    with `force=True` so it dedupes any pending call/SMS job first and still schedules the
    pre-call warmup SMS ahead of the new time — no new scheduling primitives needed."""
    run_at_raw = (payload.get("run_at") or "").strip()
    if not run_at_raw:
        raise HTTPException(400, "run_at is required")
    try:
        run_at = datetime.fromisoformat(run_at_raw.replace("Z", "+00:00"))
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=pytz.UTC)
    except Exception:
        raise HTTPException(400, "run_at must be a valid ISO 8601 timestamp")

    cand = await db.candidates.find_one({"public_token": token}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Application not found")
    if not cand.get("phone"):
        raise HTTPException(400, "No phone number on file")
    if cand.get("screening_status") in ("approved", "rejected"):
        raise HTTPException(400, "Screening already complete")

    now = now_utc()
    if run_at < now + timedelta(minutes=2):
        raise HTTPException(400, "Pick a time at least a couple of minutes from now")
    if run_at > now + timedelta(days=14):
        raise HTTPException(400, "Pick a time within the next two weeks")

    try:
        from dialer.queue import schedule_call_with_window
        delay_minutes = (run_at - now).total_seconds() / 60
        sched = await schedule_call_with_window(cand["user_id"], cand["id"], base_delay_minutes=delay_minutes, force=True)
        logger.info(f"candidate {cand['id']} picked callback time {run_at.isoformat()} ({sched.get('scheduled_at')})")
        if sched.get("status") != "scheduled":
            # force=True clears every gate except a recruiter's pause, so this is
            # almost always "dialing paused by recruiter". Falling back to the
            # requested time would promise the candidate a call nobody booked.
            logger.info(f"callback time declined for {cand['id']}: {sched.get('reason')}")
            raise HTTPException(409, "We can't book a call right now — someone will be in touch shortly")
        return {"ok": True, "scheduled_at": sched["scheduled_at"]}
    except HTTPException:
        raise  # the 409 above is a real answer, not a failure to report as 502
    except Exception as e:
        logger.exception(f"schedule-call failed for {cand['id']}: {e}")
        raise HTTPException(502, "Could not schedule that time — please try again shortly")
