"""Pre-call warmup SMS — fired `pre_call_sms_minutes` before the AI dial."""
import logging
from datetime import datetime
from typing import Any, Dict

from . import state
from .state import settings_for

logger = logging.getLogger("auto_dialer")


def sched_pre_call_sms(user_id: str, candidate_id: str, run_at: datetime) -> str:
    if state._scheduler is None:
        raise RuntimeError("scheduler not started")
    job_id = f"sms:{user_id}:{candidate_id}:{int(run_at.timestamp())}"
    state._scheduler.add_job(
        send_pre_call_sms,
        "date",
        run_date=run_at,
        args=[user_id, candidate_id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=300,
    )
    logger.info(f"scheduled pre-call SMS {job_id} for {run_at.isoformat()}")
    return job_id


async def send_pre_call_sms(user_id: str, candidate_id: str) -> Dict[str, Any]:
    """Send the warmup SMS template to the candidate before the AI dial."""
    if state._db is None:
        return {"status": "failed", "error": "db not initialized"}
    db = state._db
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user_id}, {"_id": 0})
    if not cand:
        return {"status": "failed", "error": "candidate not found"}
    if not cand.get("phone"):
        return {"status": "skipped", "reason": "no phone"}
    if not cand.get("auto_dial", True):
        return {"status": "skipped", "reason": "auto_dial disabled"}
    if (cand.get("call_attempts") or 0) > 0:
        return {"status": "skipped", "reason": "warmup only sent before first call"}
    settings = await settings_for(user_id, cand.get("pipeline_id"))
    job = None
    if cand.get("job_id"):
        job = await db.jobs.find_one({"id": cand["job_id"], "user_id": user_id}, {"_id": 0})

    from email_service import render_template, build_template_vars, get_template_for_key
    from sms_service import send_candidate_sms
    tpl = get_template_for_key(settings, "warmup")
    if tpl.get("sms_enabled") is False or tpl.get("enabled") is False:
        return {"status": "skipped", "reason": "warmup SMS channel is off"}
    sms_body = (tpl or {}).get("sms_body") or ""
    if not sms_body:
        return {"status": "skipped", "reason": "no warmup sms_body template"}
    vars_map = build_template_vars(cand, settings, job)
    # The template says "Expect a call in about [Call Delay Minutes] min", and
    # that var renders warmup_delay_minutes (10) — but this SMS fires
    # pre_call_sms_minutes (3) before the dial. Someone who planned around
    # "10 minutes" screened out the unknown number at minute 3. For THIS send,
    # the honest value is the actual lead time.
    _lead = int(((settings.get("auto_dialer") or {}).get("pre_call_sms_minutes") or 3))
    vars_map["Call Delay Minutes"] = str(max(1, _lead))
    body = render_template(sms_body, vars_map)
    res = await send_candidate_sms(db, settings, cand, body, template_key="warmup_pre_call")
    logger.info(f"pre-call SMS to {cand['phone']}: {res.get('status')}")
    return res
