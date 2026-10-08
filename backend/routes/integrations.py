"""
External integrations: the authenticated intake webhook (Zapier, Make, or any
system that can POST JSON when an application arrives).

GET  /integrations/zapier-token              — return (or create) webhook token + URL
POST /integrations/zapier-token/regenerate   — rotate the token
POST /webhooks/zapier/<token>                — candidate receiver (token in the URL)
GET  /webhooks/zapier/<token>/pipelines      — pipeline list, for mapping in a Zap
"""
import os
import secrets
import re
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request

from deps import db, logger, current_user, require_super_admin

router = APIRouter()

# ─── helpers ────────────────────────────────────────────────────────────────

def _owner_id(user: dict) -> str:
    return user.get("parent_user_id") or user["id"]


# The token lives on the tenant's GLOBAL settings doc ({pipeline_id: None})
# and nowhere else. Per-office override docs used to be cloned from the global
# doc with the token in them. Rotating then changed one doc, and the old token
# went on working from the copies.
_GLOBAL = {"pipeline_id": None}


async def _drop_token_copies(owner_id: str) -> None:
    await db.settings.update_many(
        {"user_id": owner_id, "pipeline_id": {"$ne": None}, "zapier_webhook_token": {"$exists": True}},
        {"$unset": {"zapier_webhook_token": ""}},
    )


async def _set_token(owner_id: str, token: str) -> None:
    from deps import get_or_create_settings
    await get_or_create_settings(owner_id)  # the global doc exists with its defaults
    await db.settings.update_one({"user_id": owner_id, **_GLOBAL}, {"$set": {"zapier_webhook_token": token}})
    await _drop_token_copies(owner_id)


async def _get_or_create_token(owner_id: str) -> str:
    doc = await db.settings.find_one({"user_id": owner_id, **_GLOBAL}, {"_id": 0, "zapier_webhook_token": 1})
    token = (doc or {}).get("zapier_webhook_token")
    if token:
        await _drop_token_copies(owner_id)
        return token
    # An install from before this change may hold its live token only on an
    # override doc. Adopt it, so the connected automation keeps working.
    stray = await db.settings.find_one(
        {"user_id": owner_id, "pipeline_id": {"$ne": None}, "zapier_webhook_token": {"$nin": [None, ""]}},
        {"_id": 0, "zapier_webhook_token": 1},
    )
    token = (stray or {}).get("zapier_webhook_token") or secrets.token_urlsafe(32)
    await _set_token(owner_id, token)
    return token


async def tenant_for_token(token: str) -> Optional[str]:
    """The account an intake token belongs to, or None. Only the global doc
    counts. An override doc's copy is honoured only while the global doc has no
    token at all (an install from before tokens moved), and is then moved onto
    the global doc. A copy left behind by a rotation never authenticates."""
    if not isinstance(token, str) or not token:
        return None
    doc = await db.settings.find_one({"zapier_webhook_token": token, **_GLOBAL}, {"_id": 0, "user_id": 1})
    if doc:
        return doc["user_id"]
    stray = await db.settings.find_one(
        {"zapier_webhook_token": token, "pipeline_id": {"$ne": None}}, {"_id": 0, "user_id": 1})
    if not stray:
        return None
    owner_id = stray["user_id"]
    glob = await db.settings.find_one({"user_id": owner_id, **_GLOBAL}, {"_id": 0, "zapier_webhook_token": 1})
    if (glob or {}).get("zapier_webhook_token"):
        return None  # rotated: the global doc's token is the only live one
    await _set_token(owner_id, token)
    return owner_id


def _webhook_url(token: str) -> str:
    from company_profile import backend_api_url
    # BACKEND_URL, else APP_PUBLIC_URL + /api. With neither set the URL is
    # relative and the Settings screen shows it for completion by hand.
    return f"{backend_api_url()}/webhooks/zapier/{token}"


