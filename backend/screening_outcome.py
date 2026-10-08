"""Concluding a screening that happened in text, and making it count.

Until now the only channel that produced a *result* was the phone. After an AI
call the recruiter got a verdict, a score out of 100, a written summary, key
points, a status transition, a rejection email where one was warranted, and an
in-app notification. After a chat screening they got none of that — the
conversation ended and nothing was written down at all.

That was survivable while chat was the fallback after a missed call. It is not
survivable now that chat is the front door. Two consequences in particular:

  * A candidate who fails a hard gate — under 18, no work authorisation — ends
    the chat politely and is then indistinguishable from someone who never
    engaged. They are not rejected, not archived, get no rejection notice, and
    the dialler keeps ringing them.
  * A candidate who passes arrives on the board with no score and no summary,
    so the recruiter has nothing to judge them on.

So this module is the text equivalent of `_process_screening_post_call`, and it
deliberately writes the *same fields* with the *same values* — verdict, status,
disqualification label — so every existing badge, filter, notification and
metric works without knowing which channel did the screening.

Used by both the web chat (routes/retry.py) and SMS screening.
"""
import logging
from typing import Any, Dict, List, Optional

from models import now_iso
import company_profile
from app_tz import app_zone, default_tz_name  # APP_TIMEZONE (noqa: F401)

logger = logging.getLogger("screening_outcome")

# Master switch for texting screening questions at BOOKED candidates
# ("gate checks"). Killed on 2026-07-30: the flag was firing at people whose
# web-voice screenings had been dropped by the webhook — genuinely screened
# candidates got re-asked question one, holding a confirmation in hand.
# Owner's rule: no re-screening of anyone who already has a slot, in any way.
# Booked-with-no-material still rings the bell (a human decides); nothing
# texts them questions. Flip to True only deliberately.
GATE_CHECKS_BY_TEXT = False

# Verbatim from routes/webhooks.py — the recruiter-facing label a rejected
# candidate is filed under. Kept identical so the two channels don't produce
# two different vocabularies for the same rejection.
DISQ_LABELS = {
    "age": "minimum age requirement (18+)",
    "work_authorization": "right to work in this country",
    "schedule_availability": f"schedule availability ({company_profile.get('work_schedule', 'the advertised hours')})",
    "commute": "commutability to our office",
    "withdrawn": "candidate withdrew / no longer interested",
}


