"""Notifications API — powers the in-app bell icon in the top nav."""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException

from deps import current_user
from notifications_service import (
    list_notifications, count_unread, mark_read, mark_all_read,
)

router = APIRouter()


def _allowed_pipeline_ids(user: dict) -> Optional[List[str]]:
    """Return None for super-admins (sees everything for their tenant) or
    the recruiter's assigned `pipeline_ids` list (restricts to their slice)."""
    if user.get("role") == "super_admin":
        return None
    return user.get("pipeline_ids") or []


@router.get("/notifications")
async def get_notifications(unread_only: bool = False, limit: int = 50, user: dict = Depends(current_user)):
    """Latest notifications for the bell dropdown. Default 50."""
    rows = await list_notifications(
        user["id"],
        pipeline_ids=_allowed_pipeline_ids(user),
        unread_only=unread_only,
        limit=min(limit, 100),
    )
    return {"items": rows, "unread": await count_unread(user["id"], pipeline_ids=_allowed_pipeline_ids(user))}


@router.post("/notifications/{notification_id}/read")
async def post_mark_read(notification_id: str, user: dict = Depends(current_user)):
    ok = await mark_read(user["id"], notification_id)
    if not ok:
        raise HTTPException(404, "Notification not found")
    return {"ok": True}


@router.post("/notifications/read-all")
async def post_mark_all_read(user: dict = Depends(current_user)):
    n = await mark_all_read(user["id"], pipeline_ids=_allowed_pipeline_ids(user))
    return {"ok": True, "marked": n}