def _normalise_phone(raw: str) -> str:
    """E.164 when the number can be read confidently (see phone_util and
    PHONE_DEFAULT_COUNTRY); otherwise just its digits (and a leading +)."""
    if not raw:
        return ""
    from phone_util import normalize_phone_e164
    norm = normalize_phone_e164(raw)
    if norm.startswith("+"):
        return norm
    return re.sub(r"[^\d+]", "", raw)  # leave ambiguous numbers for a human


# ─── public pipelines list (token-authenticated) ───────────────────────────

@router.get("/webhooks/zapier/{token}/pipelines")
async def webhook_pipelines(token: str):
    """Return the pipeline list so an automation can pick a pipeline_id."""
    from fastapi.responses import JSONResponse
    user_id = await tenant_for_token(token)
    if not user_id:
        return JSONResponse({"error": "invalid token"}, status_code=403,
                            headers={"Access-Control-Allow-Origin": "*"})
    pipelines = await db.pipelines.find(
        {"user_id": user_id}, {"_id": 0, "id": 1, "name": 1}
    ).to_list(50)
    return JSONResponse({"pipelines": pipelines},
                        headers={"Access-Control-Allow-Origin": "*"})


# ─── token management (authenticated) ──────────────────────────────────────

@router.get("/integrations/zapier-token")
async def get_zapier_token(user: dict = Depends(current_user)):
    """Return the current webhook token and ready-to-paste URL.

    Owner only: the token is the whole authentication of the intake webhook,
    which files candidates who are then texted and called."""
    require_super_admin(user)
    token = await _get_or_create_token(_owner_id(user))
    return {"token": token, "webhook_url": _webhook_url(token)}


@router.post("/integrations/zapier-token/regenerate")
async def regenerate_zapier_token(user: dict = Depends(current_user)):
    """Rotate the token (old URL stops working immediately)."""
    # The token is tenant-wide infra — `_owner_id` resolves a sub-account up to
    # the tenant owner, so a recruiter rotating it would kill webhook intake for
    # every office on the account. Same gate as PUT /settings/integrations.
    require_super_admin(user)
    owner_id = _owner_id(user)
    token = secrets.token_urlsafe(32)
    # Sets it on the global doc and removes every other copy, so the old URL
    # really is dead afterwards.
    await _set_token(owner_id, token)
    return {"token": token, "webhook_url": _webhook_url(token)}


# ─── public webhook receiver ─────────────────────────────────────────────────

_CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "Content-Type"}

@router.options("/webhooks/zapier/{token}")
async def zapier_webhook_preflight(token: str):
    from fastapi.responses import Response
    return Response(status_code=204, headers={**_CORS, "Access-Control-Allow-Methods": "POST, OPTIONS"})