def preserve_attendance(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """A `$push` that keeps the outcome being cleared, or {} if there is nothing.

    Rescheduling clears `attendance_status` so the new appointment starts clean —
    which is right, the candidate has a fresh interview to attend. What was wrong
    is that it *destroyed* the old answer. Candidates who were marked
    a no-show, rescheduled, and now read as though nobody ever said anything about
    them; every one of the unmarked records carrying a `no_show_at` stamp also
    carries `previous_appointment_at`, so the reschedule is the whole explanation.

    That matters twice over. It hides a real no-show from every report, and it
    inflates the "nobody records outcomes" figure with cases where somebody did.

    Callers merge this into their existing update:
        update = {"$set": {...}}
        push = preserve_attendance(cand)
        if push: update["$push"] = push
    """
    status = candidate.get("attendance_status")
    if not status:
        return {}
    return {"attendance_history": {
        "status": status,
        "appointment_at": candidate.get("appointment_at"),
        "recorded_at": candidate.get("attendance_recorded_at"),
        "recorded_by": candidate.get("attendance_recorded_by"),
        "cleared_at": now_iso(),
    }}


def transcript_from_chat_log(chat_log: List[Dict[str, Any]], channels=("retry", "sms")) -> List[Dict[str, Any]]:
    """chat_log entries → the {role, text} shape the summariser expects.

    `role` is normalised to applicant/agent here because the two channels write
    it differently and the engagement counter only recognises certain spellings.
    """
    out = []
    for m in chat_log or []:
        if channels and (m.get("channel") not in channels):
            continue
        role = (m.get("role") or "").lower()
        out.append({
            "role": "applicant" if role in ("applicant", "candidate", "user", "human") else "agent",
            "text": m.get("text") or "",
        })
    return out


async def reopen_screening(db, candidate: Dict[str, Any], *, corrected: str = "") -> Dict[str, Any]:
    """Let a candidate back in after they correct a hard-gate answer.

    A failed gate ends the conversation, and it should — but it must not be a
    locked door. People misread "18 or over" on a phone screen, answer the
    schedule question thinking it means every day, or say they can't commute and
    then realise the office is two stops away. If they come back and say so, the
    only sensible response is to carry on, not to leave them talking to a system
    that has already written them off.

    Deliberately conservative about what it undoes: the rejection, the reason and
    the dialler stand-down go, so they are screenable again. The rejection notice
    that already went out is not retracted — the next thing they receive is the
    next screening question, which reads as continuing rather than as a reversal.
    """
    cand_id, user_id = candidate.get("id"), candidate.get("user_id")
    previous = candidate.get("disqualification_reason")

    # A cap, because reopen fires a staff notification and a re-rejection fires
    # comms, so an unbounded reject↔reopen loop is a spam and cost vector. Three
    # corrections is well past any honest "I misread the question"; beyond it the
    # candidate stays rejected and no more notifications go out.
    count = int(candidate.get("screening_reopened_count") or 0)
    if count >= 3:
        logger.info("reopen_screening: %s hit the reopen cap (%s) — staying rejected", cand_id, count)
        return {"ok": False, "reason": "cap_reached"}

    await db.candidates.update_one({"id": cand_id, "user_id": user_id}, {
        "$set": {
            "screening_status": "in_progress",
            "disqualification_reason": None,
            "verdict": None,
            "auto_dial": True,
            "archived_at": None,
            "archived_reason": None,
            "updated_at": now_iso(),
            # Keeps the fact it happened without keeping the consequence, so a
            # recruiter can see the answer changed rather than wondering why a
            # rejected candidate is suddenly booked.
            "screening_reopened_at": now_iso(),
            "screening_reopened_from": previous,
        },
        "$inc": {"screening_reopened_count": 1},
    })
    name = f"{candidate.get('first_name', '')} {candidate.get('last_name', '')}".strip() or "A candidate"
    try:
        from notifications_service import create_notification
        await create_notification(
            user_id, "screening.reopened",
            f"↩️ {name} corrected a screening answer",
            body=(f"Previously ruled out on {previous or 'a hard gate'}. They've since said "
                  f"otherwise{(': ' + corrected.strip()[:160]) if corrected else ''}. "
                  "Screening has resumed — worth a glance."),
            link=f"/?candidate={cand_id}",
            candidate_id=cand_id, pipeline_id=candidate.get("pipeline_id"),
        )
    except Exception as e:
        logger.warning(f"reopen_screening: notification failed for {cand_id}: {e}")
    logger.info(f"reopen_screening: {cand_id} reopened (was {previous})")
    return {"ok": True, "was": previous}


async def book_screening_slot(
    db,
    candidate: Dict[str, Any],
    pipeline: Dict[str, Any],
    slot_iso: str,
) -> Dict[str, Any]:
    """Book an interview off the back of a text screening. Returns {ok, reason}.

    Shared by the web chat and SMS so the two cannot drift into booking people
    differently. The capacity re-check is the reason this exists as one function
    rather than two: the slot list is computed at the top of a request and the
    booking happens at the bottom, so two candidates chatting at once could
    otherwise both be handed — and both take — the last seat in a room.
    """
    from availability_service import resolve_slot_link, resolve_slot_recruiter, slot_capacity_remaining
    from deps import resolve_settings

    cand_id, user_id = candidate.get("id"), candidate.get("user_id")
    settings = await resolve_settings(user_id, candidate.get("pipeline_id")) or {}
    tz_name = (settings.get("region_language") or {}).get("timezone") or default_tz_name()
    default_cap = int((settings.get("appointments") or {}).get("applicant_limit") or 50)

    if pipeline:
        booked = await db.candidates.find(
            {"pipeline_id": pipeline.get("id"), "appointment_at": slot_iso, "id": {"$ne": cand_id}},
            {"_id": 0, "appointment_at": 1},
        ).to_list(200)
        remaining = slot_capacity_remaining(
            pipeline, [b.get("appointment_at") for b in booked], slot_iso,
            default_capacity=default_cap, tz_name=tz_name,
        )
        if remaining == -1:
            return {"ok": False, "reason": "not_in_schedule"}
        if remaining is not None and remaining <= 0:
            return {"ok": False, "reason": "full"}

    from deps import booking_link_or_alert
    link = await booking_link_or_alert(pipeline or {}, slot_iso, tz_name, candidate)
    await db.candidates.update_one({"id": cand_id}, {"$set": {
        "stage": "APPOINTMENT",
        "screening_status": "approved",
        "appointment_at": slot_iso,
        "appointment_link": link,
        "appointment_recruiter": (
            resolve_slot_recruiter(pipeline or {}, slot_iso, tz_name)
            or (pipeline or {}).get("appointment_recruiter") or "Hiring Team"
        ),
        "archived_at": None,
        "archived_reason": None,
        "updated_at": now_iso(),
    }, "$push": {"chat_log": {
        # The Comms tab reads this. Without it a booking made by text leaves no
        # trace in the place a recruiter looks for the story of a candidate.
        "role": "agent", "channel": "system",
        "text": f"Booked an interview slot for {slot_iso}.", "at": now_iso(),
    }}})

    try:
        from deps import send_stage_comms
        fresh = await db.candidates.find_one({"id": cand_id}, {"_id": 0}) or candidate
        await send_stage_comms(user_id, fresh, "approval")
    except Exception as e:
        logger.warning(f"book_screening_slot: approval comms failed for {cand_id}: {e}")
    try:
        from auto_dialer import cancel_pending_retry_calls, schedule_appointment_reminders
        await schedule_appointment_reminders(user_id, cand_id, slot_iso)
        cancel_pending_retry_calls(cand_id)
    except Exception as e:
        logger.warning(f"book_screening_slot: reminders failed for {cand_id}: {e}")

    logger.info(f"book_screening_slot: {cand_id} booked {slot_iso}")
    return {"ok": True, "slot": slot_iso, "link": link}


async def conclude_text_screening(
    db,
    candidate: Dict[str, Any],
    *,
    channel: str,
    transcript: Optional[List[Dict[str, Any]]] = None,
    booked: bool = False,
    disq_hint: Optional[str] = None,
) -> Dict[str, Any]:
    """Score a finished text screening and apply every consequence of it.

    `booked` is passed by the caller rather than re-read from the candidate,
    because the booking write and this call happen in the same request and the
    caller already knows whether the slot landed.

    Never raises — a screening that cannot be scored must still leave the
    candidate in a sane, visible state rather than a silent one.
    """
    from ai_service import summarize_text_screening

    cand_id, user_id = candidate.get("id"), candidate.get("user_id")
    name = f"{candidate.get('first_name', '')} {candidate.get('last_name', '')}".strip() or "Candidate"

    if transcript is None:
        transcript = transcript_from_chat_log(candidate.get("chat_log") or [])

    role = "the role"
    try:
        if candidate.get("job_id"):
            job = await db.jobs.find_one({"id": candidate["job_id"]}, {"_id": 0}) or {}
            role = job.get("title") or role
    except Exception:
        pass

    try:
        summary = await summarize_text_screening(transcript, name, role)
    except Exception as e:
        logger.warning(f"conclude_text_screening: scoring failed for {cand_id}: {e}")
        summary = {"summary": "", "suitability_score": 50, "key_points": [], "verdict": "borderline"}

    verdict = summary.get("verdict")
    # The screener's own [END:reason] declaration outranks the summariser's
    # re-derivation: one candidate's two-turn "No" to 18-or-over came back from
    # the summariser as "incomplete", so no rejection was written and their
    # queued retry call stayed armed. The agent that ended the conversation knows why
    # it did; only reasons in the fixed vocabulary are honoured.
    if disq_hint in DISQ_LABELS:
        raw_disq = disq_hint
    else:
        raw_disq = summary.get("disqualification_reason")
    disq_label = DISQ_LABELS.get(raw_disq, raw_disq) if raw_disq else None
    complete = verdict and verdict != "incomplete"
    if raw_disq and not complete:
        # A declared gate fail IS a completed screening whatever the
        # summariser thought — give the card a verdict to show.
        verdict = "weak"
        complete = True

    update: Dict[str, Any] = {
        "updated_at": now_iso(),
        "screened_channel": channel,
        # A concluded screening is not an abandoned one. The web chat clears
        # this itself; the SMS path never did, so someone who FINISHED by text
        # without booking got chased with "we didn't quite finish your
        # screening" — on top of the slot-picker outreach this very function
        # sends. The conclusion, wherever it happened, ends the chase.
        "retry_chat_last_at": None,
    }
    if complete:
        # A completed screening clears the booked-unscreened flag — the gate
        # check did its job; routing back to normal appointment concierge.
        update["screening_material"] = None
        update["verdict"] = verdict
        # Reuses `call_summary` on purpose: it is what the drawer, the Kanban
        # tooltip and the verdict card already read. A parallel field would mean
        # every one of those places needing to know which channel screened them.
        update["call_summary"] = summary.get("summary", "")
        update["suitability_score"] = summary.get("suitability_score")

    if booked:
        # A booking already passed the gates and took a real slot — capacity
        # checked, approval sent, reminders scheduled. A later summariser verdict
        # must never flip that to "rejected": it would leave the candidate
        # holding a confirmed interview and a rejection email. Booking wins, and
        # it is checked FIRST so a poisoned or mistaken disqualification can't
        # override it. (See the summariser prompt for the injection defence that
        # makes a poisoned verdict unlikely in the first place.)
        update["screening_status"] = "approved"
        if disq_label:
            # A booked candidate failed a gate in the gate-check. The booking
            # stands (booking wins — see above), but a person must decide what
            # to do with the slot: this is exactly what the bell is for.
            try:
                from notifications_service import create_notification
                await create_notification(
                    user_id, "screening.gate_check_failed",
                    f"🚫 {name} failed a hard gate — but has an interview booked",
                    body=(f"Gate failed: {disq_label}. Their booking was left untouched — "
                          "decide whether the interview should stand."),
                    link=f"/?candidate={cand_id}",
                    candidate_id=cand_id, pipeline_id=candidate.get("pipeline_id"),
                )
            except Exception as e:
                logger.warning(f"gate-check-failed notification failed for {cand_id}: {e}")
        disq_label = None
    elif disq_label:
        # A failed hard gate is a rejection in text exactly as it is on a call.
        update["screening_status"] = "rejected"
        update["disqualification_reason"] = disq_label
        # Stops the dialler: should_skip_call stands down on "rejected" —
        # and next_call_at must go too, or the startup recovery re-arms the
        # call after the next deploy.
        update["auto_dial"] = False
        update["next_call_at"] = None
        # Archive every hard-gate rejection, not just the withdrawals. The voice
        # path has always done this ("failed a hard gate — archive immediately");
        # the text path archived `withdrawn` alone, so someone who answered no to
        # the age or schedule gate was told they were rejected, had their
        # rejection email sent, and then stayed in the Screening column with a red
        # pill on them. Thirteen were sitting there across both offices — five
        # under-18, eight unable to work the hours — the oldest for six days.
        #
        # A gate failure is a finished outcome, not pending review: the decision
        # is made, the comms have gone, and nothing else in the funnel will touch
        # them. Archiving is also what lets them re-apply cleanly later, since
        # `find_duplicate_candidate` skips archived records — which matters most
        # for exactly these two gates, the ones a candidate grows out of.
        update["archived_at"] = now_iso()
        update["archived_reason"] = "withdrawn" if raw_disq == "withdrawn" else "rejected"
    elif complete:
        # Screened fine but didn't land a slot — same state the voice path uses,
        # which is what makes the existing slot-picker sweep pick them up.
        update["screening_status"] = "appointment_pending"
    else:
        update["screening_status"] = "incomplete_info"

    try:
        await db.candidates.update_one({"id": cand_id, "user_id": user_id}, {"$set": update})
    except Exception as e:
        logger.warning(f"conclude_text_screening: could not write outcome for {cand_id}: {e}")
        return {"verdict": verdict, "error": str(e)}

    # ── consequences ────────────────────────────────────────────────────────
    if disq_label:
        try:
            from auto_dialer import cancel_pending_retry_calls
            cancel_pending_retry_calls(cand_id)
        except Exception as e:
            logger.warning(f"conclude_text_screening: could not cancel calls for {cand_id}: {e}")
        # "withdrawn" means they asked not to be contacted — honour that rather
        # than sending one more message telling them they were rejected.
        if raw_disq != "withdrawn":
            try:
                from deps import send_stage_comms
                fresh = await db.candidates.find_one({"id": cand_id}, {"_id": 0}) or candidate
                await send_stage_comms(user_id=user_id, candidate=fresh, template_key="rejection")
            except Exception as e:
                logger.warning(f"conclude_text_screening: rejection comms failed for {cand_id}: {e}")

    if update["screening_status"] == "appointment_pending":
        try:
            from routes.attendance import _fire_slot_picker_outreach
            fresh = await db.candidates.find_one({"id": cand_id}, {"_id": 0}) or candidate
            if not fresh.get("slot_picker_sent_at"):
                await _fire_slot_picker_outreach(user_id, fresh)
                await db.candidates.update_one(
                    {"id": cand_id, "user_id": user_id},
                    {"$set": {"slot_picker_sent_at": now_iso()}},
                )
        except Exception as e:
            logger.warning(f"conclude_text_screening: slot picker failed for {cand_id}: {e}")

    await _notify(db, user_id, cand_id, candidate, name, channel,
                  status=update["screening_status"], verdict=verdict, disq_label=disq_label)

    try:
        from server import broadcast
        await broadcast(user_id)
    except Exception:
        # Live refresh is a nicety; never let it fail the screening.
        pass

    logger.info(
        f"conclude_text_screening: {cand_id} channel={channel} verdict={verdict} "
        f"status={update['screening_status']}" + (f" disq={raw_disq}" if raw_disq else "")
    )
    return {"verdict": verdict, "status": update["screening_status"],
            "disqualification_reason": raw_disq, "summary": summary}


async def _notify(db, user_id, cand_id, candidate, name, channel, *, status, verdict, disq_label):
    """The recruiter-facing alert. Mirrors the four the voice path produces.

    Worth stating plainly: before this, the entire chat path created zero
    notifications, so a recruiter had no way of learning that a screening had
    happened at all unless they happened to refresh the board.
    """
    where = "chat" if channel == "retry" else "text"
    try:
        from notifications_service import create_notification
        if status == "rejected":
            kind, title = "call.completed_negative", f"❌ {name} disqualified by {where} — {disq_label}"
            body = "Hard gate failed during screening. They've been rejected and taken out of the call queue."
        elif status == "approved":
            kind, title = "call.completed_positive", f"✅ {name} passed screening by {where} and booked a slot"
            body = f"Verdict: {verdict}. Interview booked."
        elif status == "appointment_pending":
            kind, title = "call.booking_failed", f"⚠️ {name} passed screening by {where} but didn't book"
            body = f"Verdict: {verdict}. They've been sent a link to pick a time."
        else:
            kind, title = "call.completed_review", f"📋 {name} — {where} screening incomplete"
            body = "They stopped replying partway through. Worth a look."
        await create_notification(
            user_id, kind, title, body=body,
            link=f"/?candidate={cand_id}",
            candidate_id=cand_id, pipeline_id=candidate.get("pipeline_id"),
        )
    except Exception as e:
        logger.warning(f"conclude_text_screening: notification failed for {cand_id}: {e}")


# ─── outcome-on-booking: finish the paperwork behind every door ──────────────

# Strong refs — asyncio holds tasks weakly; a GC'd task here is a silently
# missing verdict (same lesson as the paced SMS reply).
_ENSURE_TASKS: set = set()

# A call whose post-call processing hasn't run yet. Its duration and transcript
# are BOTH written by that step, so neither can be used to judge whether a
# screening happened — the record is a stub, not an answer.
_UNCONCLUDED_CALL_STATUSES = {"initiated", "in_progress", "ringing", "queued"}


async def _recover_missing_transcripts(db, convs: list) -> int:
    """Ask ElevenLabs for the transcripts we never managed to store.

    A screening lives at ElevenLabs until the post-call webhook copies it into
    Mongo, and that copy fails more often than it should: a thin webhook payload
    stamps status + duration with an EMPTY transcript, and the auto-sync sweep
    only rescues calls still marked in_progress/initiated — so a call already
    filed as "no_answer" with nothing in it is never looked at again. Cosmo's
    long screening sat uncopied for days while the caller below told
    the recruiter he had never been screened and texted him the four gates he
    had already answered out loud.

    So before making that claim we go and check. Returns how many conversations
    were recovered; `convs` is mutated in place so the caller can re-merge.
    """
    from transcript_sync import recover_conversation

    recovered = 0
    for v in convs:
        if not v.get("elevenlabs_conversation_id") or (v.get("transcript") or []):
            continue
        if await recover_conversation(db, v):
            recovered += 1
    return recovered


async def ensure_screening_outcome(db, cand_id: str) -> Dict[str, Any]:
    """After ANY booking write, make the record match the conversation.

    Bookings arrive through many doors — the chat, the SMS thread, the portal
    slot grid, the reschedule page, the no-show revival call, a recruiter
    booking by hand — but only the conversation paths wrote a verdict. The
    audit after go-live found five booked candidates with full screenings and
    empty cards, one booked while still marked no_answer, and one booked off
    a revival call that never asked a single gate. This runs after every door:

      1. status → approved (a booked candidate is not "no_answer"),
      2. if there's conversation material and no verdict, run the same
         summariser the chat/SMS paths use over the MERGED transcript
         (calls + chat + SMS — one candidate's material was a revival CALL),
      3. if there's no material at all, stamp it and tell the recruiter —
         a hiring manager should never be the first person to ask the gates.

    Idempotent: an existing verdict is never overwritten, so calling it after
    a booking that already concluded properly is a cheap no-op.
    """
    cand = await db.candidates.find_one({"id": cand_id}, {"_id": 0})
    if not cand or not cand.get("appointment_at"):
        return {"ok": False, "reason": "not_booked"}
    sets: Dict[str, Any] = {}
    if cand.get("screening_status") not in ("approved", "rejected"):
        sets["screening_status"] = "approved"
    out = {"ok": True, "summarized": False, "material_turns": None}
    if not cand.get("verdict"):
        sms_rows = await db.training_sms_messages.find(
            {"candidate_id": cand_id},
            {"_id": 0, "direction": 1, "body": 1, "timestamp": 1},
        ).sort("timestamp", 1).to_list(300)
        convs = await db.conversations.find(
            {"candidate_id": cand_id},
            {"_id": 0, "id": 1, "created_at": 1, "status": 1, "duration_seconds": 1,
             "transcript": 1, "elevenlabs_conversation_id": 1},
        ).sort("created_at", 1).to_list(50)
        # Three situations where judging now would judge a transcript that
        # doesn't exist YET, not a screening that never happened (a candidate
        # booked mid-call and was flagged "never screened" while still ON the
        # call; another's 42-turn call sat unsynced in ElevenLabs):
        #   * a call record created in the last few minutes — possibly live;
        #   * a call still at initiated/in_progress — its post-call step, which
        #     is what writes BOTH the duration and the transcript, hasn't run.
        #     The duration test below cannot see this case: a stub reads 0s
        #     precisely because nothing has concluded it. One candidate's 36-turn
        #     screening was called "never screened" through exactly that hole,
        #     fifteen minutes after they booked a slot on the call;
        #   * a call with real duration, an ElevenLabs id, and no transcript
        #     rows — the sync just hasn't happened. sync_conversation calls
        #     back here after it lands, so deferring loses nothing.
        # The stub case is time-boxed: a call abandoned at "initiated" forever
        # must not silence the alarm forever (the auto-sync sweep concludes
        # genuinely stuck ones inside 30 minutes).
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        _now = _dt.now(_tz.utc)
        _recent = (_now - _td(minutes=10)).isoformat()
        _stub_window = (_now - _td(hours=2)).isoformat()
        transcript_pending = any(
            (str(v.get("created_at") or "") > _recent)
            or ((v.get("status") or "").lower() in _UNCONCLUDED_CALL_STATUSES
                and str(v.get("created_at") or "") > _stub_window)
            or ((v.get("duration_seconds") or 0) >= 60
                and not (v.get("transcript") or [])
                and v.get("elevenlabs_conversation_id"))
            for v in convs
        )
        from transcript_service import merge

        def _material() -> tuple:
            turns = merge(cand.get("chat_log"), sms_rows, convs)
            convo = [
                {"role": "applicant" if t["who"] == "candidate" else "agent", "text": t["text"]}
                for t in turns if t["who"] in ("candidate", "assistant")
            ]
            return convo, sum(1 for t in convo if t["role"] == "applicant")

        convo, n_cand = _material()
        # Last chance before calling someone unscreened: the transcript may
        # exist at ElevenLabs and simply never have been copied here. Ask.
        if n_cand < 2 and not transcript_pending and await _recover_missing_transcripts(db, convs):
            convo, n_cand = _material()
        out["material_turns"] = n_cand
        name = f"{cand.get('first_name', '')} {cand.get('last_name', '')}".strip() or "Candidate"
        if n_cand >= 2:
            job = None
            if cand.get("job_id"):
                job = await db.jobs.find_one({"id": cand["job_id"]}, {"_id": 0, "title": 1})
            from ai_service import summarize_text_screening
            try:
                summary = await summarize_text_screening(convo, name, (job or {}).get("title") or "the role")
                sets.update({
                    "verdict": summary.get("verdict") or "borderline",
                    "suitability_score": summary.get("suitability_score"),
                    "call_summary": summary.get("summary", ""),
                })
                # Material found on a candidate previously stamped "none" —
                # a late transcript, or one we just recovered. Clear the flag
                # or the SMS screener keeps them in gate-check mode and
                # re-asks questions their transcript already answers.
                if cand.get("screening_material") == "none":
                    sets["screening_material"] = None
                out["summarized"] = True
            except Exception as e:
                logger.warning(f"ensure_screening_outcome: summarise failed for {cand_id}: {e}")
        elif transcript_pending:
            out["deferred"] = "transcript_pending"
            logger.info(f"ensure_screening_outcome: {cand_id} deferred — call transcript not synced yet")
        else:
            sets["screening_material"] = "none"
            try:
                from notifications_service import create_notification
                await create_notification(
                    cand["user_id"], "screening.booked_unscreened",
                    f"⚠️ {name} is booked but was never screened",
                    body=("They have an interview scheduled and no screening conversation on any "
                          "channel — no gates asked, nothing to judge. Worth a look before the session."),
                    link=f"/?candidate={cand_id}",
                    candidate_id=cand_id, pipeline_id=cand.get("pipeline_id"),
                )
            except Exception as e:
                logger.warning(f"ensure_screening_outcome: notification failed for {cand_id}: {e}")
    if sets:
        sets["updated_at"] = now_iso()
        await db.candidates.update_one({"id": cand_id}, {"$set": sets})
    if sets.get("screening_material") == "none" and GATE_CHECKS_BY_TEXT:
        cand.update(sets)
        await send_gate_check(db, cand)
    return out


def ensure_screening_outcome_later(db, cand_id: str, delay_seconds: float = 0) -> None:
    """Fire-and-forget wrapper so booking endpoints never wait on the LLM.

    `delay_seconds` is for doors that fire mid-conversation: a voice-agent
    booking lands while the call is still LIVE, and judging then reads an
    unsynced transcript as "never screened". Fifteen minutes lets the call
    end and the post-call sync land first; the verdict guard makes the late
    run a no-op when the post-call processor already did the work."""
    import asyncio

    async def _run():
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        await ensure_screening_outcome(db, cand_id)

    try:
        task = asyncio.create_task(_run())
        _ENSURE_TASKS.add(task)
        task.add_done_callback(_ENSURE_TASKS.discard)
    except Exception as e:
        logger.warning(f"ensure_screening_outcome_later failed to schedule for {cand_id}: {e}")


async def send_gate_check(db, cand: Dict[str, Any]) -> bool:
    """The pre-interview gate check: one text that opens the four hard gates.

    For candidates who are BOOKED with zero screening material — a hiring
    manager should never be the first person to ask whether someone is 18.
    Idempotent via gate_check_sent_at; replies route into the normal SMS
    screener (gate-check mode: questions only, never touches the booking).
    """
    if cand.get("gate_check_sent_at") or not cand.get("phone") or cand.get("sms_opted_out"):
        return False
    first = (cand.get("first_name") or "").strip() or "there"
    body = (
        f"Hi {first}, ahead of your interview we just need four quick checks "
        "so everything's set on our side. First one: are you 18 or over?"
    )
    # The static opener always asks question one — at a candidate who had answered
    # "are you 18 or over?" in the SAME thread two hours earlier. When the
    # candidate has any conversation history, let the screener read it and
    # open at the first UNANSWERED check instead; the static line stays as
    # the fallback for empty threads and LLM failures.
    try:
        from training_sms import _generate_llm_reply, _get_thread
        history = await _get_thread(db, cand["id"], include_chat=True)
        if any(h.get("direction") == "in" for h in history):
            pipeline = await db.pipelines.find_one(
                {"id": cand.get("pipeline_id")}, {"_id": 0}) or {}
            raw = await _generate_llm_reply(
                db, cand, pipeline, history,
                "(Open the pre-interview eligibility check now: one short friendly "
                "message. Acknowledge what they already answered in this thread — "
                "NEVER re-ask an answered question — and ask the first UNANSWERED "
                "hard-gate question. If all four are already answered in the thread, "
                "just confirm everything's set for their interview.)",
                "follow_up",
            )
            if raw:
                import re as _re
                cleaned = _re.sub(r"\[[A-Z_]+[^\]]*\]", "", raw).replace("**", "").strip()
                if cleaned:
                    body = cleaned
    except Exception as e:
        logger.warning(f"gate check thread-aware opener failed for {cand.get('id')}: {e}")
    try:
        from deps import resolve_settings
        from sms_service import send_candidate_sms
        settings = await resolve_settings(cand["user_id"], cand.get("pipeline_id"))
        res = await send_candidate_sms(db, settings, cand, body, template_key="gate_check")
        if res.get("status") not in ("sent",):
            logger.warning(f"gate check not delivered for {cand.get('id')}: {res}")
            return False
        # Into the SMS thread, not just the communications log: the screener
        # reads training_sms_messages, and a question it cannot see is a
        # question it will repeat — or worse, a "Yes" it has no context for.
        # was_llm_reply=True so the yes-branch treats the reply as an answer
        # to the assistant, not a confirmation of the existing slot.
        from training_sms import _log_message
        await _log_message(
            db, candidate_id=cand["id"], pipeline_id=cand.get("pipeline_id"),
            direction="out", body=res.get("body") or body,
            sender=res.get("from_number"), recipient=cand.get("phone"),
            was_llm_reply=True,
        )
        await db.candidates.update_one(
            {"id": cand["id"]}, {"$set": {"gate_check_sent_at": now_iso(), "updated_at": now_iso()}})
        logger.info(f"gate check sent to {cand.get('id')}")
        return True
    except Exception as e:
        logger.warning(f"gate check failed for {cand.get('id')}: {e}")
        return False
