"""Super-admin-only user management: create / list / update / delete
recruiter sub-accounts tied to specific pipelines."""
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, HTTPException, Depends, Body

from deps import db, current_super_admin, current_user
from auth_service import hash_password
from models import now_iso, new_id

router = APIRouter()


@router.get("/admin/users")
async def list_sub_accounts(user: dict = Depends(current_super_admin)):
    """List every recruiter sub-account that belongs to the calling super-admin."""
    rows = await db.users.find(
        {"parent_user_id": user["id"]},
        {"_id": 0, "password_hash": 0},
    ).sort("created_at", -1).to_list(100)
    # Enrich with pipeline names for display.
    pipe_rows = await db.pipelines.find({"user_id": user["id"]}, {"_id": 0, "id": 1, "name": 1}).to_list(200)
    name_by_id = {p["id"]: p["name"] for p in pipe_rows}
    for r in rows:
        r["pipeline_names"] = [name_by_id.get(pid, pid) for pid in (r.get("pipeline_ids") or [])]
    return rows


@router.post("/admin/users")
async def create_sub_account(
    payload: Dict[str, Any] = Body(...),
    user: dict = Depends(current_super_admin),
):
    """Create a new recruiter login assigned to one or more pipelines."""
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    name = (payload.get("name") or "").strip() or email.split("@", 1)[0]
    pipeline_ids: List[str] = payload.get("pipeline_ids") or []
    role = (payload.get("role") or "recruiter").strip()
    if role not in ("recruiter", "viewer"):
        raise HTTPException(400, "role must be 'recruiter' or 'viewer'")
    from auth_service import MIN_PASSWORD_LENGTH
    if not email or len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(400, f"email and password (min {MIN_PASSWORD_LENGTH} chars) required")
    if not isinstance(pipeline_ids, list) or not pipeline_ids:
        raise HTTPException(400, "at least one pipeline_id required")
    # Verify every pipeline belongs to the super-admin.
    owned = await db.pipelines.find(
        {"id": {"$in": pipeline_ids}, "user_id": user["id"]},
        {"_id": 0, "id": 1},
    ).to_list(100)
    owned_ids = {p["id"] for p in owned}
    if set(pipeline_ids) - owned_ids:
        raise HTTPException(400, "one or more pipeline_ids do not belong to you")
    exists = await db.users.find_one({"email": email}, {"_id": 0, "id": 1})
    if exists:
        raise HTTPException(400, "Email already registered")
    doc = {
        "id": new_id(),
        "email": email,
        "name": name,
        "company": user.get("company") or "",
        "role": role,
        "parent_user_id": user["id"],
        "pipeline_ids": pipeline_ids,
        "password_hash": hash_password(password),
        "created_at": now_iso(),
    }
    await db.users.insert_one(doc)
    # `insert_one` mutates `doc` to add a Mongo ObjectId — strip it (and the password
    # hash) before returning so FastAPI's JSON encoder doesn't choke on the ObjectId.
    return {k: v for k, v in doc.items() if k not in ("password_hash", "_id")}


@router.put("/admin/users/{sub_user_id}")
async def update_sub_account(
    sub_user_id: str,
    payload: Dict[str, Any] = Body(...),
    user: dict = Depends(current_super_admin),
):
    """Change name / pipeline assignment / reset password on a sub-account."""
    target = await db.users.find_one(
        {"id": sub_user_id, "parent_user_id": user["id"]},
        {"_id": 0},
    )
    if not target:
        raise HTTPException(404, "Sub-account not found")
    update: Dict[str, Any] = {}
    if "name" in payload:
        update["name"] = (payload.get("name") or "").strip() or target.get("name")
    if "role" in payload:
        new_role = (payload.get("role") or "recruiter").strip()
        if new_role not in ("recruiter", "viewer"):
            raise HTTPException(400, "role must be 'recruiter' or 'viewer'")
        update["role"] = new_role
    if "pipeline_ids" in payload:
        pipeline_ids: List[str] = payload.get("pipeline_ids") or []
        if not pipeline_ids:
            # An empty assignment would hide every candidate/pipeline from the
            # sub-account — surely a UI mistake, not an intent. Delete instead.
            raise HTTPException(400, "at least one pipeline_id required")
        owned = await db.pipelines.find(
            {"id": {"$in": pipeline_ids}, "user_id": user["id"]},
            {"_id": 0, "id": 1},
        ).to_list(100)
        if set(pipeline_ids) - {p["id"] for p in owned}:
            raise HTTPException(400, "one or more pipeline_ids do not belong to you")
        update["pipeline_ids"] = pipeline_ids
    if payload.get("password"):
        from auth_service import MIN_PASSWORD_LENGTH
        if len(payload["password"]) < MIN_PASSWORD_LENGTH:
            raise HTTPException(400, f"password must be at least {MIN_PASSWORD_LENGTH} chars")
        update["password_hash"] = hash_password(payload["password"])
        # Signs out that person's existing sessions (deps.current_user).
        from models import now_iso as _now_iso
        update["password_changed_at"] = _now_iso()
    if update:
        await db.users.update_one({"id": sub_user_id}, {"$set": update})
    refreshed = await db.users.find_one(
        {"id": sub_user_id}, {"_id": 0, "password_hash": 0},
    )
    return refreshed


@router.delete("/admin/users/{sub_user_id}")
async def delete_sub_account(sub_user_id: str, user: dict = Depends(current_super_admin)):
    res = await db.users.delete_one({"id": sub_user_id, "parent_user_id": user["id"]})
    if res.deleted_count == 0:
        raise HTTPException(404, "Sub-account not found")
    return {"ok": True}
