"""Analyst account management — super-admin only.

Analyst accounts are read-only sub-accounts scoped to the Intelligence/reporting
section. They cannot access the kanban, settings, or any write operations.

Each analyst has:
  - parent_user_id  → resolves data queries to the parent super-admin's tenant
  - pipeline_ids    → limits which pipelines' data they can see (empty = all)
  - role = "analyst"

POST   /analyst-users            create a new analyst account
GET    /analyst-users            list all analyst accounts for this tenant
PATCH  /analyst-users/{id}       update name / pipeline_ids / reset password
DELETE /analyst-users/{id}       delete analyst account
"""
import secrets
import string
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException
from fastapi.requests import Request

from auth_service import hash_password
from deps import current_user, db, is_analyst, require_super_admin
from fastapi import Depends
from models import User, now_iso

router = APIRouter()


def _generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


@router.post("/analyst-users")
async def create_analyst_user(
    payload: Dict[str, Any] = Body(...),
    user: dict = Depends(current_user),
) -> Dict[str, Any]:
    """Create a new analyst account under this super-admin tenant."""
    require_super_admin(user)

    name = (payload.get("name") or "").strip()
    email = (payload.get("email") or "").strip().lower()
    password = (payload.get("password") or "").strip()
    pipeline_ids: List[str] = payload.get("pipeline_ids") or []

    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail="valid email is required")

    existing = await db.users.find_one({"email": email}, {"_id": 0, "id": 1})
    if existing:
        raise HTTPException(status_code=409, detail="An account with that email already exists")

    if not password:
        password = _generate_password()

    analyst = User(
        email=email,
        name=name,
        role="analyst",
        parent_user_id=user["auth_user_id"],
        pipeline_ids=pipeline_ids,
    )
    doc = analyst.model_dump()
    doc["password_hash"] = hash_password(password)
    await db.users.insert_one(doc)
    doc.pop("_id", None)
    doc.pop("password_hash", None)
    doc["generated_password"] = password
    return doc


@router.get("/analyst-users")
async def list_analyst_users(
    user: dict = Depends(current_user),
) -> List[Dict[str, Any]]:
    """List all analyst accounts belonging to this tenant."""
    require_super_admin(user)

    rows = await db.users.find(
        {"parent_user_id": user["auth_user_id"], "role": "analyst"},
        {"_id": 0, "password_hash": 0},
    ).to_list(200)
    return rows


@router.patch("/analyst-users/{analyst_id}")
async def update_analyst_user(
    analyst_id: str,
    payload: Dict[str, Any] = Body(...),
    user: dict = Depends(current_user),
) -> Dict[str, Any]:
    """Update name, pipeline_ids, or reset password for an analyst account."""
    require_super_admin(user)

    doc = await db.users.find_one(
        {"id": analyst_id, "parent_user_id": user["auth_user_id"], "role": "analyst"},
        {"_id": 0},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Analyst account not found")

    updates: Dict[str, Any] = {"updated_at": now_iso()}
    generated_password: Optional[str] = None

    if "name" in payload and payload["name"]:
        updates["name"] = payload["name"].strip()
    if "pipeline_ids" in payload:
        updates["pipeline_ids"] = payload["pipeline_ids"] or []
    if payload.get("reset_password"):
        generated_password = _generate_password()
        updates["password_hash"] = hash_password(generated_password)

    await db.users.update_one({"id": analyst_id}, {"$set": updates})
    refreshed = await db.users.find_one({"id": analyst_id}, {"_id": 0, "password_hash": 0})
    if generated_password:
        refreshed["generated_password"] = generated_password
    return refreshed


@router.delete("/analyst-users/{analyst_id}")
async def delete_analyst_user(
    analyst_id: str,
    user: dict = Depends(current_user),
) -> Dict[str, Any]:
    """Permanently delete an analyst account."""
    require_super_admin(user)

    result = await db.users.delete_one(
        {"id": analyst_id, "parent_user_id": user["auth_user_id"], "role": "analyst"}
    )
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Analyst account not found")
    return {"deleted": True, "id": analyst_id}
