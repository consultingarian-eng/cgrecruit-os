"""Automated training-confirmation SMS flow for CGRecruit.

Flow:
1. APScheduler tick runs every 5 minutes (started in server.py on startup).
2. For each candidate in TRAINING whose training_start_at minus LEAD_HOURS
   falls within the last WINDOW_MIN minutes (and hasn't been sent yet), we
   fire a confirmation SMS from the pipeline's twilio_phone_number.
3. Recipient replies; Twilio POSTs to /api/webhooks/twilio/inbound-sms.
   We match by (To number → pipeline → candidate with matching phone) and
   update the candidate's sms_confirmation_status.

SMS state stored directly on candidate documents:
  sms_confirmation_sent_at     ISO timestamp when SMS went out
  sms_confirmation_status      "sent" | "confirmed" | "declined" | "opted_out" | "rescheduled"
                               ("rescheduled" = moved to a new date, awaiting the
                                pre-start re-confirm SMS; not a clean confirmation)
  sms_confirmed_at             ISO timestamp of YES/NO reply
  sms_opted_out                True if recipient sent STOP
  sms_confirmation_sid         Twilio message SID
  sms_confirmation_error       error string from last failed attempt
  sms_proposed_reschedule_date ISO date offered by LLM (next Monday)

Thread stored in training_sms_messages collection:
  candidate_id, pipeline_id, direction ("in"/"out"), body,
  sender, recipient, classification, timestamp, was_llm_reply
"""
import asyncio
import os
import re
import time
import logging
import company_profile
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, List, Dict
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Response

from phone_util import same_phone
from app_tz import app_zone, default_tz_name  # APP_TIMEZONE (noqa: F401)

logger = logging.getLogger(__name__)
router = APIRouter()

ET = app_zone()
LEAD_HOURS = float(os.getenv("TWILIO_DEFAULT_LEAD_HOURS", "3"))
WINDOW_MIN = 6
# Replaced a flat "stop replying after 5 inbound messages, ever" cap. That cut a
# genuine candidate off partway through a six-question screening — they'd get a
# canned "Reply YES to confirm attendance" mid-answer — while giving somebody
# actually abusing the line five free replies first. Message count was never the
# signal. See conversation_guard: injection is caught on the text, tone is judged
# by the model via a [FLAG:...] marker, and the ceiling below exists only to stop
# a runaway loop, far above any real conversation.
from conversation_guard import (  # noqa: E402
    FLAG_INSTRUCTIONS, OPT_OUT_CONFIRMATION, RUNAWAY_CEILING,
    is_hard_stop, looks_like_opt_out,
)

MAX_REPLY_LEN = 300

PROPOSED_DATE_RE = re.compile(r"\[\s*PROPOSED_DATE\s*:\s*(\d{4}-\d{2}-\d{2})\s*\]", re.IGNORECASE)
# The assistant moves interviews itself rather than only handing out a link, so it
# needs to say which slot and to say when someone has dropped out. Both are only
# ever honoured against the live availability list — see _extract_markers.
BOOK_RE = re.compile(r"\[\s*BOOK\s*:\s*([0-9T:+\-.Z ]+?)\s*\]", re.IGNORECASE)
CANCEL_RE = re.compile(r"\[\s*CANCEL\s*\]", re.IGNORECASE)
# [WITHDRAW] is the model's own read that this person is leaving the process
# for good, as opposed to [CANCEL] which only drops one appointment. It exists
# because the phrase list below can only ever catch wordings we have already
# been burned by: when one candidate wrote "I have accepted another position"
# the model replied perfectly ("congratulations on the new position!") while
# the classifier returned "unknown" and nothing moved. Understanding and action
# now come from the same place.
WITHDRAW_RE = re.compile(r"\[\s*WITHDRAW\s*\]", re.IGNORECASE)
# Taught to BOTH the APPOINTMENT and TRAINING prompts. It used to live only in
# the APPOINTMENT block, so a new starter's model never knew the marker existed:
# a TRAINING-stage starter who wrote "I got another offer that is more what I
# am looking for" got a warm goodbye, matched no phrase, and stayed on the
# Monday new-starts list.
WITHDRAW_MARKER_INSTRUCTIONS = (
    "WITHDRAWN MARKER:\n"
    "If they are leaving the process for good — took another job or offer, changed their mind, "
    "resigning, asking to be removed — append `[WITHDRAW]` at the very end. Use it however "
    "politely or gratefully they put it: 'I've accepted another position' and 'I got another "
    "offer' are withdrawals. This takes them off the board and stops every reminder. Reply "
    "warmly and wish them well; do not try to talk them round.\n"
)
# SCREENING/APPLICANT self-serve markers — mirror BOOK/CANCEL's pattern for the
# pre-screening chat/callback/pick-a-time options (see routes/retry.py).
REQUEST_CALLBACK_RE = re.compile(r"\[\s*REQUEST_CALLBACK\s*\]", re.IGNORECASE)
SCHEDULE_CALL_RE = re.compile(r"\[\s*SCHEDULE_CALL\s*:\s*([0-9T:+\-.Z ]+?)\s*\]", re.IGNORECASE)

YES_TOKENS = {"yes","y","yep","yeah","ya","yup","ok","okay","k","confirmed","confirm","👍","✅","✔","✔️","👌"}
YES_PHRASES = ["i'll be there","ill be there","i will be there","be there","see you","see u","see ya","cu ","coming","i'm coming","im coming","sounds good","sounds great","looking forward","can't wait","cant wait"]
# "cant"/"can't" are deliberately NOT bare no-tokens: "Can't wait!" is the most
# enthusiastic reply a booked candidate can send, and the first-token check was
# filing it as a decline. Every real can't-decline is a phrase ("can't make",
# "can't attend", "have to cancel"...) and NO_PHRASES carries all of them.
NO_TOKENS = {"no","n","nope","nah","cancel","decline","declined","❌","👎"}
NO_PHRASES = ["can't make","cant make","won't make","wont make","not coming","not going to make","no longer","not interested","got another job","other job","found a job","i'm out","im out","not be there","won't be there","wont be there","can't be there","cant be there","not gonna make",
              # Seen in the wild on real declines that classified as "unknown" and
              # so never stopped the reminder cascade.
              "not able to attend","unable to attend","can't attend","cant attend","cannot attend",
              "not able to make","won't be able","wont be able","unable to make",
              "have to cancel","need to cancel","must cancel"]
STOP_TOKENS = {"stop","unsubscribe","stopall","end","quit"}
# Phrases that signal the candidate is withdrawing from the process entirely
# (not just declining a specific session). These trigger auto-rejection + call cancellation.
WITHDRAWN_PHRASES = ["not interested","got another job","other job","found a job","i'm out","im out","no longer interested","withdraw","withdrawing","not looking","not looking anymore",
                     # Added when SMS started running the screening itself. Until
                     # then a plain "no" reached the withdrawal branch anyway, so
                     # these being missing rarely showed. Now that "no" is treated
                     # as an answer to the question just asked, someone actually
                     # asking to be left alone has to be recognised on their own
                     # words — anything not matched here goes to the model instead
                     # of being honoured immediately, and being ignored is the one
                     # response this message must never get.
                     "take me off","remove me","stop contacting","don't contact","do not contact",
                     "leave me alone","unsubscribe","changed my mind","no longer want",
                     # Every phrase above is how somebody leaves BEFORE they start.
                     # Nothing covered leaving after: a new starter resigns in the
                     # language of employment, not of applications. A starter who
                     # wrote "I have decided not to continue my employment with
                     # <company>" matched none of it, so stayed in TRAINING with
                     # reminders live until a human archived them by hand.
                     "not to continue my employment","decided not to continue",
                     "not be continuing","won't be continuing","will not be continuing",
                     "no longer wish to continue","no longer be working",
                     "hand in my notice","handing in my notice","resign","resigning",
                     # "Unfortunately I have accepted another position however I
                     # would like to stay in touch": a polite, grateful withdrawal
                     # matched nothing here, so the candidate stayed on the kanban
                     # and the NEW HIRES sheet until someone archived them by hand. People decline an offer in the
                     # language of offers, not of jobs.
                     "accepted another position","accepted a position","accepted another offer",
                     "accepted an offer","accepted another role","accepted a role",
                     "taken another position","taken another offer","taken another role",
                     "going with another company","gone with another company",
                     "another opportunity","other opportunity","decided to decline",
                     "have to decline","going to decline","won't be taking","wont be taking",
                     "not taking the position","not taking the role","not accepting",
                     # "I got another offer that is more what I am looking for".
                     # Got/received, not accepted.
                     "got another offer","got a better offer","received another offer",
                     "another job offer","took another job","accepted another job",
                     "taken another job","got a different job"]


# ─── helpers ────────────────────────────────────────────────────────────────


def _is_withdrawn(body: str) -> bool:
    """True if the message signals the candidate is withdrawing from the process entirely."""
    lower = body.lower().strip()
    return any(p in lower for p in WITHDRAWN_PHRASES)


async def _apply_withdrawn(db, candidate_id: str, user_id: str, trigger_text: str = "") -> None:
    """Archive + reject candidate and cancel all pending calls/reminders.
    Called when a candidate opts out via SMS STOP, a withdrawal phrase, or an
    email cancel reply. `trigger_text` is the message that caused it — quoted
    in the CG1 admin alert so the office reads the goodbye in their own words."""
    now = _now_iso()
    # Read before the archive stamp lands — the alert wants the stage and start
    # date the candidate withdrew FROM.
    cand = await db.candidates.find_one({"id": candidate_id}, {"_id": 0}) or {}
    await db.candidates.update_one(
        {"id": candidate_id},
        {"$set": {
            "screening_status": "rejected",
            "verdict": "withdrawn",
            "archived_at": now,
            "archived_reason": "withdrawn",
            # Belt AND buckle: cancel_pending_retry_calls below kills the
            # in-memory job, but the startup recovery re-arms calls from
            # next_call_at after a deploy — a candidate who withdrew at 15:22
            # was redialled at 17:25 because this field survived her archive.
            "next_call_at": None,
            "auto_dial": False,
            "updated_at": now,
        }},
    )
    try:
        from auto_dialer import cancel_pending_retry_calls, cancel_appointment_reminders, cancel_form_reminder
        cancel_pending_retry_calls(candidate_id)
        cancel_appointment_reminders(candidate_id)
        cancel_form_reminder(candidate_id)
    except Exception as e:
        import logging as _logging
        _logging.getLogger("training_sms").warning(f"_apply_withdrawn cancel jobs failed for {candidate_id}: {e}")
    # Every withdrawal route clears the sheet, not just the [WITHDRAW] marker's.
    # The phrase path archived first, so the marker path then saw archived_at
    # and skipped its own removal — a phrase-caught starter stayed in their
    # NEW HIRES cohort.
    if (cand.get("stage") or "").upper() == "TRAINING":
        await _remove_from_new_hires_sheet(db, cand)
    # Deliberately NO CG1 page here (owner's rule, 2026-08-18): office admins
    # don't want stop/withdraw notifications — the roster shrinking is enough.
    # Only genuine questions/correspondence from TRAINING candidates page CG1
    # (see the trainee-correspondence watch in handle_inbound).


async def _remove_from_new_hires_sheet(db, candidate: dict) -> None:
    """Clear a withdrawn starter off the NEW HIRES tab.

    Archiving them in CGRecruit is not enough on its own: the office works the
    Google Sheet, so a candidate who withdrew stayed in their cohort block and
    got counted, prepared for, and set a desk. The row is blanked rather than
    deleted, so the cohort's row positions and everyone else's attendance
    checkboxes stay where they are. Non-fatal — never block a withdrawal on
    Sheets being down."""
    try:
        pipe = await db.pipelines.find_one({"id": candidate.get("pipeline_id")}, {"_id": 0}) or {}
        office_key = company_profile.office_key_for_pipeline(pipe)
        if not office_key:
            return
        name = f"{candidate.get('first_name', '')} {candidate.get('last_name', '')}".strip()
        import asyncio as _asyncio
        from sheets_service import remove_new_hire
        await _asyncio.get_event_loop().run_in_executor(
            None, lambda: remove_new_hire(office_key, candidate.get("email") or "", name)
        )
    except Exception as e:
        logger.warning("training_sms: NEW HIRES sheet removal failed for %s: %s",
                       candidate.get("id"), e)


async def _apply_appointment_cancellation(db, candidate: dict, reason_text: str) -> None:
    """A booked candidate has told us by text they can't attend.

    Until this existed the message was answered warmly and otherwise ignored:
    the reminder cascade kept running, so someone who apologised a day early
    still got 'your interview is in 1 hour' and then 'starts in 10 min'. The
    cancellation was recorded nowhere, so they also looked like a no-show
    afterwards rather than someone who told us in good time.

    Deliberately keeps them in APPOINTMENT and unarchived — they have not
    withdrawn, they need a new slot, and the watchlist/slot-picker sweeps only
    look at live candidates."""
    cid = candidate["id"]
    if candidate.get("appointment_cancelled_at"):
        return  # already recorded for this slot — don't re-notify on every follow-up
    now = _now_iso()
    await db.candidates.update_one(
        {"id": cid},
        {"$set": {
            "appointment_cancelled_at": now,
            "appointment_cancel_reason": (reason_text or "")[:500],
            "appointment_cancelled_slot": candidate.get("appointment_at"),
            # They told us in advance — the confirmation chaser must not treat
            # silence-after-this as an unconfirmed booking.
            "appointment_sms_confirmed": False,
            "appointment_sms_confirmed_at": None,
            "screening_status": "appointment_pending",
            "updated_at": now,
        }},
    )
    try:
        from auto_dialer import cancel_appointment_reminders, cancel_pending_retry_calls
        cancel_appointment_reminders(cid)
        cancel_pending_retry_calls(cid)
    except Exception as e:
        logger.warning("training_sms: cancel jobs failed for %s: %s", cid, e)
    try:
        from notifications_service import create_notification
        nm = f"{candidate.get('first_name', '')} {candidate.get('last_name', '')}".strip() or "A candidate"
        await create_notification(
            candidate.get("user_id", ""), "appointment.cancelled_by_candidate",
            f"🚫 {nm} can't attend their interview",
            body=(reason_text or "")[:180],
            link=f"/inbox?candidate={cid}",
            candidate_id=cid, pipeline_id=candidate.get("pipeline_id"),
        )
    except Exception as e:
        logger.warning("training_sms: cancellation notification failed for %s: %s", cid, e)
    logger.info("training_sms: appointment cancelled by candidate %s", cid)


async def _apply_sms_reschedule(db, candidate: dict, pipeline: dict, slot_iso: str) -> bool:
    """Move a candidate's interview to `slot_iso` because they agreed to it by text.

    Returns False (and changes nothing) if the slot isn't one the availability
    engine is currently offering — the model is not allowed to invent a time,
    and a phantom booking is worse than an apology."""
    from availability_service import slot_capacity_remaining, resolve_slot_link, resolve_slot_recruiter
    from deps import resolve_settings

    cid = candidate["id"]
    user_id = candidate.get("user_id") or pipeline.get("user_id") or ""
    pipeline_id = candidate.get("pipeline_id") or pipeline.get("id") or ""
    settings = await resolve_settings(user_id, pipeline_id) or {}
    tz_name = (settings.get("region_language") or {}).get("timezone") or default_tz_name()
    booked = await db.candidates.find(
        {"pipeline_id": pipeline_id, "appointment_at": {"$ne": None}, "id": {"$ne": cid}},
        {"_id": 0, "appointment_at": 1},
    ).to_list(2000)
    remaining = slot_capacity_remaining(
        pipeline,
        [b.get("appointment_at") for b in booked if b.get("appointment_at")],
        slot_iso,
        default_capacity=int((settings.get("appointments") or {}).get("applicant_limit") or 50),
        tz_name=tz_name,
    )
    if remaining == -1:
        logger.warning("training_sms: refused [BOOK:%s] for %s — not a real slot", slot_iso, cid)
        return False
    try:
        slot_dt = datetime.fromisoformat(slot_iso.replace("Z", "+00:00"))
    except Exception:
        logger.warning("training_sms: refused [BOOK:%s] for %s — unparseable", slot_iso, cid)
        return False
    if slot_dt <= datetime.now(slot_dt.tzinfo or ET):
        logger.warning("training_sms: refused [BOOK:%s] for %s — in the past", slot_iso, cid)
        return False

    prev_at = candidate.get("appointment_at")
    now = _now_iso()
    from deps import booking_link_or_alert
    _resolved_link = await booking_link_or_alert(pipeline, slot_iso, tz_name, candidate)
    update = {
        "stage": "APPOINTMENT",
        "appointment_at": slot_iso,
        "previous_appointment_at": prev_at,
        "rescheduled": True,
        "rescheduled_at": now,
        "screening_status": "approved",
        "attendance_status": None,
        # Fresh slot, fresh confirmation — the chaser must re-ask for this one.
        "appointment_sms_confirmed": None,
        "appointment_sms_confirmed_at": None,
        "appointment_cancelled_at": None,
        "appointment_cancel_reason": None,
        # Never inherit the stored link: it belongs to the OLD slot and would
        # follow them onto a slot that has its own room. Empty when nothing is
        # configured — never a placeholder URL (booking_link_or_alert bells).
        "appointment_link": _resolved_link,
        # Slot's own interviewer first — same reasoning as the link: the stored
        # name belongs to the OLD slot.
        "appointment_recruiter": resolve_slot_recruiter(pipeline, slot_iso, tz_name) or pipeline.get("appointment_recruiter") or "Hiring Team",
        "archived_at": None,
        "archived_reason": None,
        "updated_at": now,
    }
    if int(candidate.get("revival_call_attempts") or 0) > 0 and candidate.get("revival_outcome") != "rebooked":
        update["revival_outcome"] = "rebooked"
    from screening_outcome import preserve_attendance
    _write = {"$set": update}
    _keep = preserve_attendance(candidate)
    if _keep:
        _write["$push"] = _keep
    await db.candidates.update_one({"id": cid}, _write)

    try:
        from auto_dialer import (
            cancel_appointment_reminders, schedule_appointment_reminders,
            cancel_pending_retry_calls,
        )
        cancel_appointment_reminders(cid)
        await schedule_appointment_reminders(user_id, cid, slot_iso)
        cancel_pending_retry_calls(cid)
    except Exception as e:
        logger.warning("training_sms: reminder reschedule failed for %s: %s", cid, e)
    try:
        from deps import send_stage_comms
        fresh = await db.candidates.find_one({"id": cid}, {"_id": 0})
        tpl_key = "appointment_rescheduled"
        tpl = (settings.get("applicant_comms") or {}).get("templates", {}).get(tpl_key) or {}
        if not tpl.get("body") or tpl.get("enabled") is False:
            tpl_key = "approval"
        # Full comms, same as every other reschedule path: the assistant's own
        # reply confirms the time, but only the template carries the Zoom link
        # for the new slot and the .ics invite.
        await send_stage_comms(user_id, fresh, tpl_key)
    except Exception as e:
        logger.warning("training_sms: reschedule comms failed for %s: %s", cid, e)
    try:
        from notifications_service import create_notification
        nm = f"{candidate.get('first_name', '')} {candidate.get('last_name', '')}".strip() or "A candidate"
        create_kwargs = dict(
            body=f"Moved to {slot_dt.astimezone(ET).strftime('%A, %b %-d at %-I:%M %p %Z')} by text.",
            link=f"/inbox?candidate={cid}",
            candidate_id=cid, pipeline_id=pipeline_id,
        )
        await create_notification(
            user_id, "appointment.rescheduled_by_sms",
            f"📅 {nm} moved their interview", **create_kwargs,
        )
    except Exception as e:
        logger.warning("training_sms: reschedule notification failed for %s: %s", cid, e)
    logger.info("training_sms: %s rescheduled to %s by SMS", cid, slot_iso)
    return True


