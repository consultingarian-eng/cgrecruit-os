"""No-show revival routes.

- POST /revival/agent/sync            — create/sync the per-pipeline ElevenLabs
                                        revival agent (stores pipeline.revival_agent_id)
- GET  /revival/preview               — dry-run: who would be called (zero writes)
- POST /candidates/{id}/revival-call  — manual single-candidate trigger (smoke test)
"""
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Body, Depends, Query

from deps import (
    candidate_write_access,
    db, logger, current_user, resolve_settings,
    assert_pipeline_access, require_super_admin, require_mover,
)
from models import now_iso

router = APIRouter()


async def _sync_one_pipeline(user_id: str, pipe: Dict[str, Any], tool_ids: list) -> Dict[str, Any]:
    """Create the revival agent if the pipeline doesn't have one, then PATCH it
    with the latest revival prompt/config. Returns a result row."""
    from voice_service import create_revival_agent_for_pipeline, sync_revival_agent_to_elevenlabs
    from dialer.revival import revival_settings

    settings = await resolve_settings(user_id, pipe["id"]) or {}
    sca = settings.get("screen_call_agent") or {}
    booking_prefs = settings.get("booking_preferences") or {}
    rev = revival_settings(settings)
    voice_id = pipe.get("voice_id_override") or sca.get("voice_id") or None

    agent_id = (pipe.get("revival_agent_id") or "").strip()
    created = False
    if not agent_id:
        res = create_revival_agent_for_pipeline(settings, pipe, tool_ids=tool_ids)
        agent_id = (res or {}).get("agent_id") or ""
        if not agent_id:
            return {"pipeline": pipe.get("name"), "pipeline_id": pipe["id"], "status": "failed",
                    "error": "ElevenLabs agent creation failed — check ELEVENLABS_API_KEY and logs"}
        created = True
        await db.pipelines.update_one(
            {"id": pipe["id"], "user_id": user_id},
            {"$set": {"revival_agent_id": agent_id}},
        )

    sync_res = sync_revival_agent_to_elevenlabs(
        agent_id, sca, voice_id=voice_id, tool_ids=tool_ids, booking_prefs=booking_prefs, rev=rev,
    )
    return {
        "pipeline": pipe.get("name"), "pipeline_id": pipe["id"],
        "agent_id": agent_id, "created": created, **sync_res,
    }


@router.post("/revival/agent/sync")
async def revival_agent_sync(
    payload: Dict[str, Any] = Body(default={}),
    user: dict = Depends(current_user),
):
    """Create + sync the dedicated revival agent(s). With pipeline_id in the
    body, only that pipeline (recruiter-accessible); without, all pipelines
    (super-admin only). Mirrors POST /elevenlabs/agent/sync."""
    require_mover(user)
    pipeline_id = (payload or {}).get("pipeline_id")
    if pipeline_id:
        await assert_pipeline_access(user, pipeline_id)
        pipelines = await db.pipelines.find(
            {"id": pipeline_id, "user_id": user["id"]}, {"_id": 0},
        ).to_list(1)
    else:
        require_super_admin(user)
        pipelines = await db.pipelines.find({"user_id": user["id"]}, {"_id": 0}).to_list(100)
    if not pipelines:
        raise HTTPException(404, "Pipeline not found")

    # Same workspace tools the screening agent uses — get_available_slots +
    # book_slot. Idempotent upsert; booking through them already restores
    # archived candidates to the kanban.
    from voice_service import upsert_elevenlabs_tools
    public_base = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
    tool_map = upsert_elevenlabs_tools(public_base)
    tool_ids = [tid for k, tid in (tool_map or {}).items() if tid and k != "error"]

    results = []
    for pipe in pipelines:
        try:
            results.append(await _sync_one_pipeline(user["id"], pipe, tool_ids))
        except Exception as e:
            logger.exception(f"revival agent sync failed for pipeline {pipe.get('id')}: {e}")
            results.append({"pipeline": pipe.get("name"), "pipeline_id": pipe.get("id"),
                            "status": "failed", "error": str(e)})
    ok = sum(1 for r in results if r.get("status") == "synced")
    return {"ok": ok == len(results), "summary": {"synced": ok, "failed": len(results) - ok}, "results": results}


@router.get("/revival/agent/preview-prompt")
async def revival_agent_preview_prompt(
    pipeline_id: Optional[str] = Query(None),
    user: dict = Depends(current_user),
):
    """Exactly what the revival agent would be synced with right now — system
    prompt, opener and voicemail message. Mirrors /elevenlabs/agent/preview-prompt.
    Unsaved UI edits are NOT included; save first, then preview."""
    if pipeline_id:
        await assert_pipeline_access(user, pipeline_id)
    settings = await resolve_settings(user["id"], pipeline_id) or {}
    from voice_service import build_revival_system_prompt, revival_first_message, _REVIVAL_VOICEMAIL_MESSAGE
    from dialer.revival import revival_settings
    sca = settings.get("screen_call_agent") or {}
    rev = revival_settings(settings)
    return {
        "system_prompt": build_revival_system_prompt(
            sca, booking_prefs=settings.get("booking_preferences") or {}, rev=rev,
        ),
        "first_message": revival_first_message(rev),
        "voicemail_message": _REVIVAL_VOICEMAIL_MESSAGE,
    }


@router.get("/revival/preview")
async def revival_preview(
    pipeline_id: str = Query(...),
    user: dict = Depends(current_user),
):
    """Dry-run of the revival tick for one pipeline: settings echo + the list
    of candidates who would be called. Zero writes."""
    await assert_pipeline_access(user, pipeline_id)
    pipe = await db.pipelines.find_one({"id": pipeline_id, "user_id": user["id"]}, {"_id": 0})
    if not pipe:
        raise HTTPException(404, "Pipeline not found")
    settings = await resolve_settings(user["id"], pipeline_id) or {}
    from dialer.revival import revival_settings, eligible_revival_candidates, _parse_iso
    from dialer.state import now_utc
    rev = revival_settings(settings)
    cands = await eligible_revival_candidates(db, user["id"], pipeline_id, rev, limit=200)
    now = now_utc()

    def _days_since(c):
        dt = _parse_iso(c.get("appointment_at"))
        return (now - dt).days if dt else None

    return {
        "pipeline": pipe.get("name"),
        "enabled": bool(rev.get("enabled")),
        "revival_agent_id": pipe.get("revival_agent_id") or "",
        "settings": rev,
        "count": len(cands),
        "candidates": [
            {
                "id": c["id"],
                "name": f"{c.get('first_name', '')} {c.get('last_name', '')}".strip(),
                "phone": c.get("phone"),
                "appointment_at": c.get("appointment_at"),
                "days_since_appointment": _days_since(c),
                "revival_call_attempts": c.get("revival_call_attempts", 0),
                "revival_outcome": c.get("revival_outcome"),
            }
            for c in cands
        ],
    }


@router.post("/candidates/{candidate_id}/revival-call", dependencies=[Depends(candidate_write_access)])
async def manual_revival_call(candidate_id: str, user: dict = Depends(current_user)):
    """Manually place a revival call for one archived no-show right now.
    Bypasses the attempt cap / call window / daily cap (manual=True) but keeps
    the archived / final-outcome / concurrency guards. Works even while the
    revival tick is disabled — this is the prod smoke-test lever."""
    require_mover(user)
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")
    from dialer.revival import place_revival_call
    result = await place_revival_call(user["id"], candidate_id, manual=True)
    if result.get("status") == "failed":
        raise HTTPException(400, result.get("error") or "Revival call failed")
    return {"ok": result.get("status") in ("initiated", "queued"), **result}
