"""Shared FastAPI dependencies and singletons.

Keeping `db`, `current_user`, helpers, and `_send_stage_email` here avoids
importing the giant `server.py` from every route module (and avoids circular imports).
"""
import os
import re
import sys
import logging
from typing import Optional, Dict, Any
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from starlette.requests import HTTPConnection

ROOT_DIR = Path(__file__).parent
# backend/.env is for running the app. The test suite must never read it: it
# would hand the tests your real MONGO_URL, SETUP_TOKEN and cookie settings.
if "pytest" not in sys.modules and os.environ.get("CGRECRUIT_DOTENV", "1") != "0":
    load_dotenv(ROOT_DIR / ".env")

# ---------- Mongo singleton ----------
_mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(_mongo_url)
db = client[os.environ["DB_NAME"]]

logger = logging.getLogger("cgrecruit")

# ---------- Auth dep ----------
from auth_service import get_current_user_payload  # noqa: E402


async def get_user(user_id: str) -> Optional[Dict[str, Any]]:
    return await db.users.find_one({"id": user_id}, {"_id": 0})


def _issued_before_password_change(payload: dict, user: Dict[str, Any]) -> bool:
    """A session token minted before the account's last password change is
    dead: resetting a password must sign out whoever had the old one."""
    changed = user.get("password_changed_at")
    iat = payload.get("iat")
    if not changed or iat is None:
        return False
    try:
        from datetime import datetime, timezone
        changed_dt = datetime.fromisoformat(str(changed).replace("Z", "+00:00"))
        if changed_dt.tzinfo is None:
            changed_dt = changed_dt.replace(tzinfo=timezone.utc)
        return float(iat) < changed_dt.timestamp() - 1
    except Exception:
        return False


# Analyst accounts are "figures only": the reporting screens, and nothing that
# names, contacts or quotes a candidate. That is enforced here, inside
# current_user, so it holds for every signed-in route at once, including
# routes added later. A per-route check would hold only for the routes that
# remembered to call it. Anything not listed answers 403 for an analyst.
# Keep this list to aggregate endpoints. A route that returns
# candidate rows (names, contact details, messages, transcripts, audio, CVs,
# appointments, exports, search) must never be added here.
ANALYST_ROUTES = frozenset({
    ("GET", "/api/auth/me"),
    ("GET", "/api/pipelines"),            # office names for the report picker (trimmed for analysts)
    ("GET", "/api/settings"),             # only region_language for analysts (the time zone)
    ("GET", "/api/intelligence/dashboard"),
    ("GET", "/api/intelligence/cohort-week"),
    ("GET", "/api/intelligence/activity-week"),
    ("GET", "/api/intelligence/funnel-report"),
    ("POST", "/api/intelligence/ai-insights"),
    ("GET", "/api/intelligence/ai-insights/latest"),
    ("GET", "/api/intelligence/ai-insights/{job_id}"),
})

ANALYST_REFUSED = "Analyst accounts see reporting figures only"


def analyst_may_call(method: str, route_path: str) -> bool:
    method = (method or "").upper()
    if method == "HEAD":
        method = "GET"
    return (method, route_path or "") in ANALYST_ROUTES


def refuse_analyst(user: Dict[str, Any], connection: Optional[HTTPConnection] = None) -> None:
    """403 for an analyst outside ANALYST_ROUTES. With no connection (a direct
    call that didn't pass its request) the route is unknown, so the analyst is
    refused. Every other role passes."""
    if not is_analyst(user):
        return
    route_path, method = "", ""
    if connection is not None:
        route = connection.scope.get("route")
        route_path = getattr(route, "path", None) or connection.scope.get("path", "")
        method = connection.scope.get("method", "")
    if not analyst_may_call(method, route_path):
        raise HTTPException(status_code=403, detail=ANALYST_REFUSED)