async def _apply_sms_callback_request(db, candidate: dict) -> bool:
    """Candidate asked (by text) for an immediate callback — same action as the
    'Call me now' button on /retry/{token}. Returns False (no call placed) if
    the candidate isn't eligible; caller falls back to an apology message."""
    cid = candidate["id"]
    if not candidate.get("phone"):
        return False
    if (candidate.get("screening_status") or "") in ("approved", "rejected"):
        return False
    try:
        from dialer.place_call import place_call_now
        result = await place_call_now(candidate["user_id"], cid, candidate_initiated=True)
        logger.info("training_sms: callback requested by %s via text: %s", cid, result.get("status"))
        return True
    except Exception as e:
        logger.warning("training_sms: callback request failed for %s: %s", cid, e)
        return False


async def _apply_sms_schedule_call(db, candidate: dict, run_at_iso: str) -> bool:
    """Candidate asked (by text) to be called at a specific time — same action as
    picking a time on /retry/{token}. `run_at_iso` must be one of the datetimes the
    model was shown (AVAILABLE CALLBACK TIMES); still re-validated here so a
    hallucinated timestamp can't silently make it through. Returns False on any
    validation failure; caller falls back to an apology message."""
    cid = candidate["id"]
    if not candidate.get("phone"):
        return False
    if (candidate.get("screening_status") or "") in ("approved", "rejected"):
        return False
    try:
        run_at = datetime.fromisoformat(run_at_iso.replace("Z", "+00:00"))
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)
    except Exception:
        return False
    now = datetime.now(timezone.utc)
    if run_at < now + timedelta(minutes=2) or run_at > now + timedelta(days=14):
        return False
    try:
        from dialer.queue import schedule_call_with_window
        delay_minutes = (run_at - now).total_seconds() / 60
        await schedule_call_with_window(candidate["user_id"], cid, base_delay_minutes=delay_minutes, force=True)
        logger.info("training_sms: %s scheduled callback for %s via text", cid, run_at.isoformat())
        return True
    except Exception as e:
        logger.warning("training_sms: schedule-call failed for %s: %s", cid, e)
        return False


def _parse_ts(raw: str):
    """Parse a timestamp from either outbound log to an aware datetime.

    The two logs disagree on timezone — `training_sms_messages.timestamp` is
    written in ET by `_now_iso()`, `communications.created_at` in UTC by
    `models.now_iso()`. Comparing them as strings is wrong by the offset, which
    at 4 hours is wider than the whole confirm-chaser window."""
    try:
        d = datetime.fromisoformat((raw or "").replace("Z", "+00:00"))
    except Exception:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


async def _last_outbound_was_ai(db, candidate_id: str) -> bool:
    """True if the last thing we texted this candidate was the assistant talking,
    not an automated template. Decides whether a bare 'yes' is confirming their
    existing slot or accepting one the assistant just offered.

    BOTH outbound logs have to be consulted. Templates — the confirm chaser, the
    1h/10m reminders — are written only to `communications`; `_log_message` is
    never called for them. Reading `training_sms_messages` alone therefore
    reported the assistant as "last to speak" however many days ago it actually
    spoke, so every "Y" answering the confirm chaser looked like a reply to the
    assistant mid-conversation. That skipped the branch which stamps
    `appointment_sms_confirmed`, and the confirmations were never recorded: most
    candidates who confirmed by text showed as unconfirmed on the board."""
    try:
        rows = await db.training_sms_messages.find(
            {"candidate_id": candidate_id, "direction": "out"},
            {"_id": 0, "was_llm_reply": 1, "timestamp": 1},
        ).sort("timestamp", -1).limit(1).to_list(1)
        if not (rows and rows[0].get("was_llm_reply")):
            return False
        ai_at = _parse_ts(rows[0].get("timestamp"))
        if ai_at is None:
            return True  # can't date it; keep the old, conservative answer

        comms = await db.communications.find(
            {"candidate_id": candidate_id, "type": "sms", "status": "sent"},
            {"_id": 0, "created_at": 1},
        ).sort("created_at", -1).limit(1).to_list(1)
        tpl_at = _parse_ts(comms[0].get("created_at")) if comms else None
        # Only the assistant having spoken MOST RECENTLY means mid-conversation.
        return tpl_at is None or ai_at >= tpl_at
    except Exception:
        return False


def _guard_decision(db, cand: Optional[dict], body: str, inbound_count: int):
    """Should the AI answer this message at all?

    Decided before the model is called, so a prompt-injection attempt costs
    nothing and — more importantly — is never handed to the model to interpret.
    """
    from conversation_guard import evaluate_inbound

    return evaluate_inbound(
        body,
        inbound_count=inbound_count,
        already_stopped=bool((cand or {}).get("sms_replies_paused")),
    )


async def _defer_chat_first_call(db, cand: Optional[dict]) -> None:
    """Push the chat_first fallback call out past the candidate's latest reply.

    Cancels any pending dial job and re-schedules it chat_first_delay_minutes
    from now (window-clamped), so an actively-texting candidate is never
    interrupted mid-screening by the call that exists only for silence. No-op
    for voice_first/chat_only pipelines and for candidates whose screening has
    already concluded — and never raises: a failed deferral must not break the
    reply that triggered it.
    """
    if not cand or not cand.get("id") or not cand.get("user_id"):
        return
    if (cand.get("screening_status") or "") in ("approved", "rejected") or cand.get("appointment_at"):
        return
    try:
        from deps import resolve_settings
        from models import resolve_screening_mode
        settings = await resolve_settings(cand["user_id"], cand.get("pipeline_id"))
        if resolve_screening_mode(settings) != "chat_first":
            return
        from screening_start import dial_delay_minutes
        from auto_dialer import cancel_pending_call_jobs, schedule_call_with_window
        cancel_pending_call_jobs(cand["id"])
        await schedule_call_with_window(
            cand["user_id"], cand["id"],
            base_delay_minutes=dial_delay_minutes(settings, "chat_first"),
        )
        logger.info("chat_first: fallback call deferred for %s (reply received)", cand.get("id"))
    except Exception as e:
        logger.warning("chat_first call deferral failed for %s: %s", cand.get("id"), e)


async def _pause_auto_replies(db, cand: dict, decision, inbound_body: str) -> None:
    """Stop replying automatically and put a person on it.

    Deliberately does NOT archive or reject. An angry message from a real
    applicant is a complaint, and quietly deleting the complainant is how you
    never find out you have a problem. A human decides what this was.
    """
    from conversation_guard import handoff_notification, HANDOFF

    name = f"{cand.get('first_name', '')} {cand.get('last_name', '')}".strip() or "A candidate"
    try:
        await db.candidates.update_one(
            {"id": cand["id"]},
            {"$set": {
                "sms_replies_paused": True,
                "sms_replies_paused_reason": decision.reason,
                "sms_replies_paused_at": _now_iso(),
            }},
        )
    except Exception as e:
        logger.warning("could not pause auto-replies for %s: %s", cand.get("id"), e)
    # Spam needs no human. Injection DOES: the patterns are heuristics, and a
    # false positive silently dead-ends a real candidate mid-screening — every
    # later message meets a paused thread, nothing auto-unpauses, and until this
    # notification existed no person was ever told. The one who pays for an
    # unwatched pause is never the attacker.
    if decision.action != HANDOFF and decision.reason == "spam":
        logger.info("auto-replies paused for %s (%s) — no handoff needed", cand.get("id"), decision.reason)
        return
    try:
        from notifications_service import create_notification
        note = handoff_notification(name, decision, inbound_body)
        await create_notification(
            cand["user_id"], "sms.replies_paused", note["title"], body=note["body"],
            link=f"/inbox?candidate={cand['id']}",
            candidate_id=cand["id"], pipeline_id=cand.get("pipeline_id"),
        )
    except Exception as e:
        logger.warning("pause notification failed for %s: %s", cand.get("id"), e)


async def _conclude_sms_screening(db, cand: dict, *, booked: bool, disq_hint: Optional[str] = None) -> None:
    """Score a finished SMS screening exactly as a web-chat one is scored.

    Same function, so a candidate screened by text gets the same verdict, score,
    summary, rejection handling and recruiter alert as one screened on the page
    or on a call. Without this the SMS route would repeat the hole chat used to
    have: a failed hard gate leaving no trace and the candidate staying in the
    queue.
    """
    try:
        from screening_outcome import conclude_text_screening
        fresh = await db.candidates.find_one({"id": cand["id"]}, {"_id": 0}) or cand
        await conclude_text_screening(db, fresh, channel="sms", booked=booked, disq_hint=disq_hint)
    except Exception as e:
        logger.warning("could not conclude SMS screening for %s: %s", cand.get("id"), e)


async def _handle_reply_markers(
    db, cand: Optional[dict], pipeline: dict, stage: str, raw: str, inbound_body: str,
) -> Tuple[str, Optional[str]]:
    """Strip the control markers off an LLM reply and act on them.

    Returns (prose to send, proposed_date for the TRAINING next-Monday flow)."""
    from conversation_guard import evaluate_reply, strip_flag

    # A [FLAG:...] the model raised about the message it just read. Stripped
    # first so it can never reach the candidate, then acted on after the reply
    # has been shaped — the candidate still gets the short, calm response the
    # model wrote; what changes is that this is the last one they get.
    flag_decision = evaluate_reply(raw or "")
    raw = strip_flag(raw or "")

    # [END] / [END:reason] means the screening is over — either a hard gate
    # failed (the reason names which) or the model has nothing left to ask.
    # Stripped here so it never reaches a phone. The declared reason feeds
    # conclude as a hint so the outcome doesn't hinge on the summariser
    # re-deriving a rejection from a two-line thread.
    _end_m = re.search(r"\[END(?::([a-z_]+))?\]", raw or "")
    ended = bool(_end_m)
    end_reason = (_end_m.group(1) or None) if _end_m else None
    reopened = "[REOPEN]" in (raw or "")
    raw = re.sub(r"\[END(?::[a-z_]+)?\]", "", raw or "").replace("[REOPEN]", "").strip()

    prose, proposed, book_iso, cancelled, callback_requested, schedule_call_iso, withdrawn = _extract_markers(raw or "")
    if not cand:
        return prose, proposed
    if not flag_decision.should_reply:
        await _pause_auto_replies(db, cand, flag_decision, inbound_body)
        return prose, proposed
    if withdrawn and not cand.get("archived_at"):
        # Stage-independent on purpose: someone can leave while booked for an
        # interview or three days into training, and the outcome is the same —
        # off the board, reminders off, roster shrinks.
        await _apply_withdrawn(db, cand["id"], cand.get("user_id", ""), inbound_body)
        return prose, proposed
    if stage == "APPOINTMENT":
        if book_iso:
            booked = await _apply_sms_reschedule(db, cand, pipeline or {}, book_iso)
            if not booked:
                # The model confirmed a time we refused to write. Sending its reply
                # would promise an interview that does not exist, so replace it.
                prose = (
                    "Sorry — that time isn't available any more. "
                    "Pick any slot that suits you here: " + (_reschedule_link(cand) or "reply and we'll sort it out.")
                )
        elif cancelled:
            await _apply_appointment_cancellation(db, cand, inbound_body)
    elif stage in ("APPLICANT", "SCREENING") or (
        (cand or {}).get("screening_material") == "none" and _gate_checks_on()
    ):
        # Same gate as the questionnaire in _screening_block: text screening is
        # only conducted where chat is the screening channel. (Gate-check
        # candidates — booked with zero screening — pass through here too, so
        # their answers conclude into a verdict like any other screening.)
        # Without this, a
        # voice_first office's concierge could conclude, reject or book a
        # candidate over text — the one thing a chat-first rollout in one office
        # is meant to keep off another office's voice-first candidates. Under
        # voice_first the phone runs the screening; the self-serve callback and
        # slot-picker markers below still work as a concierge alongside it.
        from deps import resolve_settings
        from models import resolve_screening_mode
        _s = await resolve_settings((cand or {}).get("user_id") or "", (cand or {}).get("pipeline_id"))
        _screening_by_text = resolve_screening_mode(_s) in ("chat_first", "chat_only")
        if _screening_by_text and reopened and cand.get("disqualification_reason"):
            # They corrected a hard-gate answer and confirmed it. Undo the
            # rejection so the rest of the screening can proceed; [REOPEN] and
            # [BOOK:] can legitimately arrive together if the correction was the
            # last thing standing between them and a slot.
            from screening_outcome import reopen_screening
            await reopen_screening(db, cand, corrected=inbound_body)
            cand = await db.candidates.find_one({"id": cand["id"]}, {"_id": 0}) or cand
        if book_iso and _screening_by_text:
            # The screening now ends in a booking over SMS, so [BOOK:] has to
            # mean something at this stage. Previously it was honoured only at
            # APPOINTMENT, which meant the model could say "you're booked for
            # Tuesday" and nothing whatsoever would happen.
            from screening_outcome import book_screening_slot
            res = await book_screening_slot(db, cand, pipeline or {}, book_iso)
            if res.get("ok"):
                await _conclude_sms_screening(db, cand, booked=True)
            else:
                # Never send a confirmation for a booking we refused to write.
                prose = (
                    "Ah — that time just went. Reply with another from the ones I sent, "
                    "or pick any that suits you here: "
                    + (_reschedule_link(cand) or "reply and we'll sort it out.")
                )
        elif ended and _screening_by_text:
            # Screening finished without a slot — usually a failed hard gate.
            await _conclude_sms_screening(db, cand, booked=False, disq_hint=end_reason)
        elif callback_requested:
            ok = await _apply_sms_callback_request(db, cand)
            if not ok:
                prose = "Sorry — I couldn't get a call going right now. Reply here and we'll sort it out."
        elif schedule_call_iso:
            ok = await _apply_sms_schedule_call(db, cand, schedule_call_iso)
            if not ok:
                prose = "Sorry — that time didn't go through. Reply with another time and I'll get it booked."
    return prose, proposed


def _reschedule_link(candidate: dict) -> str:
    base_url = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
    token = candidate.get("public_token")
    return f"{base_url}/reschedule/{token}" if (base_url and token) else ""


def _e164(phone: str) -> Optional[str]:
    """E.164 or None. Local numbers are read per PHONE_DEFAULT_COUNTRY (phone_util)."""
    if not phone or not re.sub(r"\D", "", phone):
        return None
    from phone_util import normalize_phone_e164
    out = normalize_phone_e164(phone)
    return out if out.startswith("+") else None


def _parse_training_dt(iso: str) -> Optional[datetime]:
    """Parse training_start_at (naive local ISO) into an ET-aware datetime."""
    if not iso:
        return None
    try:
        s = iso.replace("Z", "").split("+")[0].split(".")[0]
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=ET)
    except Exception:
        return None


def _now_iso() -> str:
    return datetime.now(ET).isoformat()


def _within_start_watch_window(training_start_at: str, now: Optional[datetime] = None) -> bool:
    """True when `now` (ET) falls inside the start-morning watch window: from
    max(LEAD_HOURS, 3) hours before the training start (when the confirmation
    SMS invites replies) to 3 hours after it. Inside this window ANY inbound
    text from a TRAINING candidate is a page-a-human event — see the
    start-morning watch in handle_inbound."""
    start = _parse_training_dt(training_start_at or "")
    if start is None:
        return False
    now = now or datetime.now(ET)
    lead = max(LEAD_HOURS, 3.0)
    return (start - timedelta(hours=lead)) < now < (start + timedelta(hours=3))


