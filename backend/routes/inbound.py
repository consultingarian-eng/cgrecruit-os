"""Inbound receptionist agent routes.

- POST /inbound/agent/sync           — create/sync the per-office ElevenLabs
                                       inbound agent (stores pipeline.inbound_agent_id)
                                       and assign it to the office's phone number.
- GET  /inbound/agent/preview-prompt — the exact prompt the agent would sync with.

The inbound agent answers calls to an office line from booked / about-to-start
candidates who want the address or to reschedule. See voice_service inbound
functions + POST /webhooks/elevenlabs/conversation-init.
"""
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from deps import (
    db, logger, current_user, resolve_settings,
    assert_pipeline_access, require_super_admin, require_mover,
)

router = APIRouter()


def _init_webhook_url() -> str:
    base = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
    return f"{base}/api/webhooks/elevenlabs/conversation-init" if base else ""


async def _resolve_office_context(user_id: str, pipe: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, str]:
    """Office address + Maps link + phone_number_id for this pipeline."""
    from email_service import office_key_from_pipeline, office_maps_link
    okey = office_key_from_pipeline(pipe)
    # Address: pipeline-level first, else the starter template's office_address,
    # else the office's address in the company profile.
    office_address = (pipe.get("office_address") or "").strip()
    if not office_address:
        office_address = ((settings.get("starter_template") or {}).get("office_address") or "").strip()
    if not office_address:
        from company_profile import office_address as _profile_address
        office_address = _profile_address(okey)
    sca = settings.get("screen_call_agent") or {}
    phone_number_id = (
        (pipe.get("elevenlabs_phone_number_id_override") or "").strip()
        or (sca.get("elevenlabs_phone_number_id") or "").strip()
    )
    return {
        "office_key": okey,
        "office_address": office_address,
        "maps_link": office_maps_link(okey),
        "phone_number_id": phone_number_id,
    }


async def _sync_one_pipeline(user_id: str, pipe: Dict[str, Any], tool_ids: list) -> Dict[str, Any]:
    from voice_service import (
        create_inbound_agent_for_pipeline, sync_inbound_agent_to_elevenlabs,
        assign_inbound_agent_to_number,
    )
    settings = await resolve_settings(user_id, pipe["id"]) or {}
    sca = settings.get("screen_call_agent") or {}
    booking_prefs = settings.get("booking_preferences") or {}
    voice_id = pipe.get("voice_id_override") or sca.get("voice_id") or None
    ctx = await _resolve_office_context(user_id, pipe, settings)
    init_url = _init_webhook_url()
    backend_url = (
        os.environ.get("BACKEND_URL", "").rstrip("/").removesuffix("/api")
        or os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
    )
    post_call_url = f"{backend_url}/api/webhooks/elevenlabs/post-call" if backend_url else ""

    agent_id = (pipe.get("inbound_agent_id") or "").strip()
    created = False
    if not agent_id:
        res = create_inbound_agent_for_pipeline(
            settings, pipe, tool_ids=tool_ids,
            office_address=ctx["office_address"], maps_link=ctx["maps_link"],
            init_webhook_url=init_url, post_call_webhook_url=post_call_url,
        )
        agent_id = (res or {}).get("agent_id") or ""
        if not agent_id:
            return {"pipeline": pipe.get("name"), "pipeline_id": pipe["id"], "status": "failed",
                    "error": "ElevenLabs agent creation failed — check ELEVENLABS_API_KEY and logs"}
        created = True
        await db.pipelines.update_one(
            {"id": pipe["id"], "user_id": user_id},
            {"$set": {"inbound_agent_id": agent_id}},
        )

    sync_res = sync_inbound_agent_to_elevenlabs(
        agent_id, sca, voice_id=voice_id, tool_ids=tool_ids, booking_prefs=booking_prefs,
        office_address=ctx["office_address"], maps_link=ctx["maps_link"], init_webhook_url=init_url,
    )

    # Point the office's phone number at this agent for inbound calls.
    assign_res = {"status": "skipped", "reason": "no phone_number_id on pipeline/settings"}
    if ctx["phone_number_id"]:
        assign_res = assign_inbound_agent_to_number(ctx["phone_number_id"], agent_id)

    return {
        "pipeline": pipe.get("name"), "pipeline_id": pipe["id"],
        "agent_id": agent_id, "created": created,
        "office_address_set": bool(ctx["office_address"]),
        "maps_link": ctx["maps_link"] or None,
        "assign": assign_res,
        **sync_res,
    }


@router.post("/inbound/agent/sync")
async def inbound_agent_sync(
    payload: Dict[str, Any] = Body(default={}),
    user: dict = Depends(current_user),
):
    """Create + sync the inbound receptionist agent(s) and assign them to their
    office phone numbers. With pipeline_id in the body, only that pipeline;
    without, all pipelines (super-admin only). Mirrors /revival/agent/sync."""
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

    from voice_service import upsert_elevenlabs_tools, ensure_workspace_init_webhook
    public_base = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
    tool_map = upsert_elevenlabs_tools(public_base)
    tool_ids = [tid for k, tid in (tool_map or {}).items() if tid and k != "error"]

    # Point the workspace conversation-initiation webhook at our endpoint (once,
    # idempotent). Only the inbound agents opt in to using it via their override
    # flag; screening/revival agents are unaffected.
    init_webhook_res = ensure_workspace_init_webhook(_init_webhook_url())

    results = []
    for pipe in pipelines:
        try:
            results.append(await _sync_one_pipeline(user["id"], pipe, tool_ids))
        except Exception as e:
            logger.exception(f"inbound agent sync failed for pipeline {pipe.get('id')}: {e}")
            results.append({"pipeline": pipe.get("name"), "pipeline_id": pipe.get("id"),
                            "status": "failed", "error": str(e)})
    ok = sum(1 for r in results if r.get("status") == "synced")
    return {"ok": ok == len(results), "summary": {"synced": ok, "failed": len(results) - ok},
            "init_webhook": init_webhook_res, "results": results}


@router.get("/inbound/agent/preview-prompt")
async def inbound_agent_preview_prompt(
    pipeline_id: Optional[str] = Query(None),
    user: dict = Depends(current_user),
):
    """Exactly what the inbound agent would sync with right now."""
    if pipeline_id:
        await assert_pipeline_access(user, pipeline_id)
    settings = await resolve_settings(user["id"], pipeline_id) or {}
    pipe = {}
    if pipeline_id:
        pipe = await db.pipelines.find_one({"id": pipeline_id, "user_id": user["id"]}, {"_id": 0}) or {}
    ctx = await _resolve_office_context(user["id"], pipe, settings)
    from voice_service import build_inbound_system_prompt, INBOUND_FIRST_MESSAGE, _convert_placeholders_to_elevenlabs
    sca = settings.get("screen_call_agent") or {}
    return {
        "system_prompt": build_inbound_system_prompt(
            sca, booking_prefs=settings.get("booking_preferences") or {},
            office_address=ctx["office_address"], maps_link=ctx["maps_link"],
        ),
        "first_message": _convert_placeholders_to_elevenlabs(INBOUND_FIRST_MESSAGE),
        "office_key": ctx["office_key"],
        "office_address": ctx["office_address"],
        "maps_link": ctx["maps_link"],
        "phone_number_id": ctx["phone_number_id"],
        "inbound_agent_id": pipe.get("inbound_agent_id") or "",
    }
