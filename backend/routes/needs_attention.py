"""Needs-attention API — the dashboard panel listing bad or partial ingests.

Two sources, one list:

  * candidates that arrived missing something the pipeline depends on
  * applications whose email only linked to the resume, which produced no
    candidate at all and so are invisible everywhere else in the UI

Ordered worst-first, because a link-only application means nobody is in the
pipeline while a missing email only means one message didn't send.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends

from deps import (
    db, current_user, require_mover, is_super_admin, assert_pipeline_access,
    assert_candidate_access, candidate_ownership_filter,
)
from models import now_iso
from needs_attention_service import (
    DEFAULT_WINDOW_DAYS,
    DETAILS,
    LABELS,
    candidate_issues,
    summarise,
    worst_severity,
)

router = APIRouter()

MAX_ITEMS = 100


def _is_super_admin(user: dict) -> bool:
    return user.get("role") == "super_admin"


def _owner_id(user: dict) -> str:
    return user.get("parent_user_id") or user["id"]


def _pipeline_filter(user: dict) -> Optional[List[str]]:
    """None means unrestricted (super-admin); otherwise the recruiter's slice."""
    if _is_super_admin(user):
        return None
    return user.get("pipeline_ids") or []


@router.get("/needs-attention")
async def list_needs_attention(days: int = DEFAULT_WINDOW_DAYS, user: dict = Depends(current_user)):
    """Recent ingests that need a human. Newest and worst first."""
    days = max(1, min(days, 90))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    owner = _owner_id(user)
    allowed = _pipeline_filter(user)

    pipelines = {
        p["id"]: p.get("name", "")
        async for p in db.pipelines.find({"user_id": owner}, {"_id": 0, "id": 1, "name": 1})
    }

    items: List[Dict[str, Any]] = []

    # 1. link-only applications — no candidate exists for these at all
    link_q: Dict[str, Any] = {"user_id": owner, "status": "open", "created_at": {"$gte": cutoff}}
    if allowed is not None:
        link_q["pipeline_id"] = {"$in": allowed}
    async for f in db.link_only_applications.find(link_q, {"_id": 0}).sort("created_at", -1):
        reason = " · ".join(x for x in (f.get("job_title"), f.get("relevant_experience")) if x)
        items.append({
            "id": f.get("id"),
            "source": "link_only",
            "issues": ["resume_not_attached"],
            "labels": [LABELS["resume_not_attached"]],
            "summary": LABELS["resume_not_attached"],
            "detail": DETAILS["resume_not_attached"],
            "reason": reason[:300],
            "name": f.get("applicant_name") or "Unknown applicant",
            "pipeline_id": f.get("pipeline_id"),
            "pipeline_name": f.get("pipeline_name") or pipelines.get(f.get("pipeline_id"), ""),
            "created_at": f.get("created_at"),
            "severity": worst_severity(["resume_not_attached"]),
            "link": None,
            "can_dismiss": True,
        })

    # 2. candidates that arrived incomplete
    cand_q: Dict[str, Any] = {
        "user_id": owner,
        "created_at": {"$gte": cutoff},
        "stage": {"$nin": ["CLOSE"]},
        "$or": [{"archived_at": None}, {"archived_at": {"$exists": False}}],
    }
    if allowed is not None:
        cand_q["pipeline_id"] = {"$in": allowed}
    cand_q.update(candidate_ownership_filter(user))  # viewers: only the ones they added
    async for c in db.candidates.find(
        cand_q,
        {"_id": 0, "id": 1, "first_name": 1, "last_name": 1, "email": 1, "phone": 1,
         "pipeline_id": 1, "created_at": 1, "resume_text": 1, "parsed_resume": 1,
         "needs_attention_dismissed_at": 1},
    ).sort("created_at", -1).limit(2000):
        if c.get("needs_attention_dismissed_at"):
            # A recruiter looked, fixed what needed fixing by hand, and said so.
            continue
        issues = candidate_issues(c)
        if not issues:
            continue
        name = f"{c.get('first_name', '')} {c.get('last_name', '')}".strip() or "Unnamed candidate"
        items.append({
            "id": c.get("id"),
            "source": "candidate",
            "issues": issues,
            "labels": [LABELS.get(i, i) for i in issues],
            "summary": summarise(issues),
            "detail": " ".join(DETAILS.get(i, "") for i in issues).strip(),
            "reason": "",
            "name": name,
            "pipeline_id": c.get("pipeline_id"),
            "pipeline_name": pipelines.get(c.get("pipeline_id"), ""),
            "created_at": c.get("created_at"),
            "severity": worst_severity(issues),
            "link": f"/?candidate={c.get('id')}",
            "can_dismiss": False,
        })

    # Worst first, then newest — a link-only application outranks a missing email regardless of age.
    items.sort(key=lambda i: (-i["severity"], -_epoch(i.get("created_at"))))

    return {"items": items[:MAX_ITEMS], "count": len(items), "truncated": len(items) > MAX_ITEMS,
            "window_days": days}


def _epoch(iso: Optional[str]) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return 0.0


@router.post("/needs-attention/dismiss/{item_id}")
async def dismiss_item(item_id: str, user: dict = Depends(current_user)):
    """A human handled it by hand — clear it from the list.

    Link-only applications are marked dismissed; candidate items get a stamp
    the listing skips. Either way, one click and the banner believes you.
    """
    # Read-only accounts don't clear work off other people's lists, and nobody
    # clears an item in an office they aren't assigned to.
    require_mover(user)
    owner = _owner_id(user)
    item = await db.link_only_applications.find_one({"id": item_id, "user_id": owner}, {"_id": 0, "id": 1, "pipeline_id": 1})
    if item:
        if not is_super_admin(user):
            await assert_pipeline_access(user, item.get("pipeline_id") or "")
        await db.link_only_applications.update_one(
            {"id": item_id}, {"$set": {"status": "dismissed", "dismissed_at": now_iso()}})
        return {"ok": True, "source": "link_only"}
    cand = await db.candidates.find_one({"id": item_id, "user_id": owner}, {"_id": 0, "id": 1, "pipeline_id": 1, "added_by_user_id": 1})
    await assert_candidate_access(user, cand)
    await db.candidates.update_one(
        {"id": item_id}, {"$set": {"needs_attention_dismissed_at": now_iso()}})
    return {"ok": True, "source": "candidate"}