def _confirm_process_open(candidate: Optional[dict], now: Optional[datetime] = None) -> bool:
    """True once the day-of confirmation process has begun for this starter:
    the pre-start confirmation SMS has gone out (scheduler_tick fires it
    lead-hours before the start — 10:00 AM for a 1 PM session) or we're
    already inside the start-morning watch window it opens. Before this
    point a YES is conversation, not confirmation: starters once asked
    questions days ahead, the assistant chased a YES on the spot, and the
    roster showed them confirmed before the morning-of process had begun.
    Attendance intent is only assessed on the day itself (owner's rule,
    2026-09-12) — an early yes is warmth to acknowledge, and the 10 AM
    chaser's yes is the one that counts. A reschedule unsets
    sms_confirmation_sent_at, so the window correctly re-closes until the
    chaser re-fires for the new date."""
    cand = candidate or {}
    if cand.get("sms_confirmation_sent_at") or cand.get("sms_confirmation_status") == "sent":
        return True
    return _within_start_watch_window(cand.get("training_start_at") or "", now)


def _start_is_stale(candidate: Optional[dict], now: Optional[datetime] = None) -> bool:
    """True when this candidate's training start passed more than 3 hours ago —
    the end of the start-morning watch window, i.e. the point where "confirm
    you're coming" stops being a sensible thing to say. A lapsed no-show who
    texts back weeks later re-enters the thread through unarchive_on_contact
    (see dialer/training_archive_sweep.py) still carrying the old date; every
    consumer of that record has to check this before chasing a YES for it —
    one candidate got "are you still good to come in today at 1:00 PM?" two
    months after their start date because nothing did."""
    dt = _parse_training_dt((candidate or {}).get("training_start_at") or "")
    if dt is None:
        return False
    return (now or datetime.now(ET)) > dt + timedelta(hours=3)


def _gate_checks_on() -> bool:
    """One source of truth for whether booked candidates may be asked
    screening questions by text. Killed 2026-07-30 — see screening_outcome."""
    from screening_outcome import GATE_CHECKS_BY_TEXT
    return GATE_CHECKS_BY_TEXT


# Invisible padding newer tapbacks carry — hair spaces and zero-width spaces
# wrapped around the emoji and the quoted text. Removed before matching, or the
# shape is invisible to the regex below.
_INVISIBLE_RE = re.compile(r"[ ​‌‍⁠﻿]")


def _strip_invisible(s: str) -> str:
    return _INVISIBLE_RE.sub("", s or "").strip()


# Phone reactions rendered as SMS text, in both shapes carriers deliver:
#   older iOS  — the verb, then the quoted message:  Liked “Take care!”
#   newer/RCS  — the emoji itself:                   👍 to “Take care!”
# Only the first was matched, so every thumbs-up on a newer handset arrived as an
# ordinary message. One starter's goodbye thread is what that costs: each
# reaction drew another farewell, six in ninety seconds, and the last one — the
# model having nothing left to say to a thumbs-up — was an invented reminder that
# their training started the following Monday, sent minutes after they had
# resigned.
_TAPBACK_RE = re.compile(
    r'^(?:'
    r'(?:Loved|Liked|Disliked|Laughed at|Emphasized|Questioned)\s+[“"].*[”"]'
    r'|Removed a[n]? (?:like|heart|exclamation|question mark|laugh|dislike) from\s+[“"].*[”"]'
    r'|[^\w\s]{1,8}\s*to\s+[“"].*[”"]'
    r'|Removed\s+[^\w\s]{1,8}\s+from\s+[“"].*[”"]'
    r')$',
    re.DOTALL,
)

# Words that flip the meaning of a following keyword: "I didn't say unsubscribe"
# must NOT classify as stop. Checked in a 3-token window before the keyword.
_NEGATORS = {"didnt", "dont", "never", "not", "havent", "wasnt", "isnt", "no"}


def _negated_before(tokens: list, idx: int, window: int = 3) -> bool:
    for w in tokens[max(0, idx - window):idx]:
        if w.replace("’", "").replace("'", "") in _NEGATORS:
            return True
    return False


def _classify(body: str) -> str:
    raw = (body or "").strip().lower()
    # Normalise curly apostrophes so phrase lists with straight quotes match.
    b = raw.replace("’", "'").replace(".", "").replace("!", "").replace("?", "").replace(",", "")
    if not b:
        return "unknown"
    tokens = b.split()
    # Opting out is decided on the RAW body, not this lower-cased, punctuation-
    # stripped version: the rule is a message that is exactly "STOP" in capitals,
    # and normalising the text away would make that impossible to tell.
    #
    # The previous rule fired on any message merely containing "stop",
    # "unsubscribe", "end" or "quit" as a word. "I quit my last job in June" and
    # "I'll be free at the end of the month" both unsubscribed a live candidate
    # and archived them — invisibly, because the symptom is someone who stops
    # replying, which looks exactly like an ordinary drop-off.
    if is_hard_stop(body or ""):
        return "stop"
    if looks_like_opt_out(body or ""):
        return "maybe_stop"
    for phrase in NO_PHRASES:
        if phrase in b:
            return "no"
    # Positive phrases outrank a leading "no"-token: "No worries, see you then!"
    # and "Nope, no questions - see you at 2" are confirmations that used to be
    # filed as declines off their first word. Real declines are caught above —
    # NO_PHRASES always wins first.
    for phrase in YES_PHRASES:
        if phrase in b:
            return "yes"
    first = tokens[0] if tokens else ""
    if first in NO_TOKENS or b in NO_TOKENS:
        return "no"
    if first in YES_TOKENS or b in YES_TOKENS:
        return "yes"
    return "unknown"


def _matches_no_phrase(body: str) -> bool:
    """True when the message contains an explicit decline PHRASE ("can't make
    it", "have to cancel", ...) rather than merely starting with a no-token.
    Used to decide whether a "no" classification is allowed to touch booking
    state while the assistant is mid-conversation."""
    b = (body or "").strip().lower().replace("’", "'").replace(".", "").replace("!", "").replace("?", "").replace(",", "")
    return any(phrase in b for phrase in NO_PHRASES)


# Mondays with no training cohort at any office. The reschedule offer skips
# them to the following Monday; a YES to one already offered books the next
# real cohort instead. Set NO_COHORT_MONDAYS (comma-separated YYYY-MM-DD) when
# a week is cancelled; empty by default.
NO_COHORT_MONDAYS = frozenset(
    d.strip() for d in (os.getenv("NO_COHORT_MONDAYS") or "").split(",") if d.strip()
)


def _skip_no_cohort(monday_iso: str) -> str:
    d = datetime.strptime(monday_iso, "%Y-%m-%d")
    while d.strftime("%Y-%m-%d") in NO_COHORT_MONDAYS:
        d += timedelta(days=7)
    return d.strftime("%Y-%m-%d")


def _next_monday_iso(training_iso: Optional[str]) -> Optional[str]:
    base: Optional[datetime] = None
    if training_iso:
        try:
            s = training_iso.replace("Z", "").split("+")[0].split(".")[0]
            base = datetime.fromisoformat(s)
        except Exception:
            pass
    if base is None:
        base = datetime.now(ET).replace(tzinfo=None)
    days_ahead = 7 - base.weekday() if base.weekday() != 0 else 7
    nxt = base + timedelta(days=days_ahead)
    # A stale training date computes a stale Monday (June 29 → July 6, months
    # gone by the time a lapsed no-show texts back). "Next Monday" is an offer
    # of a fresh start — it must never be a date that can't be attended.
    if nxt.date() <= datetime.now(ET).date():
        return _next_monday_iso(None)
    return _skip_no_cohort(nxt.strftime("%Y-%m-%d"))


def _extract_and_strip_marker(text: str) -> Tuple[str, Optional[str]]:
    prose, proposed, _book, _cancel, _callback, _sched, _withdrawn = _extract_markers(text)
    return (prose, proposed)


def _extract_markers(text: str) -> Tuple[str, Optional[str], Optional[str], bool, bool, Optional[str], bool]:
    """Split an LLM reply into (prose, proposed_date, book_iso, cancelled,
    callback_requested, schedule_call_iso, withdrawn).

    Markers are control channel, never shown to the candidate. `book_iso` and
    `schedule_call_iso` are whatever the model claimed — the caller must still
    check them against the live slot list / callback options before acting."""
    if not text:
        return ("", None, None, False, False, None, False)
    proposed = None
    book_iso = None
    m = PROPOSED_DATE_RE.search(text)
    if m:
        proposed = m.group(1)
    b = BOOK_RE.search(text)
    if b:
        book_iso = b.group(1).strip()
    cancelled = bool(CANCEL_RE.search(text))
    withdrawn = bool(WITHDRAW_RE.search(text))
    callback_requested = bool(REQUEST_CALLBACK_RE.search(text))
    schedule_call_iso = None
    sc = SCHEDULE_CALL_RE.search(text)
    if sc:
        schedule_call_iso = sc.group(1).strip()
    cleaned = CANCEL_RE.sub("", BOOK_RE.sub("", PROPOSED_DATE_RE.sub("", text)))
    cleaned = REQUEST_CALLBACK_RE.sub("", SCHEDULE_CALL_RE.sub("", cleaned)).strip()
    cleaned = WITHDRAW_RE.sub("", cleaned).strip()
    cleaned = re.sub(r"\n\s*\n+", "\n", cleaned).strip()
    return (cleaned, proposed, book_iso, cancelled, callback_requested, schedule_call_iso, withdrawn)


def _build_confirmation_message(candidate: dict, pipeline: dict, starter_tpl: dict = None) -> str:
    tpl = starter_tpl or {}
    custom_body = (tpl.get("confirmation_sms_body") or "").strip()
    if custom_body:
        # Substitute template variables
        name = (candidate.get("first_name") or "there").strip()
        company = (pipeline.get("name") or company_profile.company_name()).strip()
        dt = _parse_training_dt(candidate.get("training_start_at") or "")
        start_date = dt.strftime("%A %B %-d") if dt else ""
        start_time = dt.strftime("%-I:%M %p") if dt else ""
        office_address = (tpl.get("office_address") or pipeline.get("office_address") or "").strip()
        body = custom_body
        body = body.replace("[First Name]", name).replace("[first_name]", name)
        body = body.replace("[Company]", company).replace("[company]", company)
        body = body.replace("[Start Date]", start_date).replace("[start_date]", start_date)
        body = body.replace("[Start Time]", start_time).replace("[start_time]", start_time)
        body = body.replace("[Address]", office_address).replace("[address]", office_address)
        return body

    # Default message
    name = (candidate.get("first_name") or "there").strip()
    company = (pipeline.get("name") or company_profile.company_name()).strip()
    office_address = (tpl.get("office_address") or pipeline.get("office_address") or "").strip()
    dt = _parse_training_dt(candidate.get("training_start_at") or "")
    when = dt.strftime("%-I:%M %p") if dt else ""
    parts = [f"Hi {name}! 👋 Looking forward to seeing you at the {company} office today"]
    if when:
        parts[0] += f" at {when}"
    parts[0] += " for your first day orientation."
    if office_address:
        parts.append(f"Address: {office_address}.")
    parts.append("Reply YES to confirm. Reply STOP to opt out.")
    return " ".join(parts)


# ─── LLM reply ──────────────────────────────────────────────────────────────

async def _fetch_llm_context(db, candidate: dict, pipeline: dict) -> dict:
    """Fetch all enrichment data needed for a rich LLM context block."""
    ctx: dict = {}

    # 1. The FULL resolved settings doc, always. This used to be a raw
    #    find_one with a four-field projection that did not include
    #    screen_call_agent — so _screening_block's mode gate saw no
    #    screening_mode, fell back to voice_first, and the SMS screener
    #    never ran: candidate #1 on go-live night answered "Yes" and was
    #    promised a phone call, twice. resolve_settings also handles the
    #    override-or-global fallback the raw query didn't.
    try:
        from deps import resolve_settings
        user_id = candidate.get("user_id") or pipeline.get("user_id") or ""
        pipeline_id = candidate.get("pipeline_id") or pipeline.get("id") or ""
        ctx["settings"] = await resolve_settings(user_id, pipeline_id) or {}
    except Exception:
        ctx["settings"] = {}
    # Prefer the snapshot of the exact template sent to this candidate (stored
    # on the candidate doc when the email was sent); fall back to settings.
    ctx["starter_tpl"] = candidate.get("starter_email_snapshot") \
        or (ctx["settings"].get("starter_template") or {})

    # 2. Most recent screening call summary + top transcript excerpt
    try:
        convs = await db.conversations.find(
            {"candidate_id": candidate["id"]},
            {"_id": 0, "summary": 1, "transcript": 1, "created_at": 1},
        ).sort("created_at", -1).limit(1).to_list(1)
        if convs:
            ctx["call_summary"] = convs[0].get("summary") or ""
            transcript = convs[0].get("transcript") or []
            # Take last 6 turns (3 exchanges) so LLM has the closing sentiment
            ctx["transcript_tail"] = transcript[-6:] if len(transcript) > 6 else transcript
        else:
            ctx["call_summary"] = ""
            ctx["transcript_tail"] = []
    except Exception:
        ctx["call_summary"] = ""
        ctx["transcript_tail"] = []

    # 3. Job title + description from the pipeline
    try:
        job = await db.jobs.find_one(
            {"pipeline_id": candidate.get("pipeline_id")},
            {"_id": 0, "title": 1, "description": 1},
        )
        ctx["job_title"] = (job or {}).get("title") or ""
        ctx["job_description"] = (job or {}).get("description") or ""
    except Exception:
        ctx["job_title"] = ""
        ctx["job_description"] = ""

    # 4. Form responses (already on candidate)
    ctx["form_responses"] = candidate.get("form_responses") or {}

    # 5. Call summary already on candidate doc (AI-generated field)
    if not ctx["call_summary"]:
        ctx["call_summary"] = candidate.get("call_summary") or ""

    # 6. Real interview availability. The assistant moves appointments itself
    #    now, so it has to be looking at the actual schedule — told only "handle
    #    reschedules professionally" it agreed to a Monday this pipeline has
    #    never offered.
    ctx["slots"] = []
    ctx["slot_tz"] = default_tz_name()
    # SCREENING and APPLICANT need slots too now that the screening itself runs
    # over SMS: the conversation ends by offering interview times, so the model
    # has to be looking at the real schedule rather than improvising one.
    if (candidate.get("stage") or "").upper() in ("APPOINTMENT", "SCREENING", "APPLICANT"):
        try:
            from deps import resolve_settings
            from availability_service import compute_available_slots, candidate_window_days
            user_id = candidate.get("user_id") or pipeline.get("user_id") or ""
            pipeline_id = candidate.get("pipeline_id") or pipeline.get("id") or ""
            s = await resolve_settings(user_id, pipeline_id) or {}
            tz_name = (s.get("region_language") or {}).get("timezone") or default_tz_name()
            _appt_settings = s.get("appointments") or {}
            booked = await db.candidates.find(
                {"pipeline_id": pipeline_id, "appointment_at": {"$ne": None},
                 "id": {"$ne": candidate.get("id")}},
                {"_id": 0, "appointment_at": 1},
            ).to_list(2000)
            ctx["slots"] = compute_available_slots(
                pipeline,
                [b.get("appointment_at") for b in booked if b.get("appointment_at")],
                # Same booking window as every other candidate door — this one
                # hardcoded 14 days and bypassed booking_days_offered entirely.
                days_ahead=candidate_window_days(pipeline, _appt_settings, tz_name),
                tz_name=tz_name,
                default_capacity=int(_appt_settings.get("applicant_limit") or 50),
            )
            ctx["slot_tz"] = tz_name
            # What the interview actually IS. Without these the model could only ask someone to
            # pick a slot for an unnamed event — which is what it did: six questions answered in
            # two minutes, then "Ready to book your interview! 1) ... 2) ..." and no reply. The
            # facts all exist on the pipeline already; they were simply never handed over.
            # Deliberately no interviewer name. appointment_recruiter is a pipeline default, not a
            # property of the slot the candidate is picking — the slots carry no interviewer at
            # all — so putting a person's name in the offer states as fact something nobody has
            # committed to, and it is wrong the moment somebody else takes the session. "The
            # hiring manager" is both true and what the knowledge bank already says.
            ctx["interview"] = {
                "minutes": int(pipeline.get("appointment_duration_minutes") or 30),
                "virtual": ((s.get("appointments") or {}).get("appointment_type") or "virtual") == "virtual",
                # The link itself is deliberately NOT passed: it belongs to the confirmation, which
                # goes out with the calendar invite once a slot exists. Naming the platform is the
                # part that makes the ask concrete; sending a joinable link before there is
                # anything to join is how people turn up at the wrong time.
                "platform": "Zoom" if "zoom" in (pipeline.get("appointment_link") or "").lower() else "",
            }
        except Exception as e:
            logger.warning("training_sms: availability lookup failed for %s: %s", candidate.get("id"), e)

    return ctx


