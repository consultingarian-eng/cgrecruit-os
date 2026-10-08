"""Chasing candidates who went quiet part-way through the text screening.

This used to nudge once, ever, and never re-arm. That was survivable while the
AI phone call was the real screening; with the call reduced to a ping it is the
only follow-up there is, so someone who answered two questions and put their
phone down was simply lost.

Now it runs the sequence in nudge_schedule — 2h, 6h then 24h after their last
message, held out of the small hours — and stops after three. The timing lives
there, not here, so it can be tested without a database or a clock.

Distinct from `rebook_sweep.py` (appointment-lifecycle re-notifies) — this covers
the pre-appointment screening funnel."""
import logging
from datetime import timedelta
from typing import Any, Dict

from . import state
from .state import now_utc, settings_for

logger = logging.getLogger("auto_dialer")


async def retry_chat_nudge_sweep() -> Dict[str, Any]:
    """Find candidates with a stale in-progress retry chat (last applicant
    message 2h-3d old, chat not concluded via [END]/booking, not yet nudged)
    and send the retry_chat_nudge email/SMS once.

    `retry_chat_last_at` is cleared to None by `routes/retry.py: retry_chat_turn`
    whenever the chat legitimately concludes (booked or [END]), so this query
    naturally excludes finished conversations without needing a separate
    'completed' flag."""
    if state._db is None:
        return {"checked": 0, "notified": 0}
    db = state._db
    from models import parse_appointment_at
    from nudge_schedule import GIVE_UP_AFTER_DAYS, describe, should_nudge_now

    now = now_utc()
    lower_cutoff = (now - timedelta(days=GIVE_UP_AFTER_DAYS)).isoformat()
    # Fetches everything still in the window and lets should_nudge_now decide,
    # rather than encoding the 2h/6h/24h steps in the query. One place owns the
    # timing. Paused threads are excluded — someone whose replies were stopped
    # for abuse should not then be chased three more times.
    candidates = await db.candidates.find(
        {
            "retry_chat_last_at": {"$ne": None, "$gte": lower_cutoff},
            "appointment_at": None,
            "screening_status": {"$nin": ["approved", "rejected"]},
            "sms_replies_paused": {"$ne": True},
        },
        {"_id": 0},
    ).to_list(500)
    notified = 0
    for cand in candidates:
        try:
            last = parse_appointment_at(cand.get("retry_chat_last_at"))
            sent = int(cand.get("retry_chat_nudge_count") or 0)
            settings_doc = await settings_for(cand["user_id"], cand.get("pipeline_id"))
            tz_name = (settings_doc.get("region_language") or {}).get("timezone")
            if not last or not should_nudge_now(last, sent, now, tz_name):
                continue
            from deps import send_stage_email
            await send_stage_email(cand["user_id"], cand, "retry_chat_nudge")
            if cand.get("phone"):
                from sms_service import send_candidate_sms
                from email_service import build_template_vars, render_template, get_template_for_key
                settings = await settings_for(cand["user_id"], cand.get("pipeline_id"))
                tpl = get_template_for_key(settings, "retry_chat_nudge") or {}
                if tpl.get("sms_enabled") is False or tpl.get("enabled") is False:
                    pass  # SMS channel off — skip
                else:
                    # Someone screening BY TEXT gets nudged IN the text thread —
                    # "just reply here" — not a link that bounces them to the web
                    # mid-conversation. Replying resumes the screener where it
                    # left off (the SMS agent reads the thread). The link
                    # version stays for web-chat starters, where the page IS
                    # their thread; the email carries the link either way.
                    stalled_in_sms = await db.training_sms_messages.count_documents(
                        {"candidate_id": cand["id"], "direction": "in"}
                    ) > 0
                    if stalled_in_sms:
                        first = (cand.get("first_name") or "").strip() or "there"
                        body = (
                            f"Hi {first}, we didn't quite finish your screening - "
                            "just reply here and we'll pick up right where we left off."
                        )
                    elif tpl.get("sms_body"):
                        job = None
                        if cand.get("job_id"):
                            job = await db.jobs.find_one({"id": cand["job_id"]}, {"_id": 0})
                        body = render_template(tpl["sms_body"], build_template_vars(cand, settings, job))
                    else:
                        body = ""
                    if body:
                        await send_candidate_sms(db, settings, cand, body, template_key="retry_chat_nudge")
            await db.candidates.update_one(
                {"id": cand["id"]},
                {"$set": {"retry_chat_nudge_sent_at": now.isoformat()},
                 "$inc": {"retry_chat_nudge_count": 1}},
            )
            logger.info("retry-chat nudge for %s: %s", cand.get("id"), describe(sent))
            notified += 1
        except Exception as e:
            logger.warning(f"retry-chat nudge failed for {cand.get('id')}: {e}")
    if candidates:
        logger.info(f"retry-chat nudge sweep: checked {len(candidates)}, notified {notified}")
    return {"checked": len(candidates), "notified": notified}


def schedule_retry_chat_nudge_sweep() -> None:
    """Every 30 minutes, first run 2 minutes after startup so a deploy drains any
    backlog. Runs more often than the shortest gap in the sequence so a nudge
    lands near its due time rather than up to an hour late."""
    if state._scheduler is None:
        return
    try:
        state._scheduler.add_job(
            retry_chat_nudge_sweep,
            "interval",
            minutes=30,
            id="retry-chat-nudge-sweep",
            replace_existing=True,
            misfire_grace_time=3600,
            max_instances=1,
            next_run_time=now_utc() + timedelta(minutes=2),
        )
        logger.info("retry-chat nudge sweep scheduled (every 30 min, first run in 2 min)")
    except Exception as e:
        logger.warning(f"retry-chat nudge sweep schedule failed: {e}")
