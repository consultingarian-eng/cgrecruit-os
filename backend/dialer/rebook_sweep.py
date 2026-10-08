"""Re-notify watchlist sweep — runs hourly to email candidates who clicked
'None of these work' on the reschedule portal after their configured retry delay."""
import logging
from datetime import datetime, timedelta
from typing import Any, Dict

from . import state
from .state import now_utc, settings_for

logger = logging.getLogger("auto_dialer")


async def rebook_watchlist_sweep() -> Dict[str, Any]:
    """Find candidates who clicked 'None of these work' on the reschedule
    portal and haven't been re-notified yet → fire the appointment_no_show
    email/SMS again with the latest slot list.

    The retry delay is read from each candidate's pipeline booking_preferences
    (reschedule_retry_days, default 3)."""
    if state._db is None:
        return {"checked": 0, "notified": 0}
    db = state._db
    # Use a conservative cutoff (1 day) — we'll per-candidate check below.
    conservative_cutoff = (now_utc() - timedelta(days=1)).isoformat()
    # NOTE: deliberately no `appointment_at: None` filter. Marking a no-show and
    # texting "can't make it" both leave the OLD appointment_at on the record,
    # so that filter excluded exactly the no-shows and cancellers the watchlist
    # exists to recover — their "we'll email you in 3 days" promise could never
    # fire. "Already booked again" is checked per-candidate below instead.
    candidates = await db.candidates.find(
        {
            "rebook_watchlist": True,
            "rebook_watchlist_notified_at": None,
            "rebook_watchlist_at": {"$lt": conservative_cutoff},
        },
        {"_id": 0},
    ).to_list(500)
    notified = 0
    for cand in candidates:
        try:
            # Moved on = holds a FUTURE, un-cancelled appointment. A past slot
            # is a no-show's history; a cancelled future slot is a cancellation
            # — both still want fresh times.
            appt_at = cand.get("appointment_at")
            if appt_at and not cand.get("appointment_cancelled_at"):
                try:
                    appt_dt = datetime.fromisoformat(str(appt_at).replace("Z", "+00:00"))
                    if appt_dt.tzinfo is None:
                        from datetime import timezone as _tz
                        appt_dt = appt_dt.replace(tzinfo=_tz.utc)
                    if appt_dt > now_utc():
                        continue  # genuinely rebooked — nothing to recover
                except Exception:
                    pass
            # Per-candidate retry threshold from their pipeline's booking_preferences.
            settings = await settings_for(cand["user_id"], cand.get("pipeline_id"))
            retry_days = int((settings.get("booking_preferences") or {}).get("reschedule_retry_days") or 3)
            watchlist_at_str = cand.get("rebook_watchlist_at") or ""
            if watchlist_at_str:
                watchlist_at = datetime.fromisoformat(watchlist_at_str.replace("Z", "+00:00"))
                if now_utc() < watchlist_at + timedelta(days=retry_days):
                    continue  # not yet due for this candidate
            # Never send the link while the grid it lands on is empty — retry
            # next hour instead of burning the one-shot on a dead page.
            pipeline = await db.pipelines.find_one({"id": cand.get("pipeline_id")}, {"_id": 0}) or {}
            from routes.public import window_has_slots
            if not await window_has_slots(pipeline, settings):
                logger.info(f"watchlist re-notify deferred for {cand.get('id')}: no slots in window yet")
                continue
            # Template by history — the old hardcoded appointment_no_show meant
            # screened-but-never-booked candidates were told "we missed you for
            # your interview" about an interview that never existed.
            if cand.get("no_show_at") or cand.get("attendance_status") == "no_show" or any(
                (h or {}).get("status") == "no_show" for h in (cand.get("attendance_history") or [])
            ):
                tpl_key = "appointment_no_show"
            elif cand.get("appointment_cancelled_at"):
                tpl_key = "appointment_cancelled_rebook"
            else:
                tpl_key = "slot_picker"
            from deps import send_stage_email
            await send_stage_email(cand["user_id"], cand, tpl_key)
            if cand.get("phone"):
                from sms_service import send_candidate_sms
                from email_service import build_template_vars, render_template, get_template_for_key
                tpl = get_template_for_key(settings, tpl_key) or {}
                if tpl.get("sms_enabled") is False or tpl.get("enabled") is False:
                    pass  # SMS channel off — skip
                elif tpl.get("sms_body"):
                    job = None
                    if cand.get("job_id"):
                        job = await db.jobs.find_one({"id": cand["job_id"]}, {"_id": 0})
                    body = render_template(tpl["sms_body"], build_template_vars(cand, settings, job))
                    await send_candidate_sms(db, settings, cand, body, template_key=tpl_key)
            await db.candidates.update_one(
                {"id": cand["id"]},
                {"$set": {"rebook_watchlist_notified_at": now_utc().isoformat()}},
            )
            notified += 1
        except Exception as e:
            logger.warning(f"watchlist re-notify failed for {cand.get('id')}: {e}")
    logger.info(f"rebook watchlist sweep: checked {len(candidates)}, notified {notified}")
    return {"checked": len(candidates), "notified": notified}