def _llm_ctx_block(candidate: dict, pipeline: dict, enrichment: dict = None) -> str:
    e = enrichment or {}
    tpl = e.get("starter_tpl") or {}

    name = f"{candidate.get('first_name','')} {candidate.get('last_name','')}".strip() or "the starter"
    company = (pipeline.get("name") or company_profile.company_name()).strip()
    office_address = (tpl.get("office_address") or pipeline.get("office_address") or "address not on file").strip()
    contact_phone = (tpl.get("contact_phone") or "").strip()
    dt = _parse_training_dt(candidate.get("training_start_at") or "")
    start_date = dt.strftime("%A, %B %-d %Y") if dt else "TBC"
    start_time = dt.strftime("%-I:%M %p") if dt else "TBC"
    # A lapsed no-show comes back through unarchive_on_contact with the old
    # date still on the record. Say so in the context itself — an unmarked
    # past date next to "confirm attendance" instructions reads as today's.
    if dt and _start_is_stale(candidate):
        start_date += " — THIS DATE HAS ALREADY PASSED, do not present it as upcoming"
    next_mon = _next_monday_iso(candidate.get("training_start_at"))

    parts = [
        f"STARTER NAME: {name}",
        f"COMPANY: {company}",
        f"ROLE: {e.get('job_title') or 'Sales Representative'}",
        f"OFFICE ADDRESS: {office_address}",
        f"START DATE: {start_date}",
        f"START TIME: {start_time}",
        "ORIENTATION DURATION: 2 hours",
    ]
    if contact_phone:
        parts.append(f"OFFICE CONTACT PHONE: {contact_phone}")
    if next_mon:
        parts.append(f"NEXT MONDAY (use if offering reschedule in Step 2): {next_mon}")

    # The APPOINTMENT stage goal tells the assistant to send the reschedule
    # link — so it has to be in the context, or it waves at "the link in your
    # email". Interview reschedules only: TRAINING start dates move through the
    # next-Monday flow above, not the slot picker.
    base_url = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
    if (candidate.get("stage") or "").upper() == "APPOINTMENT" and base_url and candidate.get("public_token"):
        parts.append(
            f"RESCHEDULE LINK (send verbatim if they ask to move their interview): "
            f"{base_url}/reschedule/{candidate['public_token']}"
        )
    # Pre-screening self-serve options — APPLICANT/SCREENING candidates are
    # exactly the ones in the warmup-to-call window (see routes/retry.py).
    if (candidate.get("stage") or "").upper() in ("APPLICANT", "SCREENING") and base_url and candidate.get("public_token"):
        token = candidate["public_token"]
        parts.append(f"CHAT LINK (send verbatim if they'd rather finish screening by web chat): {base_url}/retry/{token}")
        parts.append(f"PICK-A-TIME LINK (send verbatim if they want to browse every callback option themselves): {base_url}/retry/{token}?tab=schedule")
        try:
            from routes.retry import _compute_call_time_options
            options = _compute_call_time_options(e.get("settings") or {})
            if options:
                parts.append("AVAILABLE CALLBACK TIMES (the ONLY times you can offer for [SCHEDULE_CALL:] — copy the bracketed datetime EXACTLY):")
                for o in options:
                    parts.append(f"  - {o['label']} [{o['iso']}]")
        except Exception:
            pass
    if candidate.get("appointment_at"):
        try:
            _appt = datetime.fromisoformat(candidate["appointment_at"].replace("Z", "+00:00")).astimezone(ET)
            parts.append(f"INTERVIEW BOOKED: {_appt.strftime('%A, %b %-d at %-I:%M %p %Z')}")
        except Exception:
            parts.append(f"INTERVIEW BOOKED: {candidate['appointment_at']}")

    # The complete set of times this candidate can be offered or moved to.
    # Anything not on this list does not exist — there is no Monday unless a
    # Monday is printed here. This MUST render for SCREENING too: the booking
    # instructions say "offer the first two from AVAILABLE SLOTS", and when
    # this list only rendered at APPOINTMENT stage the screening model was
    # reading instructions about a section that wasn't there — it improvised
    # two times, leaked its own confusion mid-reply ("I don't see AVAILABLE
    # SLOTS listed..."), and the invented time then failed booking validation
    # as "that time just went". First pure-SMS booking attempt found it.
    slots = e.get("slots") or []
    if (candidate.get("stage") or "").upper() in ("APPOINTMENT", "SCREENING", "APPLICANT"):
        if slots:
            parts.append(
                f"AVAILABLE SLOTS — also called AVAILABLE INTERVIEW SLOTS (all times "
                f"{e.get('slot_tz') or default_tz_name()}). These are the ONLY times that exist:"
            )
            for s in slots[:12]:
                parts.append(f"  - {s.get('label')} [{s.get('datetime')}]")
        else:
            parts.append("AVAILABLE SLOTS: none open right now — send the reschedule link instead.")

    # Schedule
    if tpl.get("monday_start"):
        parts.append(f"DAY 1 HOURS: {tpl['monday_start']} – {tpl.get('monday_end', '')}")
    if tpl.get("tuesday_start"):
        parts.append(f"DAY 2 HOURS: {tpl['tuesday_start']} – {tpl.get('tuesday_end', '')}")
    if tpl.get("regular_schedule"):
        parts.append(f"ONGOING SCHEDULE: {tpl['regular_schedule']}")
    if tpl.get("dress_code"):
        parts.append(f"DRESS CODE: {tpl['dress_code']}")
    if tpl.get("custom_notes"):
        parts.append(f"SPECIAL NOTES: {tpl['custom_notes']}")

    # Role & office facts — the screen-call agent's additional context. The web
    # chat has always had this; SMS never did, so a candidate asking "Pay rate?"
    # by text was told to call the office while the scripted answer sat unused
    # in settings.
    _sca_ctx = ((e.get("settings") or {}).get("screen_call_agent") or {})
    _extra = (_sca_ctx.get("additional_context") or "").strip()
    if _extra:
        parts.append(
            "ROLE & OFFICE FACTS (answer candidate questions from this — pay, "
            "schedule, location, process):\n" + _extra[:4000]
        )
    _pipe_extra = (pipeline.get("additional_context_override") or "").strip()
    if _pipe_extra:
        parts.append("OFFICE-SPECIFIC CONTEXT:\n" + _pipe_extra[:2000])

    # Screening call summary — gives the LLM insight into the candidate's mindset/concerns
    if e.get("call_summary"):
        summary = e["call_summary"].strip()[:600]
        parts.append(f"\nSCREENING CALL SUMMARY:\n{summary}")

    # Transcript tail — last few exchanges for tone/concern context
    tail = e.get("transcript_tail") or []
    if tail:
        lines = [f"  {t.get('role','?').upper()}: {(t.get('text') or t.get('content') or '')[:200]}" for t in tail]
        parts.append("SCREENING CALL TAIL (last exchanges):\n" + "\n".join(lines))

    # Form responses — candidate's own words about motivations/situation
    form = e.get("form_responses") or {}
    if form:
        form_lines = []
        for qid, val in list(form.items())[:5]:
            q = val.get("question", qid) if isinstance(val, dict) else qid
            a = val.get("answer", str(val)) if isinstance(val, dict) else str(val)
            if a and str(a).strip():
                form_lines.append(f"  Q: {q}\n  A: {str(a)[:200]}")
        if form_lines:
            parts.append("CANDIDATE FORM RESPONSES:\n" + "\n".join(form_lines))

    # Job description snippet (first 300 chars) — helps with role-specific questions
    if e.get("job_description"):
        parts.append(f"JOB DESCRIPTION (excerpt): {e['job_description'][:300]}")

    return "\n".join(parts)


# The concierge goal for a voice_first office: the phone is running the
# screening, so the text agent answers questions and helps, and does NOT try to
# run or conclude a screening itself.
STAGE_GOALS_VOICE_FIRST_SCREENING = (
    "This candidate is being screened by phone. Your job over text is to answer "
    "questions about the role or the process, reassure any concerns, and keep them "
    "warm for the call. If they would rather finish online, get a callback, or pick "
    "a call time, you can make that happen yourself — see SCREENING SELF-SERVE "
    "OPTIONS below. Do NOT run through screening questions or try to book an "
    "interview yourself; the call handles that."
)

STAGE_GOALS = {
    "APPLICANT": (
        "The candidate has just applied. Your goal is to answer any questions about the process, "
        "set expectations (they'll receive a screening call shortly), and keep them warm and engaged. "
        "If they'd rather chat online, get an instant callback, or pick a different call time, you can "
        "make that happen yourself — see SCREENING SELF-SERVE OPTIONS below."
    ),
    "SCREENING": (
        "You are RUNNING the screening, here over text — not waiting for a call to do it. "
        "Work through the screening questions below in order, ONE per message, then book them "
        "an interview slot. Keep it moving: acknowledge the answer in a few words and ask the "
        "next question. Do not re-ask something they have already answered earlier in the "
        "thread. When every question is answered, offer interview times as described under "
        "BOOKING. If they would rather do this over a phone call — an instant callback or a "
        "scheduled time — you can make that happen yourself; see SCREENING SELF-SERVE OPTIONS "
        "below. The call is with you, the same assistant — never imply a separate person will "
        "contact them."
    ),
    "APPOINTMENT": (
        "The candidate has been booked for an interview/appointment. Your goal is to confirm they're "
        "still attending, answer logistics questions (location, what to bring, format), and handle "
        "reschedule requests professionally — send them the RESCHEDULE LINK from context verbatim so "
        "they can pick a new time themselves. Never propose specific dates yourself, and never tell "
        "them to look for a link elsewhere. If they reply positively to a confirmation message (Yes, sounds good, "
        "see you then, etc.) just warmly acknowledge it — don't treat it as a new action item."
    ),
    "FORM": (
        "The candidate has been sent a form to complete. Your goal is to encourage completion, answer "
        "any questions about the form, and let them know what happens after they submit."
    ),
    "CLOSE": (
        "The candidate is in the final assessment stage — their closing call is happening or imminent. "
        "They have NOT yet been booked to start. Answer questions about the role, process, or what "
        "happens next. Keep them engaged and positive about the opportunity."
    ),
    "TRAINING": (
        "The candidate has been successfully assessed, booked to start, and given a start date. "
        "Your PRIMARY goal is to get them to confirm attendance by replying YES. Handle concerns, "
        "answer logistics questions (location, dress code, what to bring, schedule). "
        "If they cannot attend, the ONLY alternative you can offer is the FOLLOWING Monday at the "
        "same start time — no other days or times. If they decline that too, be empathetic, "
        "ask for brief feedback, and wish them well."
    ),
}

# TRAINING goals for a start date that has already passed (see _start_is_stale).
# The chase-a-YES goal above is only true while the date is ahead; fed to the
# model after the date has gone it produces "are you still good to come in
# today?" months late. Which replacement applies depends on whether they ever
# confirmed: an unconfirmed date that passed is a no-show to re-engage, a
# confirmed one most likely attended and is asking about something else.
STAGE_GOAL_TRAINING_STALE_UNCONFIRMED = (
    "This candidate was booked to start, but their scheduled START DATE has ALREADY PASSED "
    "and they never confirmed it — they most likely did not attend. Do NOT ask them to "
    "confirm attendance for that old date, and NEVER say their start is 'today' or imply the "
    "session is still ahead. Your goal is re-engagement: respond naturally to what they said, "
    "find out whether they're still interested in the role, and if they are, offer the NEXT "
    "MONDAY date from context as a fresh start — the ONLY date you may offer. If they've "
    "moved on, thank them warmly and wish them well."
)
STAGE_GOAL_TRAINING_STALE_CONFIRMED = (
    "This candidate confirmed a start date that has now PASSED — they most likely attended "
    "and may already be working. Do NOT ask them to confirm attendance again, and NEVER say "
    "their start is 'today'. Answer their questions helpfully. Only if they say they actually "
    "missed their start day, offer the NEXT MONDAY date from context as the ONLY alternative."
)

# TRAINING goal for the days BEFORE the confirmation window opens (see
# _confirm_process_open). The default goal chases a YES the moment anyone
# speaks; fed to the model days ahead of the start it bolts "can you confirm
# with a YES so we have you locked in?" onto the answer to an innocent pay
# question — and that manufactured yes stamped the roster confirmed before
# the day-of process had begun.
STAGE_GOAL_TRAINING_PRE_WINDOW = (
    "The candidate is booked to start on the TRAINING START date in context, which is still "
    "ahead. The attendance-confirmation process has NOT started yet — we ask for the "
    "confirming YES on the morning of their start day, never before. Answer their questions "
    "warmly (location, pay, dress code, what to bring, schedule), keep them excited, and end "
    "your reply naturally. Do NOT ask them to confirm attendance, do NOT ask for a YES, and "
    "do NOT say things like 'can we count on you'. If they volunteer that they'll be there, "
    "acknowledge it warmly — the formal confirmation text reaches them on the morning itself. "
    "If they say they cannot attend, follow the reschedule policy."
)


def _screening_block(enrichment: dict, stage: str, candidate: dict = None) -> str:
    """The questions and the booking rules, for a screening being run over SMS.

    Sourced from the same `screen_call_agent.screening_questions` the phone agent
    and the web chat use, so the four hard gates are asked identically whichever
    channel a candidate lands in — and changing them in Settings changes all
    three at once rather than two of them.
    """
    # Gate-check mode: a candidate booked WITHOUT any screening (flagged
    # screening_material="none") gets the questions despite being at
    # APPOINTMENT stage — the whole point is asking the gates before the
    # interview, and their stage is exactly why nothing else would.
    _gate_check = bool((candidate or {}).get("screening_material") == "none") and _gate_checks_on()
    if stage not in ("SCREENING", "APPLICANT") and not _gate_check:
        return ""
    settings = (enrichment or {}).get("settings") or {}
    # Only where chat IS the screening channel. Under voice_first the phone runs
    # the screening and SMS is a concierge alongside it — handing this office's
    # assistant a questionnaire and a booking marker would quietly turn on
    # text-screening everywhere, including offices whose live candidates a
    # chat-first rollout is deliberately not touching.
    from models import resolve_screening_mode
    if resolve_screening_mode(settings) not in ("chat_first", "chat_only"):
        return ""
    sca = settings.get("screen_call_agent") or {}
    questions = sca.get("screening_questions") or []
    if not questions:
        return ""

    lines = [
        "SCREENING QUESTIONS — ask in this order, one per message:",
        "  (The arrival text has usually already asked question 1. Read the thread "
        "before you speak: if they have answered it, acknowledge and go to the next "
        "one. Never re-ask a question they have already answered.)",
    ]
    for i, q in enumerate(questions, 1):
        text = (q.get("question") or "").strip()
        if not text:
            continue
        gate = ""
        if q.get("auto_screen"):
            # A disqualifying answer has to end things loudly. Left to itself the
            # model tends to soften a "no" into "we'll be in touch", which is how
            # a 17-year-old ends up still in the pipeline.
            gate = (" [HARD GATE — if the answer is no or disqualifying, thank them warmly,"
                    " tell them we can't take it forward, and end your reply with"
                    " [END:reason], where reason is exactly one of: age,"
                    " work_authorization, schedule_availability, commute —"
                    " whichever gate they failed]")
        lines.append(f"  {i}. {text}{gate}")

    if _gate_check and stage not in ("SCREENING", "APPLICANT"):
        # Already booked — this conversation exists to ask the gates, never to
        # touch the booking. When done, thank them and confirm the interview
        # stands; a failed gate concludes with [END] like any other screening
        # (the conclude path records it and alerts the recruiter — the booking
        # itself is the recruiter's call, not the model's).
        lines += [
            "",
            "ALREADY BOOKED — they have an interview scheduled. Do NOT offer, change or cancel "
            "any times, and do not mention booking. Ask the screening questions above, one per "
            "message. When every question is answered, thank them warmly and confirm their "
            "interview stands as scheduled, then end with [END].",
        ]
        return "\n".join(lines)

    prefs = settings.get("booking_preferences") or {}
    primary = int(prefs.get("slots_primary") or 2)
    fallback = int(prefs.get("slots_fallback") or 1)
    slots = (enrichment or {}).get("slots") or []
    # What the interview IS comes from the knowledge bank the voice agent already uses (it lands
    # in this prompt as ROLE & OFFICE FACTS), never from pipeline fields. The pipeline knows
    # "30 minutes with <recruiter>"; the knowledge bank knows it is, say, a 30–45 minute GROUP Zoom
    # with hiring managers and other shortlisted candidates. Deriving the sentence here would have
    # had SMS quietly contradicting what the phone agent tells the same candidate. Only when an
    # office has no knowledge bank at all does the pipeline get to fill in.
    has_kb = bool(((enrichment or {}).get("settings") or {}).get("screen_call_agent", {}).get("additional_context", "").strip())
    if has_kb:
        iv_source = ("Take the format from the interview details in ROLE & OFFICE FACTS above — "
                     "length, whether it is a group or one-to-one, and the platform — and say it "
                     "in your own warm words. Do not contradict it and do not add details it does "
                     "not give you.")
    else:
        iv = (enrichment or {}).get("interview") or {}
        bits = []
        if iv.get("minutes"):
            bits.append(f"about {iv['minutes']} minutes")
        bits.append(("a video call" + (f" on {iv['platform']}" if iv.get("platform") else ""))
                    if iv.get("virtual") else "in person at the office")
        bits.append("with the hiring manager")
        iv_source = "Say that it is " + ", ".join(bits) + "."

    lines += [
        "",
        "BOOKING — once every question above is answered:",
        # The failure this exists to stop: a candidate answered all six questions in two minutes
        # and was told "Ready to book your interview! 1) Sat 1:00 PM 2) Tue 9:15 AM — just reply 1
        # or 2". They never replied. Nothing had told them what they were being asked to attend,
        # so the biggest commitment in the flow arrived with the least context behind it.
        "  - FIRST say what the interview actually is, in one or two warm sentences, BEFORE any "
        "time appears. " + iv_source + " Also say what it is for — learning more about the role "
        "and the company, and a chance to ask questions. Never jump from the last answer straight "
        "to a list of times.",
        # A name can reach the model from the knowledge bank, the office context or the call
        # transcript, so removing it from the interview facts is not enough on its own.
        "  - NEVER name the interviewer. Say \"the hiring manager\" or \"our hiring managers\", "
        "never a person's name, even if one appears elsewhere in this prompt. No individual is "
        "tied to a slot, so naming one promises something nobody has agreed to.",
        "  - Do NOT send a joining link. The confirmation carries it once a slot is actually booked. "
        "If they want more detail before choosing, say the full details come with the confirmation "
        "once they pick a time.",
        f"  - THEN offer the FIRST {primary} times from AVAILABLE SLOTS, exactly as written there, "
        "NUMBERED — e.g. \"1) Wednesday 2:00 PM or 2) Thursday 9:15 AM - just reply 1 or 2\". "
        "Keep the pick itself to one character: the explanation is what earns the reply, but a "
        "one-character answer is what makes it easy to give. Accept a number, a time, or a day.",
        "  - Both parts go in ONE message — explanation first, then the times. Not two texts.",
        f"  - If they can't do either, offer the next {fallback} — and stop there: never show "
        "more than four times in total. A wall of options reads like a rota, not an invitation.",
        "  - EXCEPTION — they name a specific day or date: offer up to two times from AVAILABLE "
        "SLOTS on that day. If that day has none, say so and offer the closest day that does.",
        "  - If none of that lands, send them the pick-a-time link instead and end with [END].",
        "  - NEVER invent, guess or round a time. Only offer times that appear in that list.",
        "  - When they choose one, append `[BOOK: <the exact ISO datetime>]` to your reply. "
        "The server strips it — they never see it. Only send it once they have actually agreed.",
        "  - Same-day times are fine to offer if the list contains them; sooner books better.",
    ]
    if not slots:
        lines.append("  - AVAILABLE SLOTS is currently empty: do NOT offer any time. Send the "
                     "pick-a-time link and end with [END].")

    # A failed gate ends the conversation, but it must not be a locked door.
    # People misread "18 or over" on a phone screen, hear the schedule question
    # as "every day", or say they can't commute and then realise the office is
    # two stops away. If they come back and say so, carrying on is the only
    # sensible response.
    ruled_out = (candidate or {}).get("disqualification_reason")
    if ruled_out:
        lines += [
            "",
            f"PREVIOUSLY RULED OUT — on: {ruled_out}.",
            "  - They were told we couldn't take it forward, and have messaged again.",
            "  - If they are simply asking why, explain kindly and leave it there.",
            "  - If they now give a DIFFERENT answer to that question — they are 18 after "
            "all, they can do the four days, they can get to the office — read it back to "
            "them once to be sure ('just so I've got this right, you can...?'), and once "
            "they confirm, append [REOPEN] to your reply and carry on from the next "
            "unanswered question.",
            "  - Do NOT reopen on a maybe, on 'what if I could', or on anything you had to "
            "infer. Only on a clear, corrected answer they have confirmed.",
        ]
    return "\n".join(lines) + "\n\n"


