"""Grouping lapsed interviews into sessions that need marking off.

The subtle part, and the reason this is a module rather than a few lines inside
the endpoint: a session is a *room at a time*, not a pipeline.

Two offices can deliberately point some slots at ONE shared video room — joint
sessions run by one host. Attendance had always been organised per office, so
the host who was actually in the room never saw the other office's attendees,
and that office's recruiter who could see them wasn't there. Candidates in the
shared room mostly ended up with no outcome recorded.

So: everyone booked at the same time into the same link is one session, whatever
pipeline they belong to, and it is visible to anyone who can see at least one of
the pipelines represented in it — i.e. whoever is hosting.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from models import appointment_has_lapsed, parse_appointment_at

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def session_key(row: Dict[str, Any]) -> tuple:
    """A room at a time. Two candidates share a session iff this matches.

    The link is part of the key on purpose. Two offices running unrelated
    interviews at 2pm in different rooms are two sessions and must not be merged
    into one list — that would hand a host a roster of people who aren't there.
    """
    return (str(row.get("appointment_at") or ""), str(row.get("appointment_link") or ""))


def group_unmarked_into_sessions(
    rows: Iterable[Dict[str, Any]],
    *,
    pipeline_names: Optional[Dict[str, str]] = None,
    visible_pipeline_ids: Optional[Iterable[str]] = None,
    grace_minutes: int = 120,
) -> List[Dict[str, Any]]:
    """Lapsed, unmarked candidates → sessions, oldest debt first.

    `visible_pipeline_ids=None` means unrestricted (a super-admin). Otherwise a
    session is returned whole if it touches any visible pipeline, because the
    point is that the host marks off the whole room.
    """
    names = pipeline_names or {}
    visible = None if visible_pipeline_ids is None else set(visible_pipeline_ids)

    sessions: Dict[tuple, Dict[str, Any]] = {}
    for row in rows:
        if row.get("attendance_status"):
            continue
        if not appointment_has_lapsed(row.get("appointment_at"), grace_minutes=grace_minutes):
            continue
        s = sessions.setdefault(session_key(row), {
            "appointment_at": row.get("appointment_at"),
            "appointment_link": row.get("appointment_link"),
            "_pipeline_ids": set(),
            "candidates": [],
        })
        s["_pipeline_ids"].add(row.get("pipeline_id"))
        s["candidates"].append({
            "id": row.get("id"),
            "name": f"{row.get('first_name', '')} {row.get('last_name', '')}".strip(),
            "pipeline_id": row.get("pipeline_id"),
            "pipeline_name": names.get(row.get("pipeline_id"), ""),
            "confirmed": bool(row.get("appointment_sms_confirmed")),
            "archived": bool(row.get("archived_at")),
            "stage": row.get("stage"),
        })

    out: List[Dict[str, Any]] = []
    for s in sessions.values():
        pids = s.pop("_pipeline_ids")
        if visible is not None and not (pids & visible):
            continue
        s["pipeline_names"] = sorted({names.get(p, "") for p in pids if p})
        s["shared"] = len({p for p in pids if p}) > 1
        s["unmarked"] = len(s["candidates"])
        out.append(s)

    # Oldest first: the debt least likely to be remembered accurately is the one
    # to clear first.
    out.sort(key=lambda s: (parse_appointment_at(s["appointment_at"]) or _EPOCH))
    return out
