"""Notification creation helpers — the in-app bell uses these to surface
recent events (calls completed, slots booked, applications submitted, etc.).

All notifications are per-user. Recruiters see notifications scoped to
their assigned pipelines; super-admins see everything for their tenant.

Use `create_notification(user_id, kind, title, ...)` from anywhere in the
backend that touches a meaningful event. The bell badge updates next time
the frontend polls (every 30s by default)."""
from typing import Optional, List, Dict, Any
import logging

from deps import db
from models import Notification

logger = logging.getLogger(__name__)


# The bell is for situations that NEED a person — an emergency, or something
# the AI cannot handle itself. Everything routine (screenings finishing,
# bookings landing, texts arriving, reschedules the system already handled)
# shows on the board and must not ring: a bell that announces the standard
# flow trains the recruiter to ignore it, which is how the one abuse handoff
# that matters gets missed.
ACTIONABLE_KINDS = frozenset({
    "sms.replies_paused",           # abuse/runaway — AI stopped replying, person decides
    "sms.unmatched",                # unknown number said something — could be a withdrawal
    "screening.booked_unscreened",  # interview scheduled, nobody ever asked the gates
    "screening.gate_check_failed",  # booked candidate failed a gate — slot decision is human
    "call.inbound",                 # a human called the office line
    "candidate.duplicate",          # merge decision — human-only judgement
    "appointment.outcome_due",      # the recruiter's own task: record attendance
    "appointment.attendance_unrecorded",
    "appointment.session_unconfirmed",  # session imminent, zero Y-replies — human decides
    "booking.link_missing",         # booking confirmed with no join link — only a human can supply one
    "booking.park_dropped",         # parked candidate silently dropped — a human must send the link
    "email.unverified_reply",       # email about a candidate from an address not on file — AI held it
})


async def create_notification(
    user_id: str,
    kind: str,
    title: str,
    body: str = "",
    *,
    link: Optional[str] = None,
    candidate_id: Optional[str] = None,
    pipeline_id: Optional[str] = None,
) -> Optional[Notification]:
    """Create a single notification doc. Safe to call from any code path —
    failures are logged but never propagate (notifications are nice-to-have
    UX, NOT a critical path). Kinds outside ACTIONABLE_KINDS are silently
    dropped — see the policy note above."""
    # CG1 forward hook — a handful of kinds ALSO page the office admins'
    # phones through CG1 (see cg1_alerts.FORWARD_KINDS). This sits before the
    # ACTIONABLE_KINDS gate on purpose: suppressed kinds still reach this
    # function, they just don't ring the local bell — the gate below is
    # untouched, and the spawn never blocks or raises.
    try:
        from cg1_alerts import FORWARD_KINDS, spawn_forward_notification
        if kind in FORWARD_KINDS:
            spawn_forward_notification(
                kind, title, body,
                link=link, candidate_id=candidate_id, pipeline_id=pipeline_id,
            )
    except Exception as e:
        logger.warning(f"cg1 forward failed for kind={kind}: {e}")
    if kind not in ACTIONABLE_KINDS:
        logger.debug("notification suppressed (routine kind): %s — %s", kind, title)
        return None
    try:
        n = Notification(
            user_id=user_id, kind=kind, title=title, body=body, link=link,
            candidate_id=candidate_id, pipeline_id=pipeline_id,
        )
        await db.notifications.insert_one(n.model_dump())
        return n
    except Exception as e:
        # Don't let notifications break the parent flow (call completion, apply, etc.)
        logger.warning(f"failed to create notification kind={kind} user={user_id}: {e}")
        return None  # type: ignore


async def list_notifications(
    user_id: str,
    *,
    pipeline_ids: Optional[List[str]] = None,
    unread_only: bool = False,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Return up to `limit` most-recent notifications for this user.
    `pipeline_ids` is the recruiter's allowed set — if provided, restricts
    to notifications whose `pipeline_id` is in that set OR null (tenant-wide)."""
    q: Dict[str, Any] = {"user_id": user_id}
    if unread_only:
        q["read"] = False
    if pipeline_ids is not None:
        q["$or"] = [
            {"pipeline_id": {"$in": pipeline_ids}},
            {"pipeline_id": None},
        ]
    rows = await db.notifications.find(q, {"_id": 0}).sort("created_at", -1).limit(limit).to_list(limit)
    return rows


async def count_unread(user_id: str, *, pipeline_ids: Optional[List[str]] = None) -> int:
    q: Dict[str, Any] = {"user_id": user_id, "read": False}
    if pipeline_ids is not None:
        q["$or"] = [
            {"pipeline_id": {"$in": pipeline_ids}},
            {"pipeline_id": None},
        ]
    return await db.notifications.count_documents(q)


async def mark_read(user_id: str, notification_id: str) -> bool:
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    res = await db.notifications.update_one(
        {"id": notification_id, "user_id": user_id},
        {"$set": {"read": True, "read_at": now_iso}},
    )
    return res.modified_count > 0


async def mark_all_read(user_id: str, *, pipeline_ids: Optional[List[str]] = None) -> int:
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    q: Dict[str, Any] = {"user_id": user_id, "read": False}
    if pipeline_ids is not None:
        q["$or"] = [
            {"pipeline_id": {"$in": pipeline_ids}},
            {"pipeline_id": None},
        ]
    res = await db.notifications.update_many(q, {"$set": {"read": True, "read_at": now_iso}})
    return res.modified_count