def _llm_system_prompt(candidate: dict, pipeline: dict, intent: str, enrichment: dict = None) -> str:
    e = enrichment or {}
    tpl = e.get("starter_tpl") or {}
    stage = (candidate.get("stage") or "TRAINING").upper()
    company = (pipeline.get("name") or company_profile.company_name()).strip()
    custom_instructions = (tpl.get("confirmation_sms_instructions") or "").strip()

    # Per-pipeline override from settings; fall back to hardcoded default if blank.
    # APPLICANT and SCREENING share the same prompt — candidates in APPLICANT are
    # about to be called so they get the same context as SCREENING.
    _ai_prompts = (enrichment.get("settings") or {}).get("ai_stage_prompts") or {}
    _prompt_stage = "SCREENING" if stage == "APPLICANT" else stage
    _stage_override = (_ai_prompts.get(_prompt_stage) or {}).get("sms") or ""
    stage_goal = _stage_override.strip() if _stage_override.strip() else STAGE_GOALS.get(stage, STAGE_GOALS["TRAINING"])
    # The default SCREENING goal says "you are RUNNING the screening, book them a
    # slot". That is only true where chat is the screening channel; for a
    # voice_first office the phone runs it and the SMS agent is a concierge, so
    # telling it to run and book a screening would contradict _screening_block
    # (which correctly stays silent) and the ungated goal would leak the intent.
    if stage in ("SCREENING", "APPLICANT") and not _stage_override.strip():
        from models import resolve_screening_mode
        if resolve_screening_mode(enrichment.get("settings")) not in ("chat_first", "chat_only"):
            stage_goal = STAGE_GOALS_VOICE_FIRST_SCREENING

    # A start date that has already passed invalidates the TRAINING goal
    # wholesale — including a recruiter-written override, which is authored
    # for the upcoming-start case and carries the same confirm-for-today
    # assumption. See _start_is_stale.
    _stale_start = stage == "TRAINING" and _start_is_stale(candidate)
    if _stale_start:
        stage_goal = (
            STAGE_GOAL_TRAINING_STALE_CONFIRMED
            if candidate.get("sms_confirmation_status") == "confirmed"
            else STAGE_GOAL_TRAINING_STALE_UNCONFIRMED
        )

    # Ahead of the confirmation window the same wholesale swap applies — the
    # default goal and any recruiter override both chase a YES, and chasing it
    # days early is exactly what the day-of process exists to prevent.
    _pre_window = (
        stage == "TRAINING" and not _stale_start and not _confirm_process_open(candidate)
    )
    if _pre_window:
        stage_goal = STAGE_GOAL_TRAINING_PRE_WINDOW

    # The model sees absolute appointment datetimes but, without an anchor,
    # cannot relate them to now — it told a candidate his 2 PM interview was
    # "tomorrow" at 11:23 the same morning, two hours before the session.
    _now_et = datetime.now(ET)
    base = (
        f"CURRENT DATE & TIME: {_now_et.strftime('%A, %B %-d, %Y, %-I:%M %p %Z')}. "
        "Relative words like 'today', 'tomorrow' or 'tonight' must be computed from THIS "
        "timestamp — when unsure, name the day and date instead ('Thursday, Jul 30').\n\n"
        f"You are {company}'s recruitment assistant, communicating with candidates via SMS. "
        "Keep every reply under 300 characters — one or two short sentences. "
        "Sound warm, human, and professional. Emoji: at most one every few messages, never two "
        "messages in a row, and never the same emoji twice in a thread — a smiley on every reply "
        "reads as a bot. Vary your acknowledgments too (great / perfect / got it / love that). "
        "Plain text only — no markdown, no **bold**, no bullets; asterisks arrive as literal symbols in an SMS. "
        "Answer questions using ONLY the context provided — never invent specifics. "
        "If you genuinely don't know something, say so briefly and note it's covered at the "
        "interview — NEVER tell them to call or contact the office: nobody staffs that phone, "
        "you ARE the contact, and pay/schedule/location are all in your context, so check there first.\n\n"
        f"CANDIDATE PIPELINE STAGE: {stage}\n"
        f"YOUR GOAL AT THIS STAGE: {stage_goal}\n\n"
        f"{_screening_block(e, stage, candidate)}"
        # Judging tone is the model's job — a word list cannot tell "fucking
        # great news, thanks" from abuse, and getting that wrong drops a real
        # candidate. Injection is handled before this prompt is ever built.
        f"{FLAG_INSTRUCTIONS}\n\n"
    )

    if stage == "TRAINING":
        base += (
            "TRAINING RESCHEDULE POLICY (STRICT):\n"
            "- The only reschedule option is the FOLLOWING Monday at the same start time.\n"
            "- NEVER suggest alternate days, times, or one-on-one sessions.\n"
            "- NEVER ask 'what day works for you' — forbidden.\n\n"
            "RESCHEDULE MARKER:\n"
            "When (and only when) you are explicitly offering the next Monday slot as a reschedule, "
            "append `[PROPOSED_DATE: YYYY-MM-DD]` on its own line at the very end of your reply "
            "using the NEXT MONDAY date from the context block. "
            "The server strips this before sending — the candidate never sees it. "
            "Do NOT include this marker in any other situation.\n\n"
            f"{WITHDRAW_MARKER_INSTRUCTIONS}\n"
        )
        if _stale_start:
            # The no/follow_up/unknown blocks below all steer toward a YES for
            # the scheduled date — every one of them is wrong once it's gone.
            base += (
                "CURRENT INTENT: Their scheduled start date is in the past — follow YOUR GOAL AT "
                "THIS STAGE above and respond to what they actually said. Do NOT push for a YES "
                "on the old date. If you offer the next-Monday fresh start, append the "
                "[PROPOSED_DATE] marker as described.\n\n"
            )
        elif intent == "no":
            base += (
                "CURRENT INTENT: The candidate just declined or said they can't make it.\n"
                "Follow this 3-step ladder — check the prior conversation to see which step you're on:\n"
                "  STEP 1 (first pushback): Empathetically address their concern, nudge to still attend today. "
                "Do NOT mention rescheduling yet. End with a gentle ask to still come.\n"
                "  STEP 2 (they insist they can't): NOW offer the following Monday at the same time as the ONLY "
                "alternative. Ask them to reply YES to lock it in. Append [PROPOSED_DATE] marker.\n"
                "  STEP 3 (declined next Monday too): Stop offering. Thank them, wish them well. "
                "Only ask what prevented them from attending IF they haven't already told you.\n"
                "REASON ALREADY GIVEN: If the candidate has stated why they can't come (new job, school, "
                "moved, illness, family, transport, etc.), NEVER ask for a reason again — acknowledge the "
                "specific reason they gave. Re-asking reads as if you didn't listen.\n"
                "DEFINITIVE WITHDRAWAL: If they've clearly made up their mind not to start (accepted another "
                "job or school, relocating, etc.), skip the ladder — do NOT push them to attend or offer the "
                "Monday slot. Warmly acknowledge their reason, say they're welcome to reapply, wish them well.\n\n"
            )
        elif intent == "follow_up":
            if _pre_window:
                base += (
                    "CURRENT INTENT: Ongoing conversation — be natural, build on context, answer what "
                    "they raised. Do NOT steer toward confirming attendance; that ask happens on the "
                    "morning of their start day.\n\n"
                )
            else:
                base += "CURRENT INTENT: Ongoing conversation — be natural, build on context, keep nudging toward YES.\n\n"
        else:
            if _pre_window:
                base += (
                    "CURRENT INTENT: Unclear reply — read context and address what they said. "
                    "Do NOT push them to confirm attendance.\n\n"
                )
            else:
                base += "CURRENT INTENT: Unclear reply — read context, address what they said, bring them back to confirming.\n\n"
    elif stage == "APPOINTMENT":
        base += (
            "INTERVIEW RESCHEDULING (you can do this yourself):\n"
            "- The AVAILABLE INTERVIEW SLOTS list in the context is the complete set of times that exist. "
            "A day that is not on that list is not available — say so plainly and offer the nearest two "
            "real alternatives instead. NEVER say 'sure' to a day you cannot see on the list, and never "
            "invent, guess, or imply a time.\n"
            "- When they ask to move and haven't named a time, offer the two soonest slots in plain words "
            "(e.g. 'I have Tuesday 9:00 AM or Wednesday 1:00 PM').\n"
            "- When they pick one, confirm it warmly in your reply AND append `[BOOK: <datetime>]` at the "
            "very end, copying the bracketed datetime for that slot EXACTLY as printed in the list. "
            "The server does the booking, cancels the old reminders, and sends a fresh confirmation.\n"
            "- If the slot list is empty, send them the RESCHEDULE LINK instead.\n\n"
            "CANNOT-ATTEND MARKER:\n"
            "If they say they can't attend — an emergency, illness, work, anything — and have NOT yet "
            "settled on a new time, append `[CANCEL]` at the very end of your reply. This is what stops "
            "the reminders for their old slot; without it we keep texting 'your interview is in 1 hour' at "
            "someone who already told us they can't come. Still be warm and still offer real slots. "
            "Do not append it once they have picked a new time — use [BOOK:] then.\n"
            f"{WITHDRAW_MARKER_INSTRUCTIONS}"
            "Use [CANCEL] instead when they are only moving one appointment and still want the job.\n"
            "Markers are stripped before sending; the candidate never sees them.\n\n"
        )
        base += f"CURRENT INTENT: {intent}\n\n"
    elif stage in ("APPLICANT", "SCREENING"):
        base += (
            "SCREENING SELF-SERVE OPTIONS (you can act on these yourself):\n"
            "- If they'd rather finish their screening online than by phone or text, send them the "
            "CHAT LINK from context verbatim.\n"
            "- If they ask to be called back right now / ASAP, say something like 'On it — expect a "
            "call in the next minute or so!' and append `[REQUEST_CALLBACK]` at the very end of your reply.\n"
            "- If they ask to be called at a specific time, and that request is a clear match for one of "
            "the AVAILABLE CALLBACK TIMES in context, confirm it warmly and append `[SCHEDULE_CALL: <datetime>]` "
            "at the very end, copying the bracketed datetime for that option EXACTLY as printed. NEVER invent "
            "or compute a datetime yourself.\n"
            "- If the time they want isn't a clear match for any AVAILABLE CALLBACK TIME, don't guess — offer "
            "the closest 1-2 listed options in plain words, or send the PICK-A-TIME LINK from context so they "
            "can browse every option themselves.\n"
            "Markers are stripped before sending; the candidate never sees them.\n\n"
        )
        base += f"CURRENT INTENT: {intent}\n\n"
    else:
        base += f"CURRENT INTENT: {intent}\n\n"

    if custom_instructions:
        base += f"RECRUITER INSTRUCTIONS FOR THIS PIPELINE:\n{custom_instructions}\n\n"

    base += "CONTEXT:\n" + _llm_ctx_block(candidate, pipeline, enrichment)
    return base


def _format_history(history: List[Dict]) -> str:
    if not history:
        return ""
    # Each line carries when it was sent. Undated, a two-month-old "see you
    # today at 1:00 PM" reads to the model as this morning's message — that's
    # how a lapsed no-show texting back got "are you still good to come in
    # today?" — and CURRENT DATE & TIME in the system prompt can't anchor
    # lines that carry no dates of their own.
    from models import parse_appointment_at
    _this_year = datetime.now(ET).year
    lines = []
    for m in history[-10:]:
        who = "STARTER" if m.get("direction") == "in" else "BOT"
        body = (m.get("body") or "").strip().replace("\n", " ")
        stamp = ""
        dt = parse_appointment_at(m.get("timestamp"))
        if dt:
            loc = dt.astimezone(ET)
            fmt = "%b %-d, %-I:%M %p" if loc.year == _this_year else "%b %-d %Y, %-I:%M %p"
            stamp = f"[{loc.strftime(fmt)}] "
        lines.append(f"{stamp}{who}: {body}")
    return "\n\nPRIOR CONVERSATION (oldest → newest, each line stamped with when it was sent):\n" + "\n".join(lines)


async def _generate_llm_reply(
    db,
    candidate: dict,
    pipeline: dict,
    history: List[Dict],
    inbound_body: str,
    intent: str,
) -> Optional[str]:
    api_key = __import__("llm_config").api_key()
    if not api_key:
        return None
    try:
        from anthropic import AsyncAnthropic
        enrichment = await _fetch_llm_context(db, candidate, pipeline)
        sys_msg = _llm_system_prompt(candidate, pipeline, intent, enrichment) + _format_history(history)
        # The old 9s ceiling here was built on a false constraint. The reply
        # does NOT go back through the webhook — LLM replies are sent via the
        # REST paced path, and the handler answers Twilio with empty TwiML.
        # If generation outlives Twilio's ~15s patience, Twilio retries the
        # webhook and the MessageSid dedupe swallows the retry while THIS
        # request keeps running and still delivers. So a slow model costs the
        # candidate a longer "typing" pause — which reads as thought — while
        # the 9s ceiling cost them "sorry, I lagged" on half their messages
        # the night the model's latency spiked (one candidate got it seven times
        # and asked "Is your system okay?"). Wait for the model.
        # max_retries=2: the "lag" apologies that survived the 30s budget were
        # failing in 2-3 SECONDS — instant API rejections (rate limits), not
        # slow generations, and both fallback attempts shared the same
        # congested Sonnet quota so both died instantly. The SDK retries
        # 429/529 with backoff; the second model is Haiku on a separate, far
        # larger quota, so a Sonnet rate-limit burst can't take out both.
        client = AsyncAnthropic(api_key=api_key, max_retries=2)
        resp = None
        # ANTHROPIC_MODEL first, ANTHROPIC_FAST_MODEL as the fallback
        # (llm_config.py). Each attempt carries its own time budget.
        import llm_config
        _models = llm_config.chat_models()
        for _i, _model in enumerate(_models):
            _timeout = 20.0 if _i == 0 else 12.0
            try:
                resp = await client.with_options(timeout=_timeout).messages.create(
                    model=_model,
                    # SMS replies are 1-2 sentences, but max_tokens also covers
                    # any thinking the model does, so leave headroom.
                    max_tokens=2048,
                    system=sys_msg,
                    messages=[{"role": "user", "content": inbound_body}],
                    **llm_config.low_latency_params(_model),
                )
                break
            except Exception as e:
                if _i == len(_models) - 1:
                    raise
                logger.warning("sms agent: %s failed (%s) — retrying on %s",
                               _model, str(e)[:140], _models[_i + 1])
        reply = llm_config.response_text(resp) or None
        if not reply:
            return None
        text = str(reply).strip()
        # Truncate the prose only. A [BOOK:...] marker lives at the end of the
        # reply, so a blind 300-char cut would silently drop the booking and the
        # candidate would get a "you're all set" with nothing behind it.
        prose, proposed, book_iso, cancelled, callback_requested, schedule_call_iso, withdrawn = _extract_markers(text)
        # Belt-and-braces on the prompt's no-markdown rule — **bold** arrives
        # as literal asterisks in an SMS.
        prose = prose.replace("**", "")
        if len(prose) > MAX_REPLY_LEN:
            prose = prose[:MAX_REPLY_LEN - 1].rstrip() + "…"
        markers = ""
        if proposed:
            markers += f" [PROPOSED_DATE: {proposed}]"
        if book_iso:
            markers += f" [BOOK: {book_iso}]"
        if cancelled:
            markers += " [CANCEL]"
        if withdrawn:
            markers += " [WITHDRAW]"
        if callback_requested:
            markers += " [REQUEST_CALLBACK]"
        if schedule_call_iso:
            markers += f" [SCHEDULE_CALL: {schedule_call_iso}]"
        return (prose + markers).strip()
    except Exception as e:
        logger.error("training_sms: LLM reply failed: %s", e)
        return None


