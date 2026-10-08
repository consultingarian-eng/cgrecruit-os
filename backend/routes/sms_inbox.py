"""Two-way SMS Inbox API.

Powers the standalone Inbox page where a recruiter can:
  • see every SMS conversation (matched candidates AND unmatched "orphan"
    numbers that texted a pipeline line but don't map to a candidate),
  • open one thread and read the full back-and-forth,
  • text a free-form reply from the office's warmed SMS number.

The underlying store is `training_sms_messages` (the two-way SMS log written by
training_sms.handle_inbound / _log_message). Matched-candidate threads also
merge the outbound `communications` log via training_sms._get_thread so
platform-sent SMS (confirmations, reminders) show inline.

Threads are keyed by the *external* phone number (the candidate/lead's number),
normalised to its last 10 digits. For an inbound message the external number is
the `sender`; for an outbound message it's the `recipient`.
"""
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from deps import db, logger, current_user, resolve_settings, assert_candidate_access, require_mover

router = APIRouter()


# ─── helpers ──────────────────────────────────────────────────────────────────

def _last10(phone: Optional[str]) -> str:
    """Normalise a phone to its last 10 digits — the thread key."""
    d = re.sub(r"\D", "", phone or "")
    return d[-10:] if len(d) >= 10 else d


def _external_phone(m: Dict[str, Any]) -> str:
    """The lead/candidate number for a logged message (not our office number)."""
    return _last10(m.get("sender") if m.get("direction") == "in" else m.get("recipient"))


def _allowed_pipeline_ids(user: dict) -> Optional[List[str]]:
    """None for super-admins (whole tenant); else the recruiter's pipeline set."""
    if user.get("role") == "super_admin":
        return None
    return user.get("pipeline_ids") or []


# ─── list threads ─────────────────────────────────────────────────────────────

@router.get("/sms/inbox")
async def list_inbox(limit: int = Query(200, le=500), user: dict = Depends(current_user)):
    """Return one row per SMS conversation, most-recently-active first.

    Each row: {phone, phone_e164, name, candidate_id, pipeline_id, last_body,
    last_direction, last_at, count, needs_reply}. `needs_reply` is true when the
    most recent message in the thread is inbound (a proxy for "unread / awaiting
    a human"). Scope: super-admins see every thread including orphans; recruiters
    see threads for candidates in their pipelines."""
    allowed = _allowed_pipeline_ids(user)

    # Pull the recent tail of the log; group in Python (kept lightweight — the
    # bell/notifications layer uses the same 'polling is fine for now' posture).
    rows = await db.training_sms_messages.find(
        {}, {"_id": 0}
    ).sort("timestamp", -1).limit(4000).to_list(4000)

    # Resolve which candidate_ids the recruiter may see (for scoping + names).
    cand_ids = {r.get("candidate_id") for r in rows if r.get("candidate_id")}
    cand_map: Dict[str, Dict[str, Any]] = {}
    if cand_ids:
        async for c in db.candidates.find(
            {"id": {"$in": list(cand_ids)}, "user_id": user["id"]},
            {"_id": 0, "id": 1, "first_name": 1, "last_name": 1, "pipeline_id": 1,
             "phone": 1, "stage": 1, "sms_opted_out": 1, "added_by_user_id": 1},
        ):
            cand_map[c["id"]] = c

    # Pipelines of THIS account — a message tied to another account's
    # candidate or pipeline never shows here.
    own_pipeline_ids = {
        p["id"] async for p in db.pipelines.find({"user_id": user["id"]}, {"_id": 0, "id": 1})
    }
    threads: Dict[str, Dict[str, Any]] = {}
    for m in rows:
        key = _external_phone(m)
        if not key:
            continue
        cand = cand_map.get(m.get("candidate_id") or "")
        pipeline_id = (cand or {}).get("pipeline_id") or m.get("pipeline_id")
        if pipeline_id and pipeline_id not in own_pipeline_ids:
            continue
        if m.get("candidate_id") and not cand and not pipeline_id:
            continue  # a candidate this account can't see, with nothing tying it here
        # Viewers see only the candidates they added; no orphan threads.
        if user.get("role") == "viewer" and (
            not cand or cand.get("added_by_user_id") != user.get("auth_user_id")
        ):
            continue

        # Scope: recruiters only see threads tied to their pipelines. Orphans
        # (no candidate, no pipeline) are super-admin only.
        if allowed is not None:
            if not pipeline_id or pipeline_id not in allowed:
                continue

        t = threads.get(key)
        if t is None:
            t = threads[key] = {
                "phone": key,
                "phone_e164": m.get("sender") if m.get("direction") == "in" else m.get("recipient"),
                "candidate_id": None,
                "name": None,
                "pipeline_id": pipeline_id,
                "stage": None,
                "opted_out": False,
                "last_body": "",
                "last_direction": "",
                "last_at": "",
                "count": 0,
            }
        t["count"] += 1
        # rows are newest-first, so the first one we see for a key is the latest.
        if not t["last_at"]:
            t["last_body"] = (m.get("body") or "")[:160]
            t["last_direction"] = m.get("direction") or ""
            t["last_at"] = m.get("timestamp") or ""
        if cand and not t["candidate_id"]:
            t["candidate_id"] = cand["id"]
            t["name"] = f"{cand.get('first_name', '')} {cand.get('last_name', '')}".strip() or None
            t["stage"] = cand.get("stage")
            t["opted_out"] = bool(cand.get("sms_opted_out"))
            if not t["pipeline_id"]:
                t["pipeline_id"] = cand.get("pipeline_id")

    items = sorted(threads.values(), key=lambda t: t["last_at"], reverse=True)[:limit]
    for t in items:
        t["needs_reply"] = t["last_direction"] == "in"
    unread = sum(1 for t in items if t["needs_reply"])
    return {"items": items, "unread": unread}


