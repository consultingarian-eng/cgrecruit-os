"""Admin alerts pushed to CG1 (the field app) — the "a human actually hears"
transport.

CGRecruit's own bell is deliberately quiet: kinds outside
notifications_service.ACTIONABLE_KINDS never ring locally, because a recruiter
bell that announces the routine gets ignored. But the office admins who run
training mornings live in CG1, not in this dashboard — a starter who texts
that they are lost on the way in gets the address from the concierge, but
without this no human is ever pinged. This module is the missing pipe: anything starter-critical POSTs to CG1's admin-alert webhook,
which pushes it to that office's admins' phones.

Contract (implemented identically on the CG1 side):
    POST {CG1_BACKEND_URL}/api/webhooks/cgrecruit/admin-alert
    Header: x-webhook-secret = CG1_WEBHOOK_SECRET
    Body:   {kind, title, body, office_key, candidate_id, candidate_name,
             link, occurred_at}

Fire-and-forget by design: nothing here ever raises into a caller, and the
spawn helpers never block — a slow or dead CG1 must never delay a Twilio
webhook or a sweep. Failures are a warning in the log, full stop.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

CG1_ALERT_TIMEOUT = 8

# Notification kinds (see notifications_service.create_notification) that ALSO
# forward to CG1 admins. Empty since 2026-08-18 by the owner's rule: CG1 admins
# hear ONLY genuine questions/correspondence from TRAINING-stage new hires —
# never status flags (withdrawals, cancellations, reschedules, pauses). Those
# emitters page directly from the SMS/email inbound handlers; nothing that
# flows through the local bell forwards any more. The hook stays wired so a
# future kind is a one-line addition here.
FORWARD_KINDS = frozenset()

def office_label(office_key: str) -> str:
    """Human name for an office key, for alert bodies ("… today (Downtown)").
    Labels come from the company profile (backend/company_profile.json)."""
    import company_profile
    return company_profile.office_label(office_key)


def resolve_office_key(pipeline: Optional[dict]) -> str:
    """CG1 office key for a pipeline: the explicit cg1_office_key first, then
    the office whose match words appear in the slug/name (company profile)."""
    if not pipeline:
        return ""
    import company_profile
    return company_profile.office_key_for_pipeline(pipeline)


async def send_cg1_admin_alert(
    kind: str,
    title: str,
    body: str,
    *,
    candidate: Optional[dict] = None,
    pipeline: Optional[dict] = None,
    office_key: Optional[str] = None,
    link: Optional[str] = None,
) -> bool:
    """POST one admin alert to CG1. Never raises — a failed page is a log
    warning, never a broken caller. Returns True only on a 2xx from CG1.

    Office resolution order: explicit `office_key` param, then the `pipeline`
    doc, then a lookup of the candidate's pipeline. An unresolvable office
    still sends (office_key "") — over-paging beats silence, and CG1 owns the
    fallback routing."""
    try:
        cg1_url = os.getenv("CG1_BACKEND_URL", "").rstrip("/")
        secret = os.getenv("CG1_WEBHOOK_SECRET", "")
        if not cg1_url or not secret:
            logger.info("cg1_alerts: CG1_BACKEND_URL/CG1_WEBHOOK_SECRET unset — %s not sent", kind)
            return False
        office = office_key or resolve_office_key(pipeline)
        if not office and candidate and candidate.get("pipeline_id"):
            try:
                from deps import db
                pipe = await db.pipelines.find_one({"id": candidate["pipeline_id"]}, {"_id": 0}) or {}
                office = resolve_office_key(pipe)
            except Exception as e:
                logger.warning("cg1_alerts: pipeline lookup failed for %s: %s", candidate.get("id"), e)
        if not office:
            logger.warning("cg1_alerts: no office_key resolved for %s — sending anyway", kind)
        cand_id = (candidate or {}).get("id")
        cand_name = (
            f"{(candidate or {}).get('first_name', '')} {(candidate or {}).get('last_name', '')}".strip()
            or None
        )
        if not link:
            public = os.getenv("APP_PUBLIC_URL", "").rstrip("/")
            if public:
                # The dashboard drawer deep-link the notification bell already
                # uses — a stale /?candidate=X still opens the drawer.
                link = f"{public}/?candidate={cand_id}" if cand_id else public
        payload = {
            "kind": kind,
            "title": title,
            "body": body,
            "office_key": office,
            "candidate_id": cand_id,
            "candidate_name": cand_name,
            "link": link,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        }
        async with httpx.AsyncClient(timeout=CG1_ALERT_TIMEOUT) as client:
            r = await client.post(
                f"{cg1_url}/api/webhooks/cgrecruit/admin-alert",
                json=payload,
                headers={"x-webhook-secret": secret},
            )
            r.raise_for_status()
        logger.info("cg1_alerts: %s sent (%s, office=%s)", kind, cand_name or cand_id or "-", office or "?")
        return True
    except Exception as e:
        logger.warning("cg1_alerts: %s failed: %s", kind, e)
        return False


# Strong references to in-flight alert tasks — asyncio only weakly holds
# scheduled tasks, so without this set an alert could be GC'd before it sends
# (same rule as training_sms._PACED_REPLY_TASKS).
_ALERT_TASKS: set = set()


def _spawn(coro) -> None:
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        # No running loop — a sync/offline context. Alerts are nice-to-have,
        # never a crash.
        coro.close()
        logger.warning("cg1_alerts: no event loop — alert dropped")
        return
    _ALERT_TASKS.add(task)
    task.add_done_callback(_ALERT_TASKS.discard)


def spawn_cg1_admin_alert(kind: str, title: str, body: str, **kwargs) -> None:
    """Fire send_cg1_admin_alert without blocking the caller — the shape every
    emitter uses from inside a webhook handler or sweep. Never raises."""
    try:
        _spawn(send_cg1_admin_alert(kind, title, body, **kwargs))
    except Exception as e:
        logger.warning("cg1_alerts: spawn failed for %s: %s", kind, e)


def spawn_forward_notification(
    kind: str,
    title: str,
    body: str,
    *,
    link: Optional[str] = None,
    candidate_id: Optional[str] = None,
    pipeline_id: Optional[str] = None,
) -> None:
    """notifications_service.create_notification's forward hook: same
    title/body as the local notification, docs looked up from the ids the
    notification carries, relative dashboard links made absolute."""
    try:
        _spawn(_forward_notification(
            kind, title, body,
            link=link, candidate_id=candidate_id, pipeline_id=pipeline_id,
        ))
    except Exception as e:
        logger.warning("cg1_alerts: forward spawn failed for %s: %s", kind, e)


async def _forward_notification(
    kind: str,
    title: str,
    body: str,
    *,
    link: Optional[str],
    candidate_id: Optional[str],
    pipeline_id: Optional[str],
) -> None:
    try:
        from deps import db
        candidate = None
        if candidate_id:
            candidate = await db.candidates.find_one({"id": candidate_id}, {"_id": 0})
        pipeline = None
        pid = pipeline_id or (candidate or {}).get("pipeline_id")
        if pid:
            pipeline = await db.pipelines.find_one({"id": pid}, {"_id": 0})
        abs_link = None
        if link:
            if link.startswith("http"):
                abs_link = link
            else:
                public = os.getenv("APP_PUBLIC_URL", "").rstrip("/")
                if public and link.startswith("/"):
                    abs_link = f"{public}{link}"
        await send_cg1_admin_alert(
            kind, title, body,
            candidate=candidate, pipeline=pipeline, link=abs_link,
        )
    except Exception as e:
        logger.warning("cg1_alerts: forward of %s failed: %s", kind, e)