# ─── message logging ─────────────────────────────────────────────────────────

async def _log_message(
    db,
    candidate_id: Optional[str],
    pipeline_id: Optional[str],
    direction: str,
    body: str,
    sender: Optional[str] = None,
    recipient: Optional[str] = None,
    classification: Optional[str] = None,
    message_sid: Optional[str] = None,
    was_llm_reply: bool = False,
):
    doc = {
        "candidate_id": candidate_id,
        "pipeline_id": pipeline_id,
        "direction": direction,
        "body": body or "",
        "sender": sender,
        "recipient": recipient,
        "classification": classification,
        "message_sid": message_sid,
        "was_llm_reply": bool(was_llm_reply),
        "timestamp": _now_iso(),
    }
    try:
        await db.training_sms_messages.insert_one(doc)
    except Exception as e:
        logger.warning("training_sms_messages insert failed: %s", e)


async def _get_thread(db, candidate_id: str, limit: int = 100, include_chat: bool = False) -> list:
    """Return the SMS thread for one candidate, merging training_sms_messages
    (the two-way SMS log) with the outbound comms log (appointment confirmations,
    reminders, etc.) so the AI has full context of every message sent.

    `include_chat=True` also merges the web-chat turns — the reply branches in
    handle_inbound need them, because a candidate who started on the portal and
    then texts is mid-conversation: without their chat answers the SMS screener
    re-asks what the portal already asked (the mirror of the web chat loading
    SMS history). The recruiter SMS inbox and the email agent keep the default:
    those views are channel-specific by design."""
    sms_msgs = await db.training_sms_messages.find(
        {"candidate_id": candidate_id},
        {"_id": 0, "direction": 1, "body": 1, "timestamp": 1, "was_llm_reply": 1, "classification": 1},
    ).sort("timestamp", 1).limit(limit).to_list(limit)

    # Pull outbound SMS sent by the platform (warmup, form, reminders, etc.)
    # Filter to type=sms only — emails are shown in the Email tab, not here.
    comms = await db.communications.find(
        {"candidate_id": candidate_id, "type": "sms", "status": "sent"},
        {"_id": 0, "template_key": 1, "body": 1, "created_at": 1},
    ).sort("created_at", 1).limit(50).to_list(50)

    comm_msgs = []
    for c in comms:
        body = (c.get("body") or "").strip()
        if not body:
            continue
        comm_msgs.append({
            "direction": "out",
            "body": body,
            "timestamp": c.get("created_at") or "",
            "was_llm_reply": False,
            "source": "comms",
        })

    chat_msgs = []
    if include_chat:
        cand_doc = await db.candidates.find_one({"id": candidate_id}, {"_id": 0, "chat_log": 1})
        for m in (cand_doc or {}).get("chat_log") or []:
            if (m.get("channel") or "") != "retry":
                continue
            body = (m.get("text") or "").strip()
            if not body:
                continue
            chat_msgs.append({
                "direction": "in" if (m.get("role") or "").lower() in ("applicant", "candidate", "user", "human") else "out",
                "body": body,
                "timestamp": m.get("at") or "",
                "was_llm_reply": True,
                "source": "chat",
            })

    def _utc_key(m):
        # SMS rows stamp Eastern, comms and chat stamp UTC — raw string order
        # scrambles a candidate who switched channels mid-screening.
        from models import parse_appointment_at
        dt = parse_appointment_at(m.get("timestamp"))
        return dt.astimezone(timezone.utc).isoformat() if dt else ""

    merged = sorted(comm_msgs + sms_msgs + chat_msgs, key=_utc_key)
    return merged[-limit:]


async def _inbound_count(db, candidate_id: str) -> int:
    return await db.training_sms_messages.count_documents(
        {"candidate_id": candidate_id, "direction": "in"}
    )


# ─── send (outbound) ─────────────────────────────────────────────────────────

async def _get_starter_tpl(db, candidate: dict) -> dict:
    """Fetch the pipeline's starter_template from settings."""
    try:
        user_id = candidate.get("user_id") or ""
        pipeline_id = candidate.get("pipeline_id") or ""
        doc = await db.settings.find_one(
            {"user_id": user_id, "pipeline_id": pipeline_id},
            {"_id": 0, "starter_template": 1},
        )
        return (doc or {}).get("starter_template") or {}
    except Exception:
        return {}


async def _send_confirmation_sms(db, candidate_id: str, force: bool = False) -> dict:
    """Send the 3-hour confirmation SMS for one TRAINING candidate. Idempotent unless force=True."""
    cand = await db.candidates.find_one({"id": candidate_id}, {"_id": 0})
    if not cand:
        return {"ok": False, "reason": "Candidate not found"}
    if cand.get("sms_opted_out"):
        return {"ok": False, "reason": "Phone opted out"}
    if not force and cand.get("sms_confirmation_sent_at"):
        return {"ok": False, "reason": "Already sent"}
    to = _e164(cand.get("phone") or "")
    if not to:
        return {"ok": False, "reason": "Missing/invalid phone"}

    pipeline = await db.pipelines.find_one({"id": cand.get("pipeline_id")}, {"_id": 0})
    if not pipeline:
        return {"ok": False, "reason": "Pipeline not found"}
    # Same sender as every other text to this candidate (the office's
    # sms_number, then TWILIO_PHONE_NUMBER), so it can be answered in the same
    # thread. The office's calling number is only a last resort: a UK
    # geographic calling number can't send texts.
    from_num = ""
    try:
        from deps import resolve_settings
        from sms_service import resolve_sms_sender
        _settings = await resolve_settings(cand.get("user_id") or "", cand.get("pipeline_id"))
        from_num = (resolve_sms_sender(_settings, pipeline) or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"confirmation SMS: sender lookup failed for {candidate_id}: {e}")
    if not from_num:
        from_num = (pipeline.get("twilio_phone_number") or "").strip()
    if not from_num:
        return {"ok": False, "reason": "No texting number for this office (sms_number or TWILIO_PHONE_NUMBER)"}

    sid = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
    token = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
    if not sid or not token:
        return {"ok": False, "reason": "Twilio creds not configured"}

    starter_tpl = await _get_starter_tpl(db, cand)
    body = _build_confirmation_message(cand, pipeline, starter_tpl)
    try:
        from twilio.rest import Client
        client = Client(sid, token)
        msg = client.messages.create(to=to, from_=from_num, body=body)
        now = _now_iso()
        await db.candidates.update_one(
            {"id": candidate_id},
            {"$set": {
                "sms_confirmation_sent_at": now,
                "sms_confirmation_status": "sent",
                "sms_confirmation_sid": msg.sid,
                "sms_confirmation_error": None,
            }},
        )
        await _log_message(
            db, candidate_id=candidate_id, pipeline_id=cand.get("pipeline_id"),
            direction="out", body=body, sender=from_num, recipient=to,
            message_sid=msg.sid,
        )
        logger.info("training_sms: sent confirmation to %s (%s)", to, candidate_id)
        return {"ok": True, "sid": msg.sid, "to": to}
    except Exception as e:
        err = str(e)[:300]
        await db.candidates.update_one(
            {"id": candidate_id},
            {"$set": {"sms_confirmation_error": err}},
        )
        logger.error("training_sms: send failed for %s: %s", candidate_id, err)
        return {"ok": False, "reason": err}


# ─── scheduler tick ──────────────────────────────────────────────────────────

async def scheduler_tick(db):
    """Find TRAINING candidates whose start time is within the configured lead window and send confirmation SMS."""
    now_et = datetime.now(ET)
    # Cache per-pipeline lead hours to avoid repeated DB hits in the same tick
    pipeline_lead_hours: dict = {}

    cur = db.candidates.find({
        "stage": "TRAINING",
        # Archived candidates aren't expected — confirming their day would
        # invite someone the board says isn't coming. (A rebooking no-show is
        # unarchived on their first inbound text, so they still get this.)
        "$or": [{"archived_at": None}, {"archived_at": {"$exists": False}}],
        "training_start_at": {"$exists": True, "$nin": [None, ""]},
        "sms_confirmation_sent_at": {"$exists": False},
        "sms_opted_out": {"$ne": True},
        "phone": {"$exists": True, "$nin": [None, ""]},
    })
    fired = 0
    async for cand in cur:
        dt = _parse_training_dt(cand.get("training_start_at") or "")
        if not dt:
            continue

        # Get per-pipeline lead hours (falls back to LEAD_HOURS env default)
        pid = cand.get("pipeline_id") or ""
        if pid not in pipeline_lead_hours:
            tpl = await _get_starter_tpl(db, cand)
            try:
                pipeline_lead_hours[pid] = float(tpl.get("confirmation_sms_lead_hours") or LEAD_HOURS)
            except (TypeError, ValueError):
                pipeline_lead_hours[pid] = LEAD_HOURS
        lead = pipeline_lead_hours[pid]

        send_at = dt - timedelta(hours=lead)
        if not (send_at <= now_et <= dt):
            continue
        local_hour = now_et.hour
        if local_hour < 8 or local_hour >= 21:
            continue
        out = await _send_confirmation_sms(db, cand["id"])
        if out.get("ok"):
            fired += 1
        else:
            logger.warning("training_sms scheduler: skip %s — %s", cand.get("id"), out.get("reason"))
    if fired:
        logger.info("training_sms scheduler: sent %d confirmation SMS(s)", fired)
    return fired


# ─── inbound webhook ─────────────────────────────────────────────────────────