# ─── one thread ───────────────────────────────────────────────────────────────

@router.get("/sms/inbox/thread")
async def get_thread(
    phone: Optional[str] = Query(None),
    candidate_id: Optional[str] = Query(None),
    user: dict = Depends(current_user),
):
    """Full message thread for a phone number (orphan) or a candidate.

    For a matched candidate we reuse training_sms._get_thread so the outbound
    comms log (confirmations/reminders) is merged in. For an orphan phone we
    return just the raw training_sms_messages for that number."""
    from training_sms import _get_thread

    allowed = _allowed_pipeline_ids(user)

    cand = None
    if candidate_id:
        cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
        if not cand:
            raise HTTPException(404, "Candidate not found")
    elif phone:
        key = _last10(phone)
        # Try to resolve the phone to a candidate so a known number opens as a
        # named thread rather than an anonymous orphan.
        if key:
            async for c in db.candidates.find(
                {"phone": {"$regex": r"\D*".join(re.escape(d) for d in key)}, "user_id": user["id"]},
                {"_id": 0},
            ).limit(20):
                if _last10(c.get("phone")) == key:
                    cand = c
                    candidate_id = c["id"]
                    break
    else:
        raise HTTPException(400, "phone or candidate_id required")

    pipeline_id = (cand or {}).get("pipeline_id")
    if cand:
        await assert_candidate_access(user, cand)
    elif allowed is not None:
        # Unmatched numbers are owner-only (same rule as the list).
        raise HTTPException(403, "Only admins can open unmatched numbers")

    if candidate_id:
        messages = await _get_thread(db, candidate_id)
        return {
            "candidate_id": candidate_id,
            "phone": (cand or {}).get("phone"),
            "name": f"{(cand or {}).get('first_name', '')} {(cand or {}).get('last_name', '')}".strip() or None,
            "pipeline_id": pipeline_id,
            "stage": (cand or {}).get("stage"),
            "opted_out": bool((cand or {}).get("sms_opted_out")),
            "messages": messages,
        }

    # Orphan thread — no candidate. Only super-admins reach here (scope above).
    key = _last10(phone)
    raw = await db.training_sms_messages.find(
        {}, {"_id": 0, "direction": 1, "body": 1, "timestamp": 1, "sender": 1,
             "recipient": 1, "was_llm_reply": 1},
    ).sort("timestamp", 1).limit(500).to_list(500)
    messages = [
        {"direction": m.get("direction"), "body": m.get("body"),
         "timestamp": m.get("timestamp"), "was_llm_reply": m.get("was_llm_reply")}
        for m in raw if _external_phone(m) == key
    ]
    # The office number this lead last texted — used as the reply "from".
    last_in = next((m for m in reversed(raw) if _external_phone(m) == key and m.get("direction") == "in"), None)
    return {
        "candidate_id": None,
        "phone": (last_in or {}).get("sender") or phone,
        "name": None,
        "pipeline_id": None,
        "stage": None,
        "opted_out": False,
        "office_number": (last_in or {}).get("recipient") or "",
        "messages": messages,
    }


# ─── send a reply ─────────────────────────────────────────────────────────────

@router.post("/sms/inbox/send")
async def send_reply(payload: Dict[str, Any] = Body(...), user: dict = Depends(current_user)):
    """Send a free-form SMS reply.

    Body: {candidate_id?, phone?, body, from_number?}
      • With candidate_id → routed through send_candidate_sms (sender resolution,
        opt-out + STOP-clause handling, logged to communications).
      • With phone only (orphan) → sent from the office number the lead texted
        (or the default warmed sender), logged to training_sms_messages."""
    body = (payload.get("body") or "").strip()
    candidate_id = (payload.get("candidate_id") or "").strip()
    phone = (payload.get("phone") or "").strip()
    from_number = (payload.get("from_number") or "").strip()
    if not body:
        raise HTTPException(400, "body required")
    require_mover(user)  # viewers and analysts are read-only

    allowed = _allowed_pipeline_ids(user)

    # ── Matched candidate ──────────────────────────────────────────────────
    if candidate_id:
        cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
        if not cand:
            raise HTTPException(404, "Candidate not found")
        await assert_candidate_access(user, cand)
        settings = await resolve_settings(user["id"], cand.get("pipeline_id"))
        from sms_service import send_candidate_sms
        res = await send_candidate_sms(db, settings, cand, body, template_key="manual_reply")
        if res.get("status") not in ("sent",):
            raise HTTPException(400, res.get("error") or res.get("reason") or "Send failed")
        return {"ok": True, **res}

    # ── Orphan number (super-admin only) ───────────────────────────────────
    if not phone:
        raise HTTPException(400, "candidate_id or phone required")
    if allowed is not None:
        raise HTTPException(403, "Only admins can reply to unmatched numbers")

    from sms_service import is_opted_out
    from company_profile import default_twilio_number
    from voice_service import send_sms
    from training_sms import _log_message, _e164

    to = _e164(phone) or phone
    if await is_opted_out(db, to):
        raise HTTPException(400, "This number has opted out (STOP)")
    if not from_number:
        from_number = default_twilio_number()
    if not from_number:
        raise HTTPException(400, "No SMS sender configured — set TWILIO_PHONE_NUMBER")
    res = send_sms(to, body, from_number=from_number)
    if res.get("status") != "sent":
        raise HTTPException(400, res.get("error") or "Send failed")
    await _log_message(
        db, candidate_id=None, pipeline_id=None, direction="out",
        body=body, sender=from_number, recipient=to,
    )
    return {"ok": True, **res, "from_number": from_number}