def schedule_rebook_watchlist_sweep() -> None:
    """Hourly cron — picks up watchlist candidates who hit the 3-day mark."""
    if state._scheduler is None:
        return
    try:
        state._scheduler.add_job(
            rebook_watchlist_sweep,
            "interval",
            hours=1,
            id="rebook-watchlist-sweep",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        logger.info("rebook watchlist sweep scheduled (hourly)")
    except Exception as e:
        logger.warning(f"rebook watchlist sweep schedule failed: {e}")


async def unbooked_screened_sweep() -> Dict[str, Any]:
    """Find candidates who completed screening but never locked in a slot and
    send them the slot-picker link (/reschedule/{token}).

    Catches every way a screened candidate can leak out of booking:
      • appointment_pending whose webhook-time outreach failed or predates the
        slot_picker_sent_at marker,
      • appointment_pending set by the auto-sync sweep (which classifies but
        doesn't send outreach),
      • approved/strong/review candidates parked in SCREENING with no
        appointment (e.g. calls cut short by a voicemail_detection misfire
        mid-booking, or legacy statuses from before appointment_pending existed).

    One send per candidate (slot_picker_sent_at guard); if they click 'None of
    these work' the rebook watchlist takes over the follow-ups. Window-limited
    to 14 days so months-old leads aren't messaged out of the blue."""
    if state._db is None:
        return {"checked": 0, "sent": 0}
    db = state._db
    now_iso = now_utc().isoformat()
    cutoff = (now_utc() - timedelta(days=14)).isoformat()
    candidates = await db.candidates.find(
        {
            "stage": "SCREENING",
            "screening_status": {"$in": ["appointment_pending", "approved", "strong", "review"]},
            "appointment_at": None,
            "archived_at": None,
            "slot_picker_sent_at": None,
            "updated_at": {"$gte": cutoff},
        },
        {"_id": 0},
    ).to_list(500)
    sent = 0
    for cand in candidates:
        try:
            from routes.attendance import _fire_slot_picker_outreach
            res = await _fire_slot_picker_outreach(cand["user_id"], cand)
            await db.candidates.update_one(
                {"id": cand["id"]},
                {"$set": {
                    "slot_picker_sent_at": now_iso,
                    "screening_status": "appointment_pending",
                    "updated_at": now_iso,
                }},
            )
            sent += 1
            logger.info(
                f"unbooked-screened sweep: slot-picker sent to "
                f"{cand.get('first_name')} {cand.get('last_name')} ({cand['id']}): {res}"
            )
        except Exception as e:
            logger.warning(f"unbooked-screened sweep failed for {cand.get('id')}: {e}")
    if candidates:
        logger.info(f"unbooked-screened sweep: checked {len(candidates)}, sent {sent}")
    return {"checked": len(candidates), "sent": sent}


def schedule_unbooked_screened_sweep() -> None:
    """Hourly cron, with a first run 2 minutes after startup so a deploy
    immediately drains any screened-but-unbooked backlog."""
    if state._scheduler is None:
        return
    try:
        state._scheduler.add_job(
            unbooked_screened_sweep,
            "interval",
            hours=1,
            id="unbooked-screened-sweep",
            replace_existing=True,
            misfire_grace_time=3600,
            max_instances=1,
            next_run_time=now_utc() + timedelta(minutes=2),
        )
        logger.info("unbooked-screened sweep scheduled (hourly, first run in 2 min)")
    except Exception as e:
        logger.warning(f"unbooked-screened sweep schedule failed: {e}")