async def handle_inbound(
    db,
    From: str,
    Body: str,
    To: Optional[str],
    MessageSid: Optional[str],
    request: Request,
):
    """Core inbound handler — called by the FastAPI endpoint below."""
    handled_at = time.monotonic()
    # Twilio signs every webhook; that signature is the only proof the text
    # really came from the number in `From`. Fails closed (deps/webhook_auth):
    # without TWILIO_AUTH_TOKEN, or with a bad signature, nothing is processed.
    from deps import twilio_signature_invalid
    if await twilio_signature_invalid(request):
        logger.warning("training_sms: inbound SMS refused (Twilio signature not valid)")
        raise HTTPException(status_code=403, detail="Invalid signature")

    sender = _e164(From)
    to_clean = (To or "").strip()

    # Idempotency. Twilio abandons an inbound webhook after ~15s and retries it,
    # and the screening path can legitimately take longer than that — a context
    # fetch plus one or two LLM calls. Without this guard a retry re-runs the
    # whole handler: a second reply, a second [BOOK:] (re-booking the slot,
    # re-sending approval comms, re-scheduling reminders), a duplicate verdict
    # and duplicate recruiter notifications. The SID is unique per inbound
    # message, so a row already logged for it means we have already handled it.
    if MessageSid:
        try:
            seen = await db.training_sms_messages.find_one(
                {"message_sid": MessageSid, "direction": "in"}, {"_id": 1})
            if seen:
                logger.info("training_sms: duplicate inbound %s ignored", MessageSid)
                return _twiml("")
        except Exception as e:
            logger.warning("training_sms: dedupe check failed for %s: %s", MessageSid, e)

    classification = _classify(Body)

    # ── Find candidate: match by phone + pipeline Twilio number (To field) ──
    # Search ALL pipeline stages — TRAINING first (most time-sensitive),
    # then most-recently-active candidate in any other stage.
    cand = None
    pipeline = None
    if sender:
        digits = re.sub(r"\D", "", sender)
        pipeline_ids: list = []
        pipeline_map: dict = {}
        if to_clean:
            to_digits = re.sub(r"\D", "", to_clean)
            async for pipe in db.pipelines.find({"twilio_phone_number": {"$exists": True, "$ne": ""}}, {"_id": 0}):
                pnum_digits = re.sub(r"\D", "", pipe.get("twilio_phone_number") or "")
                if pnum_digits and (pnum_digits == to_digits or same_phone(pnum_digits, to_digits)):
                    pipeline_ids.append(pipe["id"])
                    pipeline_map[pipe["id"]] = pipe

        # Narrow on the phone number in the QUERY itself, then rank the handful
        # of hits in Python. Scanning a stage-ordered window and comparing phones
        # afterwards silently orphaned replies once an office grew past the
        # window size — every candidate below the cutoff became unreachable.
        if digits:
            needle = digits[-10:] if len(digits) >= 10 else digits
            # Phones are stored in whatever shape they arrived in ('+12035551234',
            # '(203) 555-1234', '203.555.1234', …), so the digits are not always
            # contiguous — tolerate any separators between them.
            phone_rx = r"\D*".join(re.escape(d) for d in needle)
            phone_q: dict = {"phone": {"$regex": phone_rx}}
            if pipeline_ids:
                phone_q["pipeline_id"] = {"$in": pipeline_ids}

            # Preference order, best first: active TRAINING (most time-sensitive),
            # then any other active stage, then CLOSE, then archived (a reply from
            # an archived candidate still matters for STOP compliance, withdrawal
            # context, or a no-show asking to rebook). Ties break on recency.
            def _rank(c: dict) -> Tuple[int, str]:
                stage_ = (c.get("stage") or "").upper()
                if c.get("archived_at") is not None:
                    return 3, (c.get("updated_at") or "")
                if stage_ == "TRAINING":
                    return 0, (c.get("training_start_at") or "")
                if stage_ == "CLOSE":
                    return 2, (c.get("updated_at") or "")
                return 1, (c.get("updated_at") or "")

            matches = []
            async for c in db.candidates.find(phone_q).limit(100):
                cphone = re.sub(r"\D", "", c.get("phone") or "")
                if cphone and (cphone == digits or same_phone(cphone, digits)):
                    matches.append(c)
            if matches:
                matches.sort(key=lambda c: _rank(c)[1], reverse=True)  # recency desc
                matches.sort(key=lambda c: _rank(c)[0])                # stage pref asc (stable)
                cand = matches[0]
                pipeline = pipeline_map.get(cand.get("pipeline_id") or "")

        if cand and pipeline is None:
            pipeline = await db.pipelines.find_one({"id": cand.get("pipeline_id")}, {"_id": 0})

    cand_id = cand["id"] if cand else None
    pipe_id = cand.get("pipeline_id") if cand else None
    stage = (cand.get("stage") or "TRAINING").upper() if cand else "TRAINING"
    # Web-chat history joins the SMS context only while the SCREENING is the
    # live conversation. Replies to appointment confirmations, reminders and
    # training reschedules keep their channel-pure context — old screening
    # turns would only muddy "are you still coming on Thursday?".
    # Gate-check candidates (booked with zero screening) count as screening
    # conversations despite their stage — that's the entire point of the check.
    _screening_stage = stage in ("SCREENING", "APPLICANT") or (
        (cand or {}).get("screening_material") == "none" and _gate_checks_on()
    )

    # iPhone tapbacks arrive as SMS text: 'Loved "You're welcome, see you
    # Tuesday!"'. They are sentiment, not conversation — a real candidate
    # reacted to a goodbye and the model, reading its own words quoted back,
    # replied with commentary on its own message quality ("Glad that landed
    # well! Warm but not over the top…"). Log the reaction for the thread's
    # story, send nothing back: nobody expects a reply to a thumbs-up.
    if cand_id and _TAPBACK_RE.match(_strip_invisible(Body or "")):
        await _log_message(
            db, candidate_id=cand_id, pipeline_id=pipe_id,
            direction="in", body=Body or "", sender=sender, recipient=to_clean,
            classification="reaction", message_sid=MessageSid,
        )
        return _twiml("")

    # Mid-screening, "yes" and "no" are ANSWERS, not commands. _classify runs on
    # the raw text before anything knows what question was asked, so "no" to "can
    # you commute four days a week?" would land in the branch that cancels
    # appointments and archives people, and "sounds good" would be read as
    # confirming an interview that hasn't been booked yet. Only an explicit
    # withdrawal means what those branches assume; everything else goes to the
    # model, which can see which question it just asked. STOP is untouched —
    # that is a legal instruction, not a screening answer.
    _stamp_sms_confirm = False
    if _screening_stage and classification in ("yes", "no"):
        # Gated on the same mode as the questionnaire above: this only makes
        # sense when the assistant is the one that asked the question. Under
        # voice_first the screening happens on a call, so a bare "no" over text
        # means what it always meant and keeps its existing branch.
        _mode = "voice_first"
        try:
            from deps import resolve_settings
            from models import resolve_screening_mode
            _mode = resolve_screening_mode(
                await resolve_settings((cand or {}).get("user_id") or "",
                                       (cand or {}).get("pipeline_id"))
            )
        except Exception as e:
            logger.warning("could not resolve screening mode for %s: %s", cand_id, e)
        # Gate-check candidates are mid-question whatever their pipeline's mode
        # AND whatever their stage — they're booked (APPOINTMENT), which is
        # exactly why a bare "No" must reach the screener as an answer to
        # "are you 18 or over?" rather than the branch that cancels interviews,
        # and a bare "Yes" must not read as confirming attendance.
        _gate_check_thread = (cand or {}).get("screening_material") == "none" and _gate_checks_on()
        if (_gate_check_thread or _mode in ("chat_first", "chat_only")) and not _is_withdrawn(Body or ""):
            # A booked candidate's "Y" is an attendance confirmation WHATEVER
            # else the screener does with it — the reroute was swallowing it,
            # so gate-check candidates who replied Y to the confirm chaser
            # never counted as confirmed and the session alarm undercounted.
            if classification == "yes" and stage == "APPOINTMENT" and (cand or {}).get("appointment_at"):
                _stamp_sms_confirm = True
            classification = "unknown"

    # A training-lapsed archive is a filing decision, not a goodbye. Someone
    # texting back after the sweep filed them is exactly the rebook case it
    # planned for — restore them to the board at their old stage BEFORE the
    # reply is handled, with a fresh 4-day hold so the sweep waits out the
    # new-date conversation. STOP/withdrawal messages stay archived: being put
    # back on a recruiter's board is not what "leave me alone" asked for.
    if cand and classification not in ("stop", "maybe_stop") and not _is_withdrawn(Body or ""):
        try:
            from dialer.training_archive_sweep import unarchive_on_contact
            await unarchive_on_contact(db, cand)
        except Exception as e:
            logger.warning("training-lapsed unarchive failed for %s: %s", cand_id, e)

    if cand_id and stage in ("SCREENING", "APPLICANT"):
        try:
            _now = _now_iso()
            _reply_set = {
                # `updated_at` keeps a live text screening looking fresh so the
                # 22-minute phone reconcile never mistakes an actively-answering
                # candidate for a stale ghost.
                "updated_at": _now,
                # `retry_chat_last_at` is what the abandoned-screening nudge
                # sweep ranges on. It was only ever set by the WEB chat, so an
                # SMS-screening candidate who went quiet was chased by nothing —
                # the exact "text first, retry message, then a call" follow-up
                # the rollout promised. Setting it here puts SMS candidates into
                # the same 2h / 6h / 24h chase, and clears any old nudge count
                # so re-engaging earns a fresh sequence.
                "retry_chat_last_at": _now,
                "retry_chat_nudge_sent_at": None,
                "retry_chat_nudge_count": 0,
            }
            # Speed-to-contact's second half. Set once — reply, go quiet, reply
            # again tomorrow, and there is still one first-reply time.
            if not (cand or {}).get("first_reply_at"):
                _reply_set["first_reply_at"] = _now
                _reply_set["first_reply_channel"] = "sms"
            await db.candidates.update_one({"id": cand_id}, {"$set": _reply_set})
        except Exception as e:
            logger.warning("could not stamp reply state for %s: %s", cand_id, e)
        # chat_first's contract is "the call follows only if they haven't
        # replied" — but the fallback call was created at intake and nothing
        # ever moved it, so a candidate three answers deep at minute 115 got a
        # cold call at minute 120 that started the screening from scratch.
        # Every inbound answer now pushes the call out by the configured delay;
        # it only ever fires chat_first_delay_minutes after their LAST reply.
        # voice_first pipelines are untouched — there the call IS the screening.
        await _defer_chat_first_call(db, cand)

    await _log_message(
        db, candidate_id=cand_id, pipeline_id=pipe_id,
        direction="in", body=Body or "", sender=sender, recipient=to_clean,
        classification=classification, message_sid=MessageSid,
    )

    # Ping a human on the bell for every inbound reply from a matched candidate
    # (the AI still auto-handles the thread — this is awareness + a clickable
    # deep-link into the Inbox so nothing texted back ever goes unseen). The
    # unmatched/orphan case is notified separately in the orphan branch below.
    if cand_id and (cand or {}).get("user_id"):
        try:
            from notifications_service import create_notification
            nm = f"{(cand or {}).get('first_name', '')} {(cand or {}).get('last_name', '')}".strip() or "A candidate"
            await create_notification(
                (cand or {})["user_id"], "sms.reply",
                f"💬 {nm} replied by text",
                body=(Body or "")[:180],
                link=f"/inbox?candidate={cand_id}",
                candidate_id=cand_id,
                pipeline_id=pipe_id,
            )
        except Exception as e:
            logger.warning(f"inbound-sms notification failed: {e}")

    # ── Trainee-correspondence watch ─────────────────────────────────────────
    # A starter once texted that they were lost on the way
    # in. The message classified `unknown`, the concierge answered with the
    # address, and the sms.reply ping above is a routine kind the local bell
    # drops — so no human ever heard. Since 2026-08-18 the rule is the owner's:
    # CG1 admins hear genuine questions/correspondence from TRAINING candidates
    # and NOTHING in the stop/withdraw family. So a decline, STOP, or polite
    # resignation never pages; a question pages any time (calmly outside the
    # start window, as the siren inside it), and a clean YES pages only on
    # start morning, where it is the answer the office is waiting on.
    if cand_id and stage == "TRAINING":
        try:
            from cg1_alerts import office_label, resolve_office_key, spawn_cg1_admin_alert
            _in_window = _within_start_watch_window((cand or {}).get("training_start_at") or "")
            _withdraw_family = classification in ("no", "stop", "maybe_stop") or _is_withdrawn(Body or "")
            _nm = f"{(cand or {}).get('first_name', '')} {(cand or {}).get('last_name', '')}".strip() or "A starter"
            _start_dt = _parse_training_dt((cand or {}).get("training_start_at") or "")
            _start_str = _start_dt.strftime("%-I:%M %p") if _start_dt else "today"
            _office = office_label(resolve_office_key(pipeline))
            _quoted = (Body or "").strip()[:300]
            if _withdraw_family:
                pass  # stop/withdraw/decline — admins asked not to be paged
            elif classification == "yes":
                if _in_window:
                    spawn_cg1_admin_alert(
                        "start_morning.confirmed",
                        f"✅ {_nm} confirmed this morning's start",
                        f'"{_quoted}" — starts {_start_str} today ({_office})',
                        candidate=cand, pipeline=pipeline,
                    )
            elif _in_window:
                spawn_cg1_admin_alert(
                    "start_morning.reply",
                    f"🚨 Start-morning text — {_nm}",
                    f'"{_quoted}" — starts {_start_str} today ({_office})',
                    candidate=cand, pipeline=pipeline,
                )
            else:
                _start_lbl = _start_dt.strftime("%a %b %-d, %-I:%M %p") if _start_dt else "date TBC"
                spawn_cg1_admin_alert(
                    "starter.message",
                    f"💬 New-hire text — {_nm}",
                    f'"{_quoted}" — starts {_start_lbl} ({_office})',
                    candidate=cand, pipeline=pipeline,
                )
        except Exception as e:
            logger.warning("trainee-correspondence CG1 alert failed for %s: %s", cand_id, e)

    update: dict = {
        "sms_reply_text": Body,
        "sms_reply_at": _now_iso(),
        "sms_reply_from": sender,
    }
    if _stamp_sms_confirm:
        update["appointment_sms_confirmed"] = True
        update["appointment_sms_confirmed_at"] = _now_iso()
    reply_text: Optional[str] = None
    was_llm_reply = False
    _llm_retry_pending = False
    # True when the guard paused this thread mid-handling: the reply is a
    # human's now, so the webhook must answer with SILENCE — the old fallthrough
    # texted a cheery "Thanks!" that was never logged, so the inbox showed the
    # candidate talking into a void that was actually replying.
    _guard_silenced = False

    # Already opted out. STOP writes sms_opted_out, but that only ever suppressed
    # PROACTIVE sends — the guard before an AI reply checks a different field
    # (sms_replies_paused), and a TwiML reply skips the opt-out list entirely.
    # So an opted-out person who kept texting kept getting AI replies. A repeat
    # STOP re-confirms; anything else is logged and gets silence.
    if cand and cand.get("sms_opted_out") and classification != "stop":
        await _log_message(
            db, candidate_id=cand_id, pipeline_id=pipe_id,
            direction="in", body=Body or "", sender=sender, recipient=to_clean,
            classification="opted_out", message_sid=MessageSid,
        )
        return _twiml("")

    # ── STOP: always handled regardless of stage ──────────────────────────────
    if classification == "stop":
        update["sms_opted_out"] = True
        if stage == "TRAINING":
            update["sms_confirmation_status"] = "opted_out"
        reply_text = f"You've been unsubscribed and won't receive further messages from {company_profile.company_name()}."
        if sender:
            digits_inner = re.sub(r"\D", "", sender)
            await db.candidates.update_many(
                {"phone": {"$regex": digits_inner[-10:]}},
                {"$set": {"sms_opted_out": True}},
            )
        # The candidate flag alone was never enough: send_candidate_sms gates on
        # the sms_optouts collection, and nothing in the codebase ever wrote to
        # it. That collection has been empty this whole time, so roughly fifteen
        # send paths would happily text someone who had replied STOP. Twilio's
        # own carrier-level blocking has been masking it.
        try:
            from sms_service import mark_opted_out
            await mark_opted_out(db, sender or "")
        except Exception as e:
            logger.warning("could not record opt-out for %s: %s", sender, e)
        if cand_id and (cand or {}).get("user_id"):
            await _apply_withdrawn(db, cand_id, (cand or {})["user_id"], Body or "")

    # ── MAYBE STOP: ask, don't guess ──────────────────────────────────────────
    # They said something that reads like wanting out but isn't the keyword.
    # Acting on it would be guessing; ignoring it would leave someone who asked
    # to be left alone still being messaged. So answer, and give them the one
    # unambiguous way to leave.
    elif classification == "maybe_stop":
        reply_text = OPT_OUT_CONFIRMATION

    # ── YES ───────────────────────────────────────────────────────────────────
    elif classification == "yes":
        if stage == "TRAINING":
            proposed = (cand or {}).get("sms_proposed_reschedule_date") if cand else None
            # A proposal left over from a thread that died months ago is a past
            # date — a YES today must not "reschedule" them into history.
            try:
                if proposed and datetime.strptime(proposed, "%Y-%m-%d").date() < datetime.now(ET).date():
                    proposed = None
            except ValueError:
                pass
            # An offer made before its week was cancelled still books a real
            # cohort; the reply below names the date they actually get.
            try:
                if proposed:
                    proposed = _skip_no_cohort(proposed)
            except ValueError:
                pass
            if proposed and cand_id:
                try:
                    nd = datetime.strptime(proposed, "%Y-%m-%d").strftime("%A %b %-d")
                except Exception:
                    nd = proposed
                dt_obj = _parse_training_dt(cand.get("training_start_at") or "")
                time_str = dt_obj.strftime("%-I:%M %p") if dt_obj else "1:00 PM"
                new_iso = f"{proposed}T{dt_obj.strftime('%H:%M:%S') if dt_obj else '13:00:00'}"
                # A reschedule is NOT a confirmation for the new date — mark it
                # 'rescheduled' (distinct from a clean 'confirmed') so a starter who
                # bailed and rebooked never shows as confirmed on the roster. We
                # unset sms_confirmation_sent_at so the pre-start confirmation SMS
                # re-fires ~lead-hours before the new date; the status then flows
                # rescheduled → sent → confirmed once they reply YES closer to it.
                # sms_confirmed_at is cleared for the same reason.
                await db.candidates.update_one(
                    {"id": cand_id},
                    {
                        "$set": {
                            "training_start_at": new_iso,
                            "sms_confirmation_status": "rescheduled",
                            "sms_rescheduled_at": _now_iso(),
                            "sms_reschedule_count": int((cand or {}).get("sms_reschedule_count") or 0) + 1,
                            "sms_proposed_reschedule_date": None,
                        },
                        "$unset": {"sms_confirmation_sent_at": "", "sms_confirmed_at": ""},
                    },
                )
                update.pop("sms_confirmation_status", None)
                # Mirror the manual reschedule-training endpoint: push the new
                # week to the NEW HIRES sheet, or the office roster keeps the
                # old date and the rebooked starter walks in unexpected.
                try:
                    import asyncio as _asyncio
                    from sheets_service import update_hire_start_date as _update_we
                    _pipe = pipeline or {}
                    _office = company_profile.office_key_for_pipeline(_pipe)
                    if _office:
                        _cand_name = f"{(cand or {}).get('first_name', '')} {(cand or {}).get('last_name', '')}".strip()
                        await _asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: _update_we(
                                office_key=_office,
                                email=(cand or {}).get("email") or "",
                                name=_cand_name,
                                training_start_at=new_iso,
                            ),
                        )
                except Exception as _se:
                    logger.warning("training_sms: sheet WE update failed for %s (non-fatal): %s", cand_id, _se)
                # No CG1 page for repeat reschedules (owner's rule, 2026-08-18):
                # a reschedule is a status change, not trainee correspondence.
                reply_text = f"You're booked in for {nd} at {time_str} 📅 We'll send a quick confirmation closer to the day — see you then!"
            elif cand_id and _start_is_stale(cand):
                # A bare YES weeks after a missed start confirms nothing — it
                # answers whatever the conversation is now about ("still
                # interested?"). Stamping the dead date 'confirmed' would put a
                # no-show on the roster for a day that already happened. Let
                # the model carry the re-engagement; if it lands on the
                # next-Monday offer, the proposed-date path above books it on
                # their NEXT yes.
                history = await _get_thread(db, cand_id, include_chat=_screening_stage)
                history = [h for h in history if not (h["direction"] == "in" and h["body"] == (Body or ""))]
                raw = await _generate_llm_reply(db, cand, pipeline or {}, history, Body or "", "follow_up")
                reply_text, proposed_iso = await _handle_reply_markers(db, cand, pipeline or {}, stage, raw or "", Body or "")
                if proposed_iso:
                    update["sms_proposed_reschedule_date"] = proposed_iso
                was_llm_reply = bool(reply_text)
                if not reply_text:
                    reply_text = f"Thanks for getting back to us! A member of our team will be in touch shortly. — {company_profile.company_name()}"
            elif cand_id and not _confirm_process_open(cand):
                # A YES days before the start is conversation, not confirmation
                # — enthusiasm, or an answer to whatever the assistant just
                # asked. The roster must never show someone confirmed before
                # the day-of process (the 10 AM confirmation SMS) has begun;
                # sms_confirmation_sent_at stays unset so the scheduler still
                # fires that chaser, and THAT yes is the one that counts.
                history = await _get_thread(db, cand_id, include_chat=_screening_stage)
                history = [h for h in history if not (h["direction"] == "in" and h["body"] == (Body or ""))]
                raw = await _generate_llm_reply(db, cand, pipeline or {}, history, Body or "", "follow_up")
                reply_text, proposed_iso = await _handle_reply_markers(db, cand, pipeline or {}, stage, raw or "", Body or "")
                if proposed_iso:
                    update["sms_proposed_reschedule_date"] = proposed_iso
                was_llm_reply = bool(reply_text)
                if not reply_text:
                    reply_text = f"Great — you're all set! We'll text you a quick confirmation on the morning of your start day. — {company_profile.company_name()}"
            else:
                update["sms_confirmation_status"] = "confirmed"
                update["sms_confirmed_at"] = _now_iso()
                # Sign off as the brand, not the office — a city ("— Downtown")
                # reads oddly as a signature.
                reply_text = f"Thanks for confirming! See you then. — {company_profile.company_name()} ✅"
        elif stage == "APPOINTMENT":
            # A bare "yes" means "I'll be there" only when we last asked them to
            # confirm. If the assistant was mid-conversation — offering slots
            # after they said they couldn't attend — "yes" means "that time
            # works", and auto-confirming the OLD slot would be exactly wrong.
            mid_conversation = bool((cand or {}).get("appointment_cancelled_at")) or (
                cand_id and await _last_outbound_was_ai(db, cand_id)
            )
            if mid_conversation and cand_id:
                history = await _get_thread(db, cand_id, include_chat=_screening_stage)
                history = [h for h in history if not (h["direction"] == "in" and h["body"] == (Body or ""))]
                raw = await _generate_llm_reply(db, cand, pipeline or {}, history, Body or "", "follow_up")
                reply_text, _ = await _handle_reply_markers(db, cand, pipeline or {}, stage, raw or "", Body or "")
                was_llm_reply = bool(reply_text)
            if not reply_text and not mid_conversation:
                update["appointment_sms_confirmed"] = True
                update["appointment_sms_confirmed_at"] = _now_iso()
                appt_at = (cand or {}).get("appointment_at") or ""
                appt_str = None
                try:
                    dt = datetime.fromisoformat(appt_at.replace("Z", "+00:00")).astimezone(ET)
                    appt_str = dt.strftime("%-I:%M %p %Z on %A, %b %-d")
                except Exception:
                    pass
                if appt_str:
                    reply_text = f"Confirmed! See you at {appt_str}. Good luck! — {company_profile.company_name()}"
                else:
                    reply_text = f"Confirmed! See you at your interview. Good luck! — {company_profile.company_name()}"
            elif not reply_text:
                reply_text = f"Got it! Let me know which time suits you and I'll lock it in. — {company_profile.company_name()}"
        else:
            # A bare "Yes" outside TRAINING/APPOINTMENT is usually the ANSWER to
            # the current screening question — the chat-first arrival text itself
            # asks one ("are you 18 or over?"). Route it through the full
            # screening path so control markers act and the next question goes
            # out. Found live on go-live night: candidate #1 answered "Yes" and
            # got a warm acknowledgment promising a call instead of question 2.
            # Voice-first offices are unaffected: their system prompt carries no
            # screening block and the marker gates hold, so this is a concierge
            # reply there exactly as before.
            if cand_id:
                history = await _get_thread(db, cand_id, include_chat=_screening_stage)
                history = [h for h in history if not (h["direction"] == "in" and h["body"] == (Body or ""))]
                raw = await _generate_llm_reply(db, cand, pipeline or {}, history, Body or "", "follow_up")
                reply_text, _ = await _handle_reply_markers(db, cand, pipeline or {}, stage, raw or "", Body or "")
                was_llm_reply = bool(reply_text)
            if not reply_text:
                reply_text = f"Great, looking forward to it! — {company_profile.company_name()}"

    # ── NO ────────────────────────────────────────────────────────────────────
    elif classification == "no" and cand_id:
        guard = _guard_decision(db, cand, Body or "", await _inbound_count(db, cand_id))
        if not guard.should_reply and cand:
            await _pause_auto_replies(db, cand, guard, Body or "")
            _guard_silenced = True
        # Mirror of the YES branch's disambiguation: a bare "No" is a decline
        # only when we last asked something a "No" declines — the chaser, a
        # reminder. When the assistant was mid-conversation, "No" answers the
        # assistant's own question ("any questions before Thursday?") and must
        # NOT cancel anything; the model still holds [CANCEL] for cancellations
        # said conversationally. An explicit decline PHRASE ("can't make it",
        # "have to cancel") acts immediately in either case — nobody who said
        # they can't come should still hear "your interview is in 1 hour".
        _no_mid_conversation = bool((cand or {}).get("appointment_cancelled_at")) or (
            await _last_outbound_was_ai(db, cand_id)
        )
        _explicit_decline = _matches_no_phrase(Body or "")
        if stage == "TRAINING" and (_explicit_decline or not _no_mid_conversation):
            update["sms_confirmation_status"] = "declined"
            update["sms_confirmed_at"] = _now_iso()
        # Hard withdrawal phrases → archive + cancel calls immediately
        if _is_withdrawn(Body or "") and (cand or {}).get("user_id"):
            await _apply_withdrawn(db, cand_id, (cand or {})["user_id"], Body or "")
        elif stage == "APPOINTMENT" and cand and (_explicit_decline or not _no_mid_conversation):
            await _apply_appointment_cancellation(db, cand, Body or "")
            cand = await db.candidates.find_one({"id": cand_id}, {"_id": 0}) or cand
        if guard.should_reply:
            history = await _get_thread(db, cand_id, include_chat=_screening_stage)
            history = [h for h in history if not (h["direction"] == "in" and h["body"] == (Body or ""))]
            raw = await _generate_llm_reply(db, cand, pipeline or {}, history, Body or "", "no")
            reply_text, proposed_iso = await _handle_reply_markers(db, cand, pipeline or {}, stage, raw or "", Body or "")
            if proposed_iso and stage == "TRAINING":
                update["sms_proposed_reschedule_date"] = proposed_iso
            was_llm_reply = bool(reply_text)
        if not reply_text and not _guard_silenced:
            reply_text = "Got it — thanks for letting us know. We'll be in touch."

    # ── UNKNOWN: full LLM with stage context ─────────────────────────────────
    elif classification == "unknown" and cand_id:
        guard = _guard_decision(db, cand, Body or "", await _inbound_count(db, cand_id))
        if not guard.should_reply and cand:
            await _pause_auto_replies(db, cand, guard, Body or "")
            _guard_silenced = True
        # A withdrawal only stopped the machine when it also read as a "no". Said
        # politely and at length — which is how people actually resign — it
        # classifies as `unknown` and went straight to the model, so the reminders
        # for a job the candidate had just quit stayed armed. Act on it here too,
        # then still let the model write the reply: the acknowledgement was the one
        # part of that goodbye thread that was right.
        if _is_withdrawn(Body or "") and (cand or {}).get("user_id"):
            await _apply_withdrawn(db, cand_id, (cand or {})["user_id"], Body or "")
            cand = await db.candidates.find_one({"id": cand_id}, {"_id": 0}) or cand
        if guard.should_reply:
            history = await _get_thread(db, cand_id, include_chat=_screening_stage)
            history = [h for h in history if not (h["direction"] == "in" and h["body"] == (Body or ""))]
            intent = "follow_up" if any(h["direction"] == "out" for h in history) else "unknown"
            raw = await _generate_llm_reply(db, cand, pipeline or {}, history, Body or "", intent)
            reply_text, proposed_iso = await _handle_reply_markers(db, cand, pipeline or {}, stage, raw or "", Body or "")
            if proposed_iso:
                update["sms_proposed_reschedule_date"] = proposed_iso
            was_llm_reply = bool(reply_text)
        if guard.should_reply and not reply_text:
            if _screening_stage:
                # No apology, no keypad line — a failed generation gets a
                # quiet background second chance and the candidate simply
                # waits a little longer for the real answer.
                _schedule_llm_retry(db, cand, pipeline, stage, _screening_stage,
                                    Body or "", sender or "", to_clean)
                _llm_retry_pending = True
            elif stage == "TRAINING" and _start_is_stale(cand):
                # The canned line below asks them to confirm attendance — for a
                # start date that already passed, that's the exact reply this
                # gate exists to prevent.
                reply_text = f"Thanks for reaching out! A member of our team will get back to you shortly. — {company_profile.company_name()}"
            else:
                reply_text = "Thanks! Reply YES to confirm attendance, NO to decline, or STOP to opt out."

    if cand_id:
        await db.candidates.update_one({"id": cand_id}, {"$set": update})
    else:
        await db.training_sms_orphans.insert_one({
            "from": sender, "to": to_clean, "body": Body,
            "received_at": _now_iso(), "message_sid": MessageSid,
            "classification": classification,
        })
        # Surface unmatched replies on the notification bell so a human can
        # triage them — e.g. a candidate texting from a different number than
        # they applied with ("I don't want the job anymore" must not vanish).
        try:
            owner_pipe = None
            if pipeline_map:
                owner_pipe = next(iter(pipeline_map.values()), None)
            if owner_pipe and owner_pipe.get("user_id"):
                from notifications_service import create_notification
                masked = f"…{sender[-4:]}" if sender and len(sender) >= 4 else (sender or "unknown")
                await create_notification(
                    owner_pipe["user_id"], "sms.unmatched",
                    f"💬 Unmatched SMS reply ({owner_pipe.get('name', '')})",
                    body=f"From {masked}: {(Body or '')[:180]}",
                    link=f"/inbox?phone={sender}",
                    pipeline_id=owner_pipe.get("id"),
                )
        except Exception as e:
            logger.warning(f"unmatched-sms notification failed: {e}")
        if not reply_text:
            reply_text = "Thanks for your message."

    if _llm_retry_pending:
        # The background regeneration owns this reply now — answer the
        # webhook empty so nothing interim reaches the phone.
        return _twiml("")

    if cand_id and reply_text:
        # LLM conversation replies are paced like a person typing them (see
        # reply_pacing) — an answer landing one second after the candidate's
        # reads as automation. TwiML can't delay (Twilio abandons the webhook
        # at ~15s and retries, and the reply IS the response), so the paced
        # path answers the webhook empty and sends via the REST API instead.
        # Everything non-conversational — STOP/YES/NO confirmations, canned
        # fallbacks, orphans — keeps the instant TwiML reply: compliance
        # messages must never sit in a pacing queue.
        if was_llm_reply and sender:
            from reply_pacing import remaining_delay_seconds

            delay = remaining_delay_seconds(reply_text, Body or "", time.monotonic() - handled_at)
            # The task reference MUST be kept until it finishes — a bare
            # create_task can be garbage-collected mid-sleep, which here
            # would silently drop the reply.
            task = asyncio.create_task(_send_paced_reply(
                db, cand_id=cand_id, pipeline_id=pipe_id, body=reply_text,
                from_number=to_clean, to_number=sender, delay_seconds=delay,
            ))
            _PACED_REPLY_TASKS.add(task)
            task.add_done_callback(_PACED_REPLY_TASKS.discard)
            return _twiml("")
        await _log_message(
            db, candidate_id=cand_id, pipeline_id=pipe_id,
            direction="out", body=reply_text, sender=to_clean, recipient=sender,
            was_llm_reply=was_llm_reply,
        )

    # A guard-paused thread answers with true silence: the pause means a human
    # owns the conversation now, and any auto-text — especially an unlogged one
    # — contradicts that. Everyone else keeps the friendly fallback.
    return _twiml(reply_text or ("" if _guard_silenced else "Thanks!"))