async def current_user(
    payload: dict = Depends(get_current_user_payload),
    connection: HTTPConnection = None,
) -> Dict[str, Any]:
    """The signed-in user, with `id` set to the tenant owner.

    FastAPI passes `connection` in, and refuse_analyst uses it to keep analysts
    to ANALYST_ROUTES. Code that calls this directly should pass its Request.
    Without it the route is unknown and an analyst is refused."""
    uid = payload.get("sub")
    user = await get_user(uid)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if _issued_before_password_change(payload, user):
        raise HTTPException(status_code=401, detail="Your password was changed — sign in again")
    # Normalise role: any existing user without parent_user_id is effectively a super-admin
    # (legacy accounts created before multi-tenant were recruiters by default but own all their data).
    if not user.get("parent_user_id") and user.get("role") not in ("super_admin", "recruiter", "viewer"):
        user["role"] = "super_admin"
    if not user.get("parent_user_id"):
        # Upgrade legacy "recruiter" (no parent) → "super_admin" on the fly.
        if user.get("role") not in ("super_admin", "viewer"):
            await db.users.update_one({"id": user["id"]}, {"$set": {"role": "super_admin"}})
            user["role"] = "super_admin"
    # Expose the *real* authenticated identity separately, then override `id` to the
    # effective owner so existing data queries (`user_id=user["id"]`) scope correctly.
    user["auth_user_id"] = user["id"]
    if user.get("parent_user_id"):
        user["id"] = user["parent_user_id"]
    user.setdefault("pipeline_ids", [])
    user.pop("password_hash", None)
    refuse_analyst(user, connection)
    return user


def is_super_admin(user: Dict[str, Any]) -> bool:
    return user.get("role") == "super_admin"


def is_viewer(user: Dict[str, Any]) -> bool:
    return user.get("role") == "viewer"


def is_demo(user: Dict[str, Any]) -> bool:
    """A walk-around trial account. demo_guard.py stops it writing; callers use
    this where a READ would hand it something a trial shouldn't hold."""
    return bool(user.get("is_demo"))


def is_analyst(user: Dict[str, Any]) -> bool:
    return user.get("role") == "analyst"


def require_super_admin(user: Dict[str, Any]) -> Dict[str, Any]:
    if not is_super_admin(user):
        raise HTTPException(status_code=403, detail="Super-admin required")
    return user


def require_mover(user: Dict[str, Any]) -> Dict[str, Any]:
    """Raise 403 if the user is viewer or analyst (read-only roles)."""
    if is_viewer(user) or is_analyst(user):
        raise HTTPException(status_code=403, detail="Your account is read-only — contact your administrator")
    return user


def pipeline_scope_filter(user: Dict[str, Any]) -> Dict[str, Any]:
    """Mongo filter fragment that limits queries to the pipelines a recruiter/viewer
    is assigned to. Empty dict for super-admins (no additional restriction)."""
    if is_super_admin(user):
        return {}
    ids = user.get("pipeline_ids") or []
    return {"pipeline_id": {"$in": ids}}


