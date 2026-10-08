"""Delayed auto-promote on screening success (v32).

When the AI screening call lands a positive verdict AND a slot was booked, we
DON'T promote the candidate to APPOINTMENT immediately — instead we schedule a
delayed job so the recruiter has a window (default: 2 hours) to override
(Approve early / Deny / change slot / etc.) before the auto-promotion fires.
Recruiter actions (move, deny, reset) cancel the pending job; the cron only
fires if no human intervened during the window.
"""
import asyncio
import logging
from datetime import timedelta
from typing import Any, Dict, Optional

from . import state
from .state import now_utc, settings_for

logger = logging.getLogger("auto_dialer")

AUTO_PROMOTE_DELAY_MINUTES = 120  # 2 hours


def _auto_promote_job_id(candidate_id: str) -> str:
    return f"auto-promote:{candidate_id}"


async def schedule_auto_promote(user_id: str, candidate_id: str, delay_minutes: Optional[int] = None) -> Dict[str, Any]:
    """Schedule a delayed promotion to APPOINTMENT for a candidate that just had
    a successful screening call. Stores `auto_promote_at` on the candidate so
    the FE can render a countdown. Replaces any existing pending job.

    Delay is read from the candidate's per-pipeline `screen_call_agent.auto_promote_delay_minutes`
    (default 120). Pass `delay_minutes` to override (used by tests)."""
    if state._db is None or state._scheduler is None:
        return {"status": "failed", "error": "scheduler not ready"}
    if delay_minutes is None:
        cand = await state._db.candidates.find_one({"id": candidate_id, "user_id": user_id}, {"_id": 0}) or {}
        settings = await settings_for(user_id, cand.get("pipeline_id"))
        sca = (settings.get("screen_call_agent") or {})
        try:
            delay_minutes = int(sca.get("auto_promote_delay_minutes") or AUTO_PROMOTE_DELAY_MINUTES)
        except (TypeError, ValueError):
            delay_minutes = AUTO_PROMOTE_DELAY_MINUTES
        if delay_minutes <= 0:
            delay_minutes = AUTO_PROMOTE_DELAY_MINUTES
    run_at = now_utc() + timedelta(minutes=delay_minutes)
    job_id = _auto_promote_job_id(candidate_id)
    try:
        state._scheduler.remove_job(job_id)
    except Exception:
        pass
    state._scheduler.add_job(
        fire_auto_promote,
        "date",
        run_date=run_at,
        id=job_id,
        kwargs={"user_id": user_id, "candidate_id": candidate_id},
        replace_existing=True,
    )
    await state._db.candidates.update_one(
        {"id": candidate_id, "user_id": user_id},
        {"$set": {
            "auto_promote_at": run_at.isoformat(),
            "auto_promote_status": "pending",
            "updated_at": now_utc().isoformat(),
        }},
    )
    logger.info(f"auto-promote scheduled for {candidate_id} at {run_at.isoformat()}")
    return {"status": "scheduled", "scheduled_at": run_at.isoformat()}


def cancel_auto_promote(candidate_id: str, reason: str = "manual") -> bool:
    """Cancel a pending auto-promote job. Called whenever a recruiter manually
    intervenes (Approve / Deny / move stage / reset). Best-effort — silently
    no-ops if no job exists."""
    if state._scheduler is None:
        return False
    try:
        state._scheduler.remove_job(_auto_promote_job_id(candidate_id))
    except Exception:
        return False
    if state._db is not None:
        try:
            asyncio.create_task(state._db.candidates.update_one(
                {"id": candidate_id},
                {"$set": {
                    "auto_promote_at": None,
                    "auto_promote_status": f"cancelled:{reason}",
                    "updated_at": now_utc().isoformat(),
                }},
            ))
        except Exception:
            pass
    logger.info(f"auto-promote cancelled for {candidate_id} (reason={reason})")
    return True


async def fire_auto_promote(user_id: str, candidate_id: str) -> Dict[str, Any]:
    """Cron callback: actually promote the candidate to APPOINTMENT and fire the
    approval email. Skips silently if the recruiter already moved them."""
    if state._db is None:
        return {"status": "failed"}
    cand = await state._db.candidates.find_one({"id": candidate_id, "user_id": user_id}, {"_id": 0}) or {}
    if not cand:
        return {"status": "failed", "reason": "candidate not found"}
    # Re-validate preconditions: stage still upstream, slot still set, recruiter
    # didn't already act.
    if cand.get("stage") not in ("APPLICANT", "SCREENING"):
        return {"status": "skipped", "reason": f"stage already {cand.get('stage')}"}
    if not cand.get("appointment_at"):
        return {"status": "skipped", "reason": "no appointment_at — recruiter cleared slot"}
    if (cand.get("verdict") or "").lower() not in ("strong", "good"):
        return {"status": "skipped", "reason": f"verdict={cand.get('verdict')}"}

    await state._db.candidates.update_one(
        {"id": candidate_id, "user_id": user_id},
        {"$set": {
            "stage": "APPOINTMENT",
            "screening_status": "approved",
            "auto_promote_at": None,
            "auto_promote_status": "fired",
            "updated_at": now_utc().isoformat(),
        }},
    )
    # Fire the approval email + SMS, identical to a manual booking confirmation.
    try:
        from deps import send_stage_comms
        refreshed = await state._db.candidates.find_one({"id": candidate_id, "user_id": user_id}, {"_id": 0}) or {}
        res = await send_stage_comms(user_id, refreshed, "approval")
        logger.info(f"auto-promote fired approval comms for {candidate_id}: email={res['email'].get('status')} sms={res['sms'].get('status')}")
    except Exception as e:
        logger.warning(f"auto-promote approval comms failed: {e}")
    return {"status": "fired", "candidate_id": candidate_id}