# Strong references to in-flight paced replies — asyncio only weakly holds
# scheduled tasks, so without this set a reply could be GC'd before it sends.
_PACED_REPLY_TASKS: set = set()

# In-flight background regenerations after a failed LLM call. Same GC rule.
_LLM_RETRY_TASKS: set = set()


def _schedule_llm_retry(
    db, cand: dict, pipeline: Optional[dict], stage: str, screening_stage: bool,
    inbound_body: str, sender: str, to_clean: str,
) -> None:
    """A failed generation mid-screening gets a quiet second chance, not an
    apology. Rate-limit bursts pass in seconds — so wait one out, regenerate
    with full context, and send through the paced path. The candidate just
    experiences a slower reply. Only if the retry ALSO fails does the
    apology line go out (through the paced sender, whose duplicate guard
    stops it ever appearing twice in a row again)."""
    cand_id, pipe_id = cand.get("id"), cand.get("pipeline_id")
    attempted_at = _now_iso()

    async def _run():
        try:
            await asyncio.sleep(18)
            # If anything already went out since the failure (the candidate
            # re-texted and THAT webhook succeeded), stand down — a late
            # second answer to the same question reads worse than the delay.
            last_out = await db.training_sms_messages.find(
                {"candidate_id": cand_id, "direction": "out"},
                {"_id": 0, "timestamp": 1},
            ).sort("timestamp", -1).limit(1).to_list(1)
            if last_out and str(last_out[0].get("timestamp") or "") > attempted_at:
                logger.info(f"llm retry for {cand_id} stood down — thread moved on")
                return
            fresh = await db.candidates.find_one({"id": cand_id}, {"_id": 0}) or cand
            history = await _get_thread(db, cand_id, include_chat=screening_stage)
            history = [h for h in history if not (h["direction"] == "in" and h["body"] == inbound_body)]
            raw = await _generate_llm_reply(db, fresh, pipeline or {}, history, inbound_body, "follow_up")
            reply, _ = await _handle_reply_markers(db, fresh, pipeline or {}, stage, raw or "", inbound_body)
            if not reply:
                reply = "Sorry, I lagged for a second there - could you send that once more?"
            await _send_paced_reply(
                db, cand_id=cand_id, pipeline_id=pipe_id, body=reply,
                from_number=to_clean, to_number=sender, delay_seconds=0,
            )
        except Exception as e:
            logger.warning(f"llm retry failed for {cand_id}: {e}")

    task = asyncio.create_task(_run())
    _LLM_RETRY_TASKS.add(task)
    task.add_done_callback(_LLM_RETRY_TASKS.discard)


async def _send_paced_reply(
    db, *, cand_id: str, pipeline_id: Optional[str], body: str,
    from_number: str, to_number: str, delay_seconds: float,
) -> None:
    """Sleep out the remaining thinking time, then send + log the reply.

    Logged at send time, not queue time, so the thread's timeline stays true.
    In-memory like the rest of the scheduler: a restart inside the window
    (≤15s) drops the reply, same trade-off the call queue already accepts.
    """
    try:
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        # Rapid double-texts ("Yes, full working rights" + "not a student visa"
        # seconds apart) run two handlers concurrently, and both can generate
        # near-identical replies — a real candidate got the schedule question
        # twice, four seconds apart. If an identical outbound was already
        # logged in the last few minutes, this one is the echo: drop it.
        recent_out = await db.training_sms_messages.find(
            {"candidate_id": cand_id, "direction": "out"},
            {"_id": 0, "body": 1, "timestamp": 1},
        ).sort("timestamp", -1).limit(1).to_list(1)
        if recent_out and (recent_out[0].get("body") or "").strip() == (body or "").strip():
            logger.info(f"paced reply for {cand_id} suppressed — identical to last outbound")
            return
        from voice_service import send_sms

        result = send_sms(to_number, body, from_number=from_number or None)
        if result.get("status") != "sent":
            logger.warning(
                f"paced reply not delivered for {cand_id}: {result.get('status')} "
                f"{result.get('error') or result.get('reason') or ''}"
            )
        await _log_message(
            db, candidate_id=cand_id, pipeline_id=pipeline_id,
            direction="out", body=body, sender=from_number, recipient=to_number,
            was_llm_reply=True,
        )
    except Exception as e:
        logger.warning(f"paced reply failed for {cand_id}: {e}")


def _twiml(message: str) -> Response:
    """A TwiML reply. An empty message returns an empty <Response/>, which tells
    Twilio to send nothing — used for the duplicate-inbound short-circuit."""
    if not message:
        return Response(content="<?xml version='1.0' encoding='UTF-8'?><Response></Response>",
                        media_type="application/xml")
    safe = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return Response(
        content=f"<?xml version='1.0' encoding='UTF-8'?><Response><Message>{safe}</Message></Response>",
        media_type="application/xml",
    )


# ─── FastAPI endpoints ────────────────────────────────────────────────────────

# The Twilio inbound-SMS webhook (POST /api/webhooks/twilio/inbound-sms) lives in
# routes/webhooks.py and delegates to handle_inbound above.


async def _authorize_candidate_caller(request: Request, cand: Optional[dict], *, action: str = "read") -> None:
    """Who may use the per-candidate SMS endpoints below.

    They used to check nothing (`user: dict = None` is not a dependency), so
    anyone holding a candidate id could read the thread or trigger a resend.
    Two legitimate callers exist and both keep working:
      * CG1's backend proxies them for its Recruits screen, sending the shared
        `x-webhook-secret` (CG1 routes/cgrecruit_webhook.py) - same secret and
        same header as /api/internal/*, but this check fails CLOSED if the
        secret is ever unset.
      * CGRecruit's own candidate drawer, with the signed-in user's session -
        then the usual tenant + pipeline (+ viewer ownership) rules apply, and
        read-only accounts can't send.
    """
    import hmac
    sent = request.headers.get("x-webhook-secret", "")
    if sent:
        secret = os.getenv("CGRECRUIT_WEBHOOK_SECRET", "")
        if secret and hmac.compare_digest(sent.encode("utf-8"), secret.encode("utf-8")):
            return
        raise HTTPException(403, "Invalid secret")
    from auth_service import get_current_user_payload
    from deps import assert_candidate_access, current_user, is_analyst, is_viewer
    # The request goes along so current_user can apply the per-route rules
    # (analysts never read a candidate's messages).
    user = await current_user(await get_current_user_payload(request, request.headers.get("authorization")), request)
    if not cand or cand.get("user_id") != user["id"]:
        raise HTTPException(404, "Candidate not found")
    await assert_candidate_access(user, cand)
    if action == "send" and (is_viewer(user) or is_analyst(user)):
        raise HTTPException(403, "This account is read-only")


@router.get("/candidates/{candidate_id}/sms-thread")
async def get_sms_thread(candidate_id: str, request: Request):
    """Return the full SMS thread for a TRAINING candidate."""
    from deps import db
    cand = await db.candidates.find_one({"id": candidate_id}, {"_id": 0})
    await _authorize_candidate_caller(request, cand)
    if not cand:
        raise HTTPException(404, "Candidate not found")
    msgs = await _get_thread(db, candidate_id)
    return {
        "candidate_id": candidate_id,
        "name": f"{cand.get('first_name','')} {cand.get('last_name','')}".strip(),
        "phone": cand.get("phone"),
        "pipeline_id": cand.get("pipeline_id"),
        "sms_confirmation_status": cand.get("sms_confirmation_status"),
        "sms_confirmed_at": cand.get("sms_confirmed_at"),
        "sms_opted_out": bool(cand.get("sms_opted_out")),
        "messages": msgs,
    }


@router.post("/candidates/{candidate_id}/sms/resend")
async def resend_confirmation_sms(candidate_id: str, request: Request):
    """Force-resend the confirmation SMS regardless of prior send state."""
    from deps import db
    cand = await db.candidates.find_one({"id": candidate_id}, {"_id": 0, "id": 1, "user_id": 1, "pipeline_id": 1, "added_by_user_id": 1})
    await _authorize_candidate_caller(request, cand, action="send")
    out = await _send_confirmation_sms(db, candidate_id, force=True)
    if not out.get("ok"):
        raise HTTPException(400, out.get("reason", "Send failed"))
    return out


@router.post("/webhooks/twilio/inbound-sms/tick")
async def manual_tick(request: Request):
    """Admin-only manual scheduler tick — useful for testing.

    It checked nothing, so anyone could make the scheduler send its due texts
    early. Now: the shared webhook secret, or a signed-in super admin."""
    import hmac
    from deps import db
    sent = request.headers.get("x-webhook-secret", "")
    secret = os.getenv("CGRECRUIT_WEBHOOK_SECRET", "")
    if not (sent and secret and hmac.compare_digest(sent.encode("utf-8"), secret.encode("utf-8"))):
        from auth_service import get_current_user_payload
        from deps import current_user, is_super_admin
        user = await current_user(await get_current_user_payload(request, request.headers.get("authorization")), request)
        if not is_super_admin(user):
            raise HTTPException(403, "Super-admin required")
    fired = await scheduler_tick(db)
    return {"ok": True, "fired": fired}