def analyst_intelligence_filter(user: Dict[str, Any], requested_pipeline_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """For analyst-role users: returns the pipeline_id Mongo filter fragment that
    scopes intelligence queries to their allowed pipelines. Returns None for
    super-admins (no restriction). Raises 403 if analyst requests a pipeline
    they aren't allowed to see."""
    if not is_analyst(user):
        return None
    allowed = set(user.get("pipeline_ids") or [])
    if requested_pipeline_id:
        if requested_pipeline_id not in allowed:
            raise HTTPException(status_code=403, detail="Pipeline not in your analyst scope")
        return None  # specific pipeline already applied by caller
    if allowed:
        return {"pipeline_id": {"$in": list(allowed)}}
    return {"pipeline_id": {"$in": []}}  # no pipelines → empty result


def candidate_ownership_filter(user: Dict[str, Any]) -> Dict[str, Any]:
    """Additional Mongo filter for viewer-role users: they only see candidates
    they personally added (added_by_user_id == auth_user_id).
    Returns {} for all other roles (no additional restriction)."""
    if not is_viewer(user):
        return {}
    return {"added_by_user_id": user.get("auth_user_id")}


async def assert_pipeline_access(user: Dict[str, Any], pipeline_id: str) -> None:
    """Raise 403 if the current user (recruiter/viewer) cannot touch this pipeline."""
    if is_super_admin(user):
        return
    if not pipeline_id or pipeline_id not in (user.get("pipeline_ids") or []):
        raise HTTPException(status_code=403, detail="Pipeline not in your assignment")


async def assert_pipeline_in_tenant(user: Dict[str, Any], pipeline_id: Optional[str]) -> Dict[str, Any]:
    """The pipeline must belong to the caller's account AND be one they are
    assigned to. Returns the pipeline doc; 404 / 403 otherwise. Use it wherever
    a request names a pipeline to WRITE into (new candidates, jobs)."""
    if not pipeline_id:
        raise HTTPException(status_code=400, detail="pipeline_id is required")
    pipe = await db.pipelines.find_one({"id": pipeline_id, "user_id": user["id"]}, {"_id": 0})
    if not pipe:
        raise HTTPException(status_code=404, detail="Pipeline not found")
    await assert_pipeline_access(user, pipeline_id)
    return pipe


async def settings_write_scope(user: Dict[str, Any], pipeline_id: Optional[str]) -> Optional[str]:
    """Who may save a settings section. Returns the normalised pipeline_id
    (None = the tenant-wide global doc).

    - Read-only accounts (viewer, analyst) never write settings. These sections
      are the agent's script and the branded emails and texts every candidate
      receives.
    - No pipeline_id means the GLOBAL default, which every office inherits.
      Only the owner (super-admin) may change it.
    - A pipeline_id must be an office in the caller's account that they are
      assigned to.
    Without this, any signed-in account could rewrite the global email
    templates or an office's voice-agent prompt."""
    require_mover(user)
    if pipeline_id is not None and not isinstance(pipeline_id, str):
        raise HTTPException(status_code=400, detail="pipeline_id must be a string")
    pipeline_id = (pipeline_id or "").strip() or None
    if pipeline_id is None:
        require_super_admin(user)
        return None
    await assert_pipeline_in_tenant(user, pipeline_id)
    return pipeline_id


async def assert_candidate_access(user: Dict[str, Any], candidate: Optional[Dict[str, Any]]) -> None:
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if is_super_admin(user):
        return
    await assert_pipeline_access(user, candidate.get("pipeline_id") or "")
    # Viewers can only see their own additions.
    if is_viewer(user) and candidate.get("added_by_user_id") != user.get("auth_user_id"):
        raise HTTPException(status_code=404, detail="Candidate not found")


async def candidate_write_access(candidate_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """Route dependency for every per-candidate action (texts, emails, calls,
    stage moves, edits, archive/restore/delete, attendance…).

    The candidate must be in the caller's account AND in a pipeline they are
    assigned to (viewers: one they added), and the caller must not be a
    read-only account (viewer or analyst). Checking the account alone let a
    recruiter act on offices they aren't assigned to, and let read-only
    accounts text, email and call people. Returns the minimal candidate doc."""
    require_mover(user)
    cand = await db.candidates.find_one(
        {"id": candidate_id, "user_id": user["id"]},
        {"_id": 0, "id": 1, "user_id": 1, "pipeline_id": 1, "added_by_user_id": 1},
    )
    await assert_candidate_access(user, cand)
    return cand


async def current_super_admin(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """Like `current_user` but 403s if the caller isn't a super-admin. Use this
    dep on any endpoint that touches third-party integration keys/IDs."""
    require_super_admin(user)
    return user


# A settings field whose name ends like this is a credential. GET /settings
# never returns its value to any role, the owner included. The intake token
# (`zapier_webhook_token`) has its own owner-only endpoint,
# GET /integrations/zapier-token. The CG1 `cg1_webhook_secret` is write-only:
# the response carries `cg1_webhook_secret_set` instead, and a blank value on
# save keeps the stored one.
_SETTINGS_SECRET_NAME = re.compile(r"(secret|token|password|api_key)$", re.I)


def strip_settings_secrets(doc: Any) -> Any:
    """Blank every credential-named field of a settings doc, at any depth, and
    add `<field>_set: true/false` beside it so the UI can say one is saved."""
    if isinstance(doc, dict):
        out: Dict[str, Any] = {}
        for k, v in doc.items():
            if isinstance(k, str) and _SETTINGS_SECRET_NAME.search(k) and not isinstance(v, (dict, list)):
                out[k] = ""
                out[f"{k}_set"] = bool(v)
            else:
                out[k] = strip_settings_secrets(v)
        return out
    if isinstance(doc, list):
        return [strip_settings_secrets(v) for v in doc]
    return doc


def settings_view_for_analyst(settings_doc: Dict[str, Any]) -> Dict[str, Any]:
    """An analyst's GET /settings: the time zone their reports render in, and
    nothing else (no templates, agent scripts or staff contact details)."""
    settings_doc = settings_doc or {}
    return {
        "pipeline_id": settings_doc.get("pipeline_id"),
        "region_language": settings_doc.get("region_language") or {},
    }


def redact_settings_for_recruiter(settings_doc: Dict[str, Any]) -> Dict[str, Any]:
    """Strip third-party infrastructure config from a settings document before
    returning it to a non-super-admin. Credentials go too (strip_settings_secrets).
    Preserves recruiter-useful fields
    (company info, branded email templates, region/language, appointments,
    custom form, and the prompt-editable Screen Call Agent content).

    Recruiters CAN edit the prompt content for their own pipeline (questions,
    opening, booking, closing, additional_context, voicemail message) via
    PUT /settings/screen-call-agent?pipeline_id=... — so they MUST be able to
    SEE that content first. Previously this function scrubbed everything down
    to {agent_name, warmup_delay, sms_optout}, which made the editor render
    "0 questions" and blank textareas. Now we keep the prompt-editable
    surface and only redact ElevenLabs / Twilio infra identifiers."""
    if not settings_doc:
        return settings_doc
    s = strip_settings_secrets(dict(settings_doc))
    # Drop infra-heavy sections entirely — recruiters never edit these.
    s.pop("email_intake", None)
    s.pop("auto_dialer", None)
    s.pop("integrations", None)
    # Scrub only the infra IDs from screen_call_agent. Keep all editable
    # prompt content + agent persona / voice picker controls.
    sca = dict(s.get("screen_call_agent") or {})
    INFRA_KEYS = {
        "elevenlabs_agent_id",
        "elevenlabs_phone_number_id",
        # `voice_id` / `voice_model` / `llm_model` are recruiter-friendly
        # cosmetic dials, NOT infra — leave them visible.
    }
    for k in INFRA_KEYS:
        sca.pop(k, None)
    s["screen_call_agent"] = sca
    return s


# ---------- Settings + Communications helpers ----------
# Sections that support a per-pipeline override (everything except true tenant
# infra: email_intake, integrations, team membership). The override doc is a
# full clone of the global settings doc with `pipeline_id` set; runtime reads
# prefer the override, falling back to global if missing.
SETTINGS_OVERRIDABLE_SECTIONS = {
    "recruiter_profile",
    "screen_call_agent",
    "applicant_comms",
    "region_language",
    "appointments",
    "custom_form",
    "auto_dialer",
    "smart_scoring",
    "starter_template",
    "starter_date_overrides",
    "booking_preferences",
    "no_show_revival",
}

# Never copied into a per-office override. These are tenant-wide credentials,
# and infra that is only ever read from the global doc. A cloned copy of the
# intake token kept working after the owner rotated it.
SETTINGS_NEVER_CLONED = frozenset({"zapier_webhook_token", "integrations"})


async def get_or_create_settings(user_id: str, pipeline_id: Optional[str] = None) -> Dict[str, Any]:
    """Returns the settings doc for `user_id`. When `pipeline_id` is provided,
    returns the per-pipeline override doc — auto-creating it from the global doc
    on first access so editing in the UI feels seamless.

    NOTE: only PUT-style code paths should call this with `pipeline_id` set —
    it MUTATES the database when the override is missing. For read-only paths
    (GET /settings, runtime resolution during email/call dispatch) use
    `resolve_settings(user_id, pipeline_id)` instead, which never creates.
    """
    from models import Settings, CommunicationTemplate, get_default_templates, now_iso
    if pipeline_id:
        override = await db.settings.find_one(
            {"user_id": user_id, "pipeline_id": pipeline_id}, {"_id": 0}
        )
        if override:
            override.setdefault("pipeline_id", pipeline_id)
            return override
        # Clone the global doc into a brand-new override.
        global_doc = await get_or_create_settings(user_id, None)
        clone = {
            k: v for k, v in global_doc.items()
            if k not in SETTINGS_NEVER_CLONED and not _SETTINGS_SECRET_NAME.search(str(k))
        }
        clone.pop("_id", None)
        clone["pipeline_id"] = pipeline_id
        clone["updated_at"] = now_iso()
        await db.settings.insert_one(dict(clone))
        clone.pop("_id", None)
        return clone
    # Global doc.
    doc = await db.settings.find_one({"user_id": user_id, "pipeline_id": None}, {"_id": 0})
    if doc:
        # Legacy docs stored before the v26 schema migration may not have the
        # `pipeline_id` field; surface it explicitly so the FE can branch on it.
        doc.setdefault("pipeline_id", None)
        return doc
    s = Settings(user_id=user_id)
    defaults = {k: v.model_dump() for k, v in get_default_templates().items()}
    s.applicant_comms.templates = {k: CommunicationTemplate(**v) for k, v in defaults.items()}
    doc = s.model_dump()
    doc["pipeline_id"] = None
    await db.settings.insert_one(dict(doc))
    doc.pop("_id", None)
    doc.setdefault("pipeline_id", None)
    return doc


async def resolve_settings(user_id: str, pipeline_id: Optional[str] = None) -> Dict[str, Any]:
    """Read-only settings resolver. Returns the per-pipeline override merged on
    top of the global doc when one exists, else the global doc. Never creates.
    Use this in GET endpoints AND in every place the backend reads settings as
    part of fulfilling a candidate-related action (sending an email, scheduling
    a call, etc.) so per-pipeline overrides actually take effect at runtime.

    The returned dict always carries `pipeline_id`: null for plain global,
    pipeline_id=<id> when an override exists for the requested scope, OR null
    when the requested pipeline has no override yet (so the FE can render
    'using global defaults until you save')."""
    base = await db.settings.find_one(
        {"user_id": user_id, "pipeline_id": None}, {"_id": 0}
    ) or {}
    base.setdefault("pipeline_id", None)
    if not pipeline_id:
        return base
    override = await db.settings.find_one(
        {"user_id": user_id, "pipeline_id": pipeline_id}, {"_id": 0}
    )
    if not override:
        # No override yet — return the global doc with pipeline_id=null so the
        # FE knows there's nothing to "Reset to global default" yet.
        return base
    # Override wins for the overridable sections; everything else (email_intake,
    # integrations, etc.) keeps its global value.
    merged = dict(base)
    for k in SETTINGS_OVERRIDABLE_SECTIONS:
        if k in override:
            merged[k] = override[k]
    merged["pipeline_id"] = pipeline_id
    return merged


def normalize_phone_e164(phone: str) -> str:
    """Best-effort E.164: '(617) 555-0134' → '+16175550134' (US default), or
    '07700 900123' → '+447700900123' with PHONE_DEFAULT_COUNTRY=GB.

    The apply form accepts any 7+ digit value on purpose (typo tolerance), but
    the dial APIs require E.164 — so a naturally-typed number was stored raw,
    passed to ElevenLabs/Twilio untouched, and the promised screening call
    silently never happened. Returns the input unchanged when it can't be
    normalized confidently: intake must never destroy what the candidate typed,
    and a human can still fix a weird value the dialler can't. The rules live
    in phone_util.py.
    """
    from phone_util import normalize_phone_e164 as _norm
    return _norm(phone)


async def booking_link_or_alert(pipeline: Dict[str, Any], slot_iso: str, tz_name: str,
                                candidate: Optional[Dict[str, Any]] = None) -> str:
    """Resolve the join link for a booking: slot override → pipeline default → "".

    Never a placeholder. A made-up meeting URL used to be the final
    fallback in six booking paths — it is not a real meeting ID, so any pipeline
    with a blank appointment_link confirmed candidates into a Join button that
    opened Zoom's invalid-meeting error at interview minute, a manufactured
    no-show that read as a ghost. An empty link renders gracefully on every
    surface (the email join button and .ics location simply omit it, the var
    says the link follows shortly), and the recruiter gets ONE bell per pipeline
    per day to actually fix it.
    """
    from datetime import datetime, timedelta, timezone
    from availability_service import resolve_slot_link
    link = resolve_slot_link(pipeline or {}, slot_iso, tz_name) or ((pipeline or {}).get("appointment_link") or "").strip()
    if link:
        return link
    try:
        user_id = (pipeline or {}).get("user_id") or (candidate or {}).get("user_id") or ""
        pipeline_id = (pipeline or {}).get("id") or (candidate or {}).get("pipeline_id")
        if user_id:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            already = await db.notifications.find_one({
                "user_id": user_id, "kind": "booking.link_missing",
                "pipeline_id": pipeline_id, "created_at": {"$gte": cutoff},
            })
            if not already:
                from notifications_service import create_notification
                await create_notification(
                    user_id, "booking.link_missing",
                    f"🔗 {(pipeline or {}).get('name') or 'A pipeline'} has no meeting link",
                    body=("A candidate was just booked with no join link — their confirmation says the "
                          "link will follow. Set the pipeline's appointment link (or a slot override) "
                          "before the session, then reschedule-confirm or message them the link."),
                    link="/settings",
                    candidate_id=(candidate or {}).get("id"), pipeline_id=pipeline_id,
                )
    except Exception as e:
        logger.warning(f"booking link alert failed: {e}")
    return ""


async def log_communication(*, candidate_id: str, user_id: str, type_: str, template_key: str,
                            subject: Optional[str], body: str, to_address: str,
                            status: str, error: Optional[str] = None) -> None:
    from models import Communication
    comm = Communication(
        candidate_id=candidate_id, user_id=user_id, type=type_, template_key=template_key,
        subject=subject, body=body, to_address=to_address, status=status, error=error,
    ).model_dump()
    await db.communications.insert_one(comm)


# ---------- Duplicate-candidate guard ----------
async def find_duplicate_candidate(pipeline_id: str, email: str, phone: str) -> Optional[Dict[str, Any]]:
    """Return an existing active candidate in this pipeline that shares the same
    email or phone (last-10-digit fingerprint). Returns None when no duplicate found."""
    if not email and not phone:
        return None
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    phone_last10 = digits[-10:] if len(digits) >= 10 else digits
    query: Dict[str, Any] = {
        "pipeline_id": pipeline_id,
        "archived_at": None,
        "stage": {"$nin": ["CLOSE"]},
    }
    conditions = []
    if email:
        conditions.append({"email": email.lower()})
    if phone_last10:
        conditions.append({"phone": {"$regex": phone_last10}})
    if not conditions:
        return None
    query["$or"] = conditions
    return await db.candidates.find_one(query, {"_id": 0, "id": 1, "first_name": 1, "last_name": 1, "stage": 1})


# ---------- Stage email helper (used by candidates, public booking, email intake) ----------
async def send_stage_email(user_id: str, candidate: Dict[str, Any], template_key: str) -> Dict[str, Any]:
    """Render + dispatch a stage-based email and log a Communication row."""
    from email_service import (
        send_email_via_sendgrid, render_template, build_template_vars, get_template_for_key,
        ICS_INVITE_TEMPLATE_KEYS,
    )
    # Per-pipeline overrides: emails for a candidate use their office's templates.
    # Falls back to global if no override.
    settings = await resolve_settings(user_id, candidate.get("pipeline_id"))
    if not settings:
        settings = await get_or_create_settings(user_id)
    job = None
    if candidate.get("job_id"):
        job = await db.jobs.find_one({"id": candidate["job_id"], "user_id": user_id}, {"_id": 0})
    tpl = get_template_for_key(settings, template_key)
    # v30: per-channel toggle. Email gating happens here. SMS for warmup fires
    # as a *pre-call disclosure* via auto_dialer (a few minutes before the AI
    # dials), NOT here — different trigger, different lead time. This function
    # is email-only by design.
    if tpl.get("email_enabled") is False or tpl.get("enabled") is False:
        return {"status": "skipped", "reason": f"Template '{template_key}' email channel is off"}
    if not tpl["body"]:
        return {"status": "skipped", "reason": f"No template for key '{template_key}'"}
    if not candidate.get("email"):
        return {"status": "skipped", "reason": "Candidate has no email"}
    vars_map = build_template_vars(candidate, settings, job)
    subject = render_template(tpl["subject"], vars_map)
    body = render_template(tpl["body"], vars_map)
    from company_profile import email_reply_domain
    reply_domain = email_reply_domain()
    reply_to = f"reply+{candidate['id']}@{reply_domain}" if (candidate.get("id") and reply_domain) else None
    # Calendar-invite (.ics) attachments need the pipeline's slot length, which
    # lives on the Pipeline doc (not the candidate or settings). Only fetch it
    # for the booking-confirmation/reschedule keys that actually attach an .ics —
    # no need to add a query to every other stage email.
    appointment_duration_minutes = None
    if template_key in ICS_INVITE_TEMPLATE_KEYS and candidate.get("appointment_at"):
        pipeline = await db.pipelines.find_one(
            {"id": candidate.get("pipeline_id")}, {"_id": 0, "appointment_duration_minutes": 1}
        )
        appointment_duration_minutes = (pipeline or {}).get("appointment_duration_minutes")
    res = await send_email_via_sendgrid(
        candidate["email"], subject, body,
        candidate=candidate, settings=settings, template_key=template_key,
        reply_to=reply_to,
        appointment_duration_minutes=appointment_duration_minutes,
    )
    await log_communication(
        candidate_id=candidate["id"], user_id=user_id, type_="email", template_key=template_key,
        subject=subject, body=body, to_address=candidate["email"], status=res.get("status", "failed"),
        error=res.get("error"),
    )
    return {"subject": subject, "body": body, **res}


async def send_stage_comms(user_id: str, candidate: Dict[str, Any], template_key: str) -> Dict[str, Any]:
    """Send both the email AND SMS for a template key in one call.
    Respects per-channel toggles (email_enabled / sms_enabled) and only sends
    if the candidate has the relevant contact info. Safe to call from any trigger."""
    from email_service import get_template_for_key, build_template_vars, render_template
    from sms_service import send_candidate_sms
    email_res = await send_stage_email(user_id, candidate, template_key)
    sms_res: Dict[str, Any] = {"status": "skipped", "reason": "no phone"}
    if candidate.get("phone"):
        try:
            settings = await resolve_settings(user_id, candidate.get("pipeline_id"))
            tpl = get_template_for_key(settings, template_key) or {}
            if tpl.get("sms_enabled") is False or tpl.get("enabled") is False:
                sms_res = {"status": "skipped", "reason": f"Template '{template_key}' SMS channel is off"}
            else:
                sms_body_tpl = tpl.get("sms_body") or ""
                if sms_body_tpl:
                    job = None
                    if candidate.get("job_id"):
                        job = await db.jobs.find_one({"id": candidate["job_id"]}, {"_id": 0})
                    body = render_template(sms_body_tpl, build_template_vars(candidate, settings, job))
                    sms_res = await send_candidate_sms(db, settings, candidate, body, template_key=template_key)
                else:
                    sms_res = {"status": "skipped", "reason": "no sms_body in template"}
        except Exception as e:
            logger.warning(f"send_stage_comms sms ({template_key}) failed: {e}")
            sms_res = {"status": "failed", "error": str(e)}
    return {"email": email_res, "sms": sms_res}


async def maybe_fire_screening_retry(
    user_id: str, candidate_id: str, outcome: Optional[str] = None,
) -> Dict[str, Any]:
    """If the candidate's last screening attempt failed (no_answer / didnt_connect /
    incomplete_info) AND they haven't exceeded the 3-attempt cap, send the screening
    retry email + SMS with a one-click link to finish online (chat or web voice).
    Idempotent — bumps `screening_attempts` and stamps `screening_retry_sent_at` so
    each failed attempt only triggers ONE retry message.

    `outcome` is the call result the CALLER just observed. Pass it whenever you
    know it: the stored `screening_status` is a shared mutable field that two
    other writers overwrite within milliseconds of a failed call, so reading it
    here loses the race and silently skips the send (see the gate below)."""
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user_id}, {"_id": 0})
    if not cand:
        return {"status": "skipped", "reason": "candidate not found"}
    # A booked candidate must NEVER get "we missed you on the screening call —
    # start your screening online". One candidate self-booked from the portal
    # while their fallback call was mid-flight, and the call's follow-up sent
    # them the retry text SIXTY SECONDS after the booking confirmation. Their screening
    # continues via the gate-check thread; this message is for people with
    # nothing booked and nothing in progress.
    if cand.get("appointment_at"):
        return {"status": "skipped", "reason": "already booked — gate check owns any remaining screening"}
    status = cand.get("screening_status")
    # Terminal and paused states always win, whatever the caller observed: an
    # approved/rejected candidate is done, and a recruiter's pause must not be
    # talked over by a call that was already ringing when they hit pause.
    if status in ("approved", "rejected", "paused"):
        return {"status": "skipped", "reason": f"status '{status}' not eligible"}
    # Otherwise trust the caller's observed outcome over the stored status.
    # WHY: `schedule_dnd_retry` writes screening_status='queued' ~25s after a
    # voicemail, and `maybe_schedule_incomplete_retry` writes 'queued' again
    # when it books the next attempt. Both routinely land before anyone asks
    # this question, so re-reading the field returned 'queued' — not in the
    # eligible list — and the "we missed you" text was skipped. It then only
    # went out once the whole call sequence was exhausted — often many hours
    # after the missed call, and for some candidates never at all.
    effective = outcome or status
    if effective not in ("no_answer", "didnt_connect", "incomplete_info"):
        return {"status": "skipped", "reason": f"outcome '{effective}' not eligible"}
    attempts = int(cand.get("screening_attempts") or 0)
    if attempts >= 3:
        return {"status": "skipped", "reason": "max 3 attempts reached"}
    # Only send once ever — subsequent call retries should not re-send comms.
    if cand.get("screening_retry_sent_at"):
        return {"status": "skipped", "reason": "retry already sent"}

    from models import now_iso
    new_attempts = attempts + 1
    await db.candidates.update_one(
        {"id": candidate_id},
        {"$set": {
            "screening_attempts": new_attempts,
            "screening_retry_sent_at": now_iso(),
            "updated_at": now_iso(),
        }},
    )
    refreshed = await db.candidates.find_one({"id": candidate_id}, {"_id": 0})
    email_res = await send_stage_email(user_id, refreshed, "screening_retry")
    # Also fire SMS if the template has a body and candidate has phone
    sms_res: Dict[str, Any] = {"status": "skipped", "reason": "no phone or no sms_body"}
    try:
        if refreshed.get("phone"):
            from sms_service import send_candidate_sms
            from email_service import build_template_vars, render_template, get_template_for_key
            settings = await resolve_settings(user_id, refreshed.get("pipeline_id"))
            if not settings:
                settings = await get_or_create_settings(user_id)
            job = None
            if refreshed.get("job_id"):
                job = await db.jobs.find_one({"id": refreshed["job_id"]}, {"_id": 0})
            tpl = get_template_for_key(settings, "screening_retry")
            if tpl.get("sms_enabled") is False or tpl.get("enabled") is False:
                sms_res = {"status": "skipped", "reason": "screening_retry SMS channel is off"}
            elif tpl.get("sms_body"):
                vars_map = build_template_vars(refreshed, settings, job)
                sms_body = render_template(tpl["sms_body"], vars_map)
                sms_res = await send_candidate_sms(
                    db, settings, refreshed, sms_body, template_key="screening_retry",
                )
    except Exception as e:
        logger.warning(f"retry sms failed: {e}")
    return {"status": "sent", "attempts": new_attempts, "email": email_res.get("status"), "sms": sms_res.get("status")}


# ---------- Twilio webhook authenticity ----------
async def twilio_signature_invalid(request: Request) -> bool:
    """True when a Twilio webhook can't prove Twilio sent it.

    Twilio cannot present a JWT, so the signature IS the authentication on
    these endpoints. Fails CLOSED (see webhook_auth.py): no TWILIO_AUTH_TOKEN,
    a missing or wrong signature, or an error while checking all count as
    invalid. Local development without Twilio can opt out with
    ALLOW_UNSIGNED_TWILIO_WEBHOOKS=true."""
    from webhook_auth import twilio_request_is_authentic

    return not await twilio_request_is_authentic(request)