@router.post("/webhooks/zapier/{token}")
async def zapier_webhook_receiver(token: str, request: Request):
    """
    Intake webhook: Zapier (or anything that can POST) sends one applicant
    here, from any job board, form or spreadsheet.

    Expected fields (all optional except at least a name):
      name / first_name + last_name
      email
      phone       — international format (+44..., +1...). A bare 10-digit
                    number is assumed to be US; anything else is kept as sent.
      job_title   — used to find the right pipeline/job
      resume      — plain text resume (parsed by AI if present)
      pipeline_id — skip matching, use this pipeline directly
      source      — where the applicant came from (e.g. "totaljobs");
                    stored on the candidate, default "webhook"
    """
    # Authenticate via token
    user_id = await tenant_for_token(token)
    if not user_id:
        raise HTTPException(403, "Invalid webhook token")

    # Support both JSON (API/Zapier) and form-encoded posts
    content_type = request.headers.get("content-type", "")
    is_form = "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type
    if is_form:
        try:
            form = await request.form()
            body = dict(form)
        except Exception:
            raise HTTPException(400, "Invalid form data")
    else:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Request body must be JSON")

    # ── parse name ──────────────────────────────────────────────────────────
    full_name = (body.get("name") or body.get("applicant_name") or "").strip()
    first_name = (body.get("first_name") or "").strip()
    last_name = (body.get("last_name") or "").strip()
    if full_name and not first_name:
        parts = full_name.split(" ", 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""
    if not first_name:
        first_name = "Unknown"

    email = (body.get("email") or body.get("applicant_email") or "").lower().strip()
    phone = _normalise_phone(body.get("phone") or body.get("applicant_phone") or "")
    job_title = (body.get("job_title") or body.get("position") or body.get("job") or "").strip()
    resume_text = (body.get("resume") or body.get("resume_text") or body.get("cover_letter") or "").strip()
    pipeline_id: Optional[str] = body.get("pipeline_id")
    if pipeline_id is not None and not isinstance(pipeline_id, str):
        raise HTTPException(400, "pipeline_id must be a string")
    pipeline_id = (pipeline_id or "").strip() or None

    # ── resolve pipeline ────────────────────────────────────────────────────
    if not pipeline_id:
        if job_title:
            jobs = await db.jobs.find({"user_id": user_id}, {"_id": 0, "id": 1, "title": 1, "pipeline_id": 1}).to_list(200)
            jt_lower = job_title.lower()
            for job in jobs:
                t = (job.get("title") or "").lower()
                if jt_lower in t or t in jt_lower:
                    pipeline_id = job.get("pipeline_id")
                    break
        if not pipeline_id:
            first_pipeline = await db.pipelines.find_one({"user_id": user_id}, {"_id": 0, "id": 1})
            if first_pipeline:
                pipeline_id = first_pipeline["id"]

    if not pipeline_id:
        raise HTTPException(400, "No matching pipeline found — add at least one pipeline to your account")
    # A caller-supplied pipeline_id must belong to the account that owns the token.
    if not await db.pipelines.find_one({"id": pipeline_id, "user_id": user_id}, {"_id": 1}):
        raise HTTPException(400, "Unknown pipeline_id for this webhook")

    # ── duplicate check ─────────────────────────────────────────────────────
    from deps import find_duplicate_candidate
    dup = await find_duplicate_candidate(pipeline_id, email, phone)
    if dup:
        logger.info(f"zapier webhook: duplicate candidate skipped ({email or phone})")
        return {"ok": True, "duplicate": True, "candidate_id": dup["id"]}

    # ── create candidate ────────────────────────────────────────────────────
    from models import Candidate, now_iso as _now_iso
    candidate = Candidate(
        user_id=user_id,
        pipeline_id=pipeline_id,
        first_name=first_name,
        last_name=last_name,
        email=email,
        phone=phone,
    )
    doc = candidate.model_dump()
    # Set on the doc: Candidate ignores unknown fields, so source= in the
    # constructor would be dropped.
    _src = body.get("source")
    doc["source"] = (_src.strip()[:40] if isinstance(_src, str) and _src.strip() else "webhook")

    # ── AI resume parse (if text provided) ──────────────────────────────────
    if resume_text and len(resume_text.strip()) >= 30:
        try:
            from ai_service import parse_resume
            parsed = await parse_resume(resume_text, candidate.id)
            if parsed:
                # Only fill in fields the caller didn't already provide
                if first_name == "Unknown" and parsed.get("first_name"):
                    doc["first_name"] = parsed["first_name"]
                    doc["last_name"] = parsed.get("last_name") or ""
                if not email and parsed.get("email"):
                    doc["email"] = parsed["email"].lower()
                if not phone and parsed.get("phone"):
                    doc["phone"] = _normalise_phone(parsed["phone"])
                doc["resume_summary"] = parsed.get("summary") or ""
                doc["resume_skills"] = parsed.get("skills") or []
                doc["resume_experience"] = parsed.get("experience") or []
        except Exception as e:
            logger.warning(f"zapier resume parse failed: {e}")

    await db.candidates.insert_one(doc)
    logger.info(f"zapier: created candidate {candidate.id} ({first_name} {last_name}) in pipeline {pipeline_id}")

    # ── arrival comms + screening call ───────────────────────────────────────
    from deps import resolve_settings
    from screening_start import start_screening

    settings = await resolve_settings(user_id, pipeline_id)
    await start_screening(user_id, doc, settings)

    from fastapi.responses import JSONResponse
    return JSONResponse(
        {"ok": True, "candidate_id": candidate.id, "pipeline_id": pipeline_id},
        headers=_CORS,
    )
