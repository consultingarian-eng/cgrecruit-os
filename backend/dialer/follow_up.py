"""Post-call follow-up: pull transcript from ElevenLabs, summarise, decide retry."""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from . import state
from .state import now_utc, settings_for
from .window import parse_next_call_at
from app_tz import app_zone, default_tz_name  # APP_TIMEZONE (noqa: F401)

logger = logging.getLogger("auto_dialer")


def sched_call_followup(user_id: str, candidate_id: str, run_at: datetime) -> str:
    if state._scheduler is None:
        raise RuntimeError("scheduler not started")
    job_id = f"followup:{user_id}:{candidate_id}:{int(run_at.timestamp())}"
    state._scheduler.add_job(
        followup_after_call,
        "date",
        run_date=run_at,
        args=[user_id, candidate_id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=600,
    )
    return job_id


def _normalise_transcript(raw: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """ElevenLabs returns transcripts with both `message` and `text` keys depending
    on the agent version — flatten to one shape."""
    return [
        {"role": x.get("role", ""), "text": x.get("message", "") or x.get("text", "")}
        for x in raw
    ]


def _classify_verdict(is_complete: bool, summary: Dict[str, Any], has_appointment: bool = False) -> str:
    """Decide a FINAL screening status. Never leaves the candidate in_progress
    after a complete call — recruiters need a definitive state for filtering.

    Delegates to the shared ladder so this +8-minute net can never disagree
    with the webhook that usually ran first: it used to file "weak" as
    rejected and "strong" as approved-without-a-slot while the webhook sent
    the same person a slot-picker — status flip-flopping under the retry
    cadence."""
    from call_classification import screening_status_after_call
    return screening_status_after_call(
        is_complete=is_complete,
        verdict=summary.get("verdict"),
        disq_reason=summary.get("disqualification_reason"),
        has_appointment=has_appointment,
    )


def _build_candidate_update(
    cand: Dict[str, Any],
    new_status: str,
    final_status: str,
    summary: Dict[str, Any],
    is_complete: bool,
) -> Dict[str, Any]:
    """Compose the $set patch for the candidate doc."""
    update: Dict[str, Any] = {
        "last_call_status": new_status,
        "screening_status": final_status,
        "verdict": summary.get("verdict") if is_complete else cand.get("verdict"),
        "call_summary": summary.get("summary") if is_complete else cand.get("call_summary"),
        "updated_at": now_utc().isoformat(),
    }
    if is_complete and summary.get("suitability_score") is not None:
        update["smart_score"] = max(
            cand.get("smart_score") or 0,
            int(summary.get("suitability_score")),
        )
    if is_complete:
        # The phone genuinely reached them — the conversation moved channels,
        # so the abandoned-text-chase sequence is over. Cleared HERE, on a
        # connected call, never at dial time: an unanswered ring must not kill
        # the 2h/6h/24h chase of someone who was answering by text an hour ago.
        update["retry_chat_last_at"] = None
    if final_status == "rejected":
        # Same archive stamping as every other rejection path — unarchived
        # rejections wear a red pill forever and block their own re-application.
        from call_classification import DISQ_LABELS
        raw = summary.get("disqualification_reason")
        update["disqualification_reason"] = DISQ_LABELS.get(raw, raw)
        update["archived_at"] = now_utc().isoformat()
        update["archived_reason"] = "withdrawn" if raw == "withdrawn" else "rejected"
    return update


async def _maybe_schedule_retry(
    cand: Dict[str, Any],
    candidate_id: str,
    user_id: str,
    final_status: str,
    settings: Dict[str, Any],
) -> Tuple[bool, Optional[str]]:
    """Schedule a retry call if the candidate landed in a recoverable state.
    Returns (needs_retry, next_call_at_iso). Caller is responsible for storing
    next_call_at on the candidate doc.

    `_place_call_now` has its own appointment-booked guard, but checking here
    too avoids scheduling a useless APScheduler job that would just no-op."""
    needs_retry = final_status in ("no_answer", "didnt_connect", "incomplete_info")
    if not needs_retry:
        return False, None
    ad = (settings.get("auto_dialer") or {})
    attempts = cand.get("call_attempts") or 0
    max_attempts = int(ad.get("max_retry_attempts", 3))
    retry_delays = ad.get("retry_delays") or []
    idx = max(0, attempts - 1)  # attempts already incremented; idx 0 = after 1st call
    if retry_delays and idx < len(retry_delays):
        retry_hours = int(retry_delays[idx])
    else:
        retry_hours = int(ad.get("retry_delay_hours", 2))
    already_booked = bool(cand.get("appointment_at"))
    if already_booked or attempts >= max_attempts or not ad.get("enabled", True):
        return True, None
    # Idempotency, same guard as retry.maybe_schedule_incomplete_retry: the
    # post-call webhook has almost always booked this exact retry minutes ago.
    # Without it every follow-up cancels that job and re-books the dial another
    # sync_after_call_minutes out, walking the recruiter's ETA backwards.
    existing_at = cand.get("next_call_at")
    if existing_at:
        tz_name = (settings.get("region_language") or {}).get("timezone") or default_tz_name()
        existing = parse_next_call_at(existing_at, tz_name)
        if existing and existing > now_utc():
            return True, existing_at
    # Lazy import — see place_call.py docstring re: 3-way dialer circulars.
    from .queue import schedule_call_with_window
    sched = await schedule_call_with_window(
        user_id, candidate_id, base_delay_minutes=retry_hours * 60,
    )
    next_at = sched.get("scheduled_at")
    logger.info(f"retry {attempts + 1}/{max_attempts} scheduled for {candidate_id} at {next_at}")
    return True, next_at


async def _fire_retry_message(user_id: str, candidate_id: str) -> None:
    """Fire the email/SMS retry message via the deps helper. Wrapped so the
    main coroutine has a clean control-flow."""
    try:
        from deps import maybe_fire_screening_retry
        res = await maybe_fire_screening_retry(user_id, candidate_id)
        logger.info(f"sync screening retry msg: {res}")
    except Exception as e:
        logger.warning(f"sync screening retry trigger failed: {e}")


async def followup_after_call(user_id: str, candidate_id: str) -> None:
    """Pull conversation from ElevenLabs, summarize, and decide if a retry is needed."""
    if state._db is None:
        return
    db = state._db
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user_id}, {"_id": 0})
    if not cand:
        return
    # The recruiter paused this candidate after the call was placed. A follow-up
    # job that outlived the pause must not stamp a fresh status or re-queue a
    # dial — /queue/resume is the only way back into the queue.
    if cand.get("screening_status") == "paused":
        logger.info(f"followup_after_call: {candidate_id} is paused — skipping")
        return
    settings = await settings_for(user_id, cand.get("pipeline_id"))
    conv = await db.conversations.find_one(
        {"candidate_id": candidate_id, "user_id": user_id},
        {"_id": 0}, sort=[("created_at", -1)],
    )
    if not conv or not conv.get("elevenlabs_conversation_id"):
        return

    from voice_service import fetch_elevenlabs_conversation
    from ai_service import summarize_call_transcript

    data = fetch_elevenlabs_conversation(conv["elevenlabs_conversation_id"])
    if not data or data.get("error"):
        logger.warning(f"sync fetch err for {candidate_id}: {data}")
        return

    transcript = _normalise_transcript(data.get("transcript") or [])
    duration = (data.get("metadata") or {}).get("call_duration_secs")
    job = None
    if cand.get("job_id"):
        job = await db.jobs.find_one({"id": cand["job_id"], "user_id": user_id}, {"_id": 0})
    summary = await summarize_call_transcript(
        transcript, cand.get("first_name", ""), (job or {}).get("title", ""),
        duration_seconds=duration,
    )

    # Shared definition — this net used to count ANY transcript over 30s as
    # complete, including agent-only transcripts (the agent talking to a dead
    # line) and voicemails, then flip the webhook's classification underneath
    # the retry cadence.
    from call_classification import is_complete_call, resolve_voicemail
    _spoke = any(t.get("role") in ("user", "human") for t in transcript)
    _vm, _ = resolve_voicemail(False, transcript)
    is_complete = is_complete_call(_spoke, duration, _vm)
    new_status = "completed" if is_complete else "no_answer"
    final_status = _classify_verdict(is_complete, summary, has_appointment=bool(cand.get("appointment_at")))

    # Persist conversation outcome.
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

    # The candidate may have withdrawn or been rejected in the minutes between
    # the call and this follow-up landing — a candidate texted "I am not
    # interested", was archived, and this function then overwrote that
    # rejection with "no_answer" and scheduled a retry. The call record
    # above is still written (it happened); the candidate's fate is not ours
    # to decide any more.
    fresh = await db.candidates.find_one(
        {"id": candidate_id, "user_id": user_id},
        {"_id": 0, "archived_at": 1, "screening_status": 1, "verdict": 1,
         "disqualification_reason": 1},
    ) or {}
    if (fresh.get("archived_at") or fresh.get("verdict") == "withdrawn"
            or fresh.get("screening_status") in ("rejected", "paused")
            or fresh.get("disqualification_reason")):
        logger.info(
            f"followup_after_call: {candidate_id} withdrew / was rejected / was paused "
            "since the call — call record saved, candidate state left alone, no retry."
        )
        return

    cand_update = _build_candidate_update(cand, new_status, final_status, summary, is_complete)

    needs_retry, next_at = await _maybe_schedule_retry(
        cand, candidate_id, user_id, final_status, settings,
    )
    if next_at:
        # schedule_call_with_window already moved the candidate to 'queued'.
        # Writing our stale final_status over it drops them out of every Call
        # Queue bucket while the dial stays armed and invisible to the recruiter.
        cand_update.pop("screening_status", None)
        cand_update["next_call_at"] = next_at
    elif not needs_retry and cand.get("next_call_at"):
        # Reclassified out of a retryable state (e.g. 'review'), but the call job
        # the webhook armed is still live and nothing else ever cancels it.
        from .retry import cancel_pending_retry_calls
        cancel_pending_retry_calls(candidate_id)
        cand_update["next_call_at"] = None

    await db.candidates.update_one(
        {"id": candidate_id, "user_id": user_id},
        {"$set": cand_update},
    )

    # A gate-fail filed HERE (twiml_bridge mode, where no webhook runs) must
    # still tell the candidate — they were thanked on the phone and would
    # otherwise never hear the outcome. The early-return above already
    # guarantees we are the first to file it, so this can't double-send.
    if (
        final_status == "rejected"
        and (summary.get("disqualification_reason") or "") != "withdrawn"
    ):
        try:
            from deps import send_stage_comms
            refreshed = await db.candidates.find_one({"id": candidate_id}, {"_id": 0}) or cand
            await send_stage_comms(user_id=user_id, candidate=refreshed, template_key="rejection")
        except Exception as e:
            logger.warning(f"followup rejection comms failed for {candidate_id}: {e}")

    # After persisting the final state, fire the email/SMS retry message so
    # the candidate gets the link to finish online. This path handles the
    # twiml_bridge sync flow; the ElevenLabs post-call webhook fires its own copy.
    if needs_retry:
        await _fire_retry_message(user_id, candidate_id)
