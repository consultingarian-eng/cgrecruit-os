"""CGRecruit - main FastAPI server."""
from fastapi import FastAPI, APIRouter, HTTPException, Depends, UploadFile, File, Form, Body, Request, Response, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.middleware.cors import CORSMiddleware
import os
import company_profile
import json
import logging
import requests
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone, timedelta, date

# Shared singletons (mongo client/db, current_user dep, helpers, _send_stage_email)
# live in deps.py so route modules can import them without pulling all of server.
import asyncio
from pubsub import subscribe, unsubscribe, broadcast
from app_tz import app_zone, default_tz_name  # APP_TIMEZONE (noqa: F401)

# Cap concurrent AI resume parses so bulk uploads don't OOM the server
_upload_parse_sem = asyncio.Semaphore(3)
from deps import (
    db, client, current_user, current_super_admin, get_user, get_or_create_settings,
    resolve_settings,
    log_communication, send_stage_email as _send_stage_email, send_stage_comms as _send_stage_comms,
    is_super_admin, is_viewer, is_analyst, is_demo, require_super_admin, require_mover,
    twilio_signature_invalid,
    pipeline_scope_filter, candidate_ownership_filter, analyst_intelligence_filter,
    assert_pipeline_access, assert_candidate_access, redact_settings_for_recruiter,
    assert_pipeline_in_tenant, candidate_write_access,
    settings_write_scope, strip_settings_secrets, settings_view_for_analyst,
)

# ----- App -----
app = FastAPI(title="CGRecruit")
api = APIRouter(prefix="/api")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("cgrecruit")

# ----- Imports (after env load via deps) -----
from models import (
    User, UserCreate, UserLogin,
    Pipeline, PipelineCreate,
    Job, JobCreate,
    Candidate, CandidateCreate, CandidateMove,
    Conversation, Communication,
    Settings, RecruiterProfile, ScreenCallAgentSettings, ApplicantCommsSettings,
    RegionLanguageSettings, AppointmentSettings, CustomFormSettings, CommunicationTemplate,
    BookingPreferences,
    STAGES, ATTENDANCE_VALUES, appointment_has_lapsed, local_slot_label,
    get_default_templates, COMM_TEMPLATE_KEYS, new_id, now_iso,
)
from pydantic import BaseModel
from auth_service import hash_password, verify_password, create_token, get_current_user_payload
from ai_service import extract_resume_text, parse_resume, smart_score, summarize_call_transcript
from email_service import send_email_via_sendgrid, render_template, build_template_vars, get_template_for_key
from voice_service import (
    initiate_caller_id_verification, list_verified_caller_ids, list_twilio_phone_numbers,
    send_sms, initiate_elevenlabs_outbound_call, fetch_elevenlabs_conversation,
    list_elevenlabs_voices, get_elevenlabs_agent, sync_agent_to_elevenlabs,
    build_agent_system_prompt, list_elevenlabs_phone_numbers,
    fetch_elevenlabs_conversation_audio,
)
from auto_dialer import (
    start_scheduler, stop_scheduler,
    schedule_call_with_window, batch_dial_pipeline,
)
from training_sms import router as training_sms_router, scheduler_tick as training_sms_tick
from email_replies import router as email_replies_router


# ===== Timezone helper =====
try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _ET = app_zone()
except ImportError:
    import pytz as _pytz
    _ET = _pytz.timezone(default_tz_name())

import re as _re_tz


def _parse_training_ts(ts: str) -> datetime:
    """Parse training_start_at to an ET datetime for display in emails.

    Older CG1 builds sent UTC (trailing Z); newer builds send a naive local ISO.
    Both are normalised to ET so the formatted time is always correct regardless
    of when the mobile app was last updated.
    """
    ts = ts.strip()
    has_tz = bool(_re_tz.search(r'(Z|[+-]\d{2}:?\d{2})$', ts))
    if has_tz:
        dt_utc = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt_utc.astimezone(_ET)
    # Naive ISO from new mobile builds — already local (ET) time.
    return datetime.fromisoformat(ts)


# ===== Health =====
@api.get("/")
async def root():
    return {"app": "CGRecruit", "status": "ok"}


def _resolve_git_sha() -> str:
    """Deploy fingerprint. Railway injects RAILWAY_GIT_COMMIT_SHA; fall back to
    the local git checkout (works on Railway too — /app is a git repo)."""
    sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA") or ""
    if not sha:
        try:
            import subprocess
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5,
                cwd=os.path.dirname(os.path.abspath(__file__)),
            ).stdout.strip()
        except Exception:
            sha = ""
    return sha[:12]


_GIT_SHA = _resolve_git_sha()


@api.get("/version")
async def version():
    """Which code is live — lets a deploy be verified without auth (the SPA
    fallback swallows unknown /api GETs, so absence of `sha` = old build)."""
    return {"app": "CGRecruit", "sha": _GIT_SHA or None}


# ===== Auth =====
@api.post("/auth/register")
async def register(payload: UserCreate, response: Response, request: Request):
    # Not self-serve. This app holds one company's live recruitment pipeline,
    # and an open route let anyone on the internet mint an account — which came
    # out as a super-admin, because a user with no parent_user_id is treated as
    # a tenant owner (see current_user in deps.py). Accounts are created by an
    # existing super-admin. The one exception is a deployment with no users at
    # all, so a fresh install can still be bootstrapped.
    if not await db.users.count_documents({}, limit=1):
        # First account = owner of the whole deployment. Between deploying
        # and creating it, anyone who found the address could claim it, so
        # the bootstrap needs SETUP_TOKEN (Railway -> Variables), sent as the
        # X-Setup-Token header by .claude/skills/setup/create_owner.py.
        import hmac as _hmac
        setup_token = (os.environ.get("SETUP_TOKEN") or "").strip()
        if not setup_token:
            raise HTTPException(
                status_code=503,
                detail="Set SETUP_TOKEN on the server to create the first (owner) account.",
            )
        sent = (request.headers.get("x-setup-token") or "").strip()
        if not sent or not _hmac.compare_digest(sent.encode("utf-8"), setup_token.encode("utf-8")):
            raise HTTPException(status_code=403, detail="Wrong or missing setup token.")
    else:
        caller = None
        try:
            from auth_service import get_current_user_payload
            caller = await current_user(await get_current_user_payload(
                request, authorization=request.headers.get("Authorization")
            ))
        except Exception:
            caller = None
        if not caller or not is_super_admin(caller):
            raise HTTPException(
                status_code=403,
                detail="Accounts are created by an administrator — ask yours for a login.",
            )
    existing = await db.users.find_one({"email": payload.email.lower()}, {"_id": 0})
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    user = User(email=payload.email.lower(), name=payload.name, company=payload.company or "")
    doc = user.model_dump()
    doc["password_hash"] = hash_password(payload.password)
    await db.users.insert_one(doc)
    await get_or_create_settings(user.id)
    token = create_token(user.id, user.email)
    # Set httpOnly cookie for SPA flows; also return token in body for API/test
    # consumers (Bearer header path is the additive fallback).
    from auth_service import set_auth_cookie
    set_auth_cookie(response, token)
    return {"token": token, "user": user.model_dump()}


# bcrypt hash of a random string: an unknown email costs the same time as a
# wrong password, so response timing can't reveal which accounts exist.
def _make_dummy_hash() -> str:
    import bcrypt as _bcrypt
    import secrets as _secrets
    return _bcrypt.hashpw(_secrets.token_bytes(16), _bcrypt.gensalt(rounds=12)).decode("utf-8")


_LOGIN_DUMMY_HASH = _make_dummy_hash()


@api.post("/auth/login")
async def login(payload: UserLogin, response: Response, request: Request):
    import login_throttle
    keys = login_throttle.keys_for(payload.email, request.headers.get("x-forwarded-for"),
                                   request.client.host if request.client else None)
    if login_throttle.is_blocked(keys):
        raise HTTPException(status_code=429, detail="Too many failed sign-in attempts. Wait 15 minutes and try again.")
    doc = await db.users.find_one({"email": payload.email.lower()}, {"_id": 0})
    hashed = (doc or {}).get("password_hash") or _LOGIN_DUMMY_HASH
    if not await asyncio.to_thread(verify_password, payload.password, hashed) or not doc or not doc.get("password_hash"):
        login_throttle.record_failure(keys)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    login_throttle.clear([keys["account"]])
    token = create_token(doc["id"], doc["email"])
    user_clean = {k: v for k, v in doc.items() if k != "password_hash"}
    from auth_service import set_auth_cookie
    set_auth_cookie(response, token)
    return {"token": token, "user": user_clean}


@api.post("/auth/logout")
async def logout(response: Response):
    """Clears the httpOnly auth cookie. Frontend should also drop any cached
    user state and bounce to /login."""
    from auth_service import clear_auth_cookie
    clear_auth_cookie(response)
    return {"ok": True}


class ForgotPasswordPayload(BaseModel):
    email: str

class ResetPasswordPayload(BaseModel):
    token: str
    new_password: str

@api.post("/auth/forgot-password")
async def forgot_password(payload: ForgotPasswordPayload):
    """Generate a password-reset token and email it. Always returns 200 so we
    don't reveal whether an email is registered."""
    import secrets
    email = payload.email.lower().strip()
    user_doc = await db.users.find_one({"email": email}, {"_id": 0, "id": 1})
    if user_doc:
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        await db.password_reset_tokens.delete_many({"email": email})
        await db.password_reset_tokens.insert_one({
            "email": email,
            "token_hash": _reset_token_hash(token),
            "expires_at": expires_at,
        })
        base = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
        reset_link = f"{base}/reset-password?token={token}"
        subject = "Reset your CGRecruit password"
        body = (
            f"Hi,\n\nWe received a request to reset your CGRecruit password.\n\n"
            f"Click the link below to set a new password (valid for 1 hour):\n\n"
            f"{reset_link}\n\n"
            f"If you didn't request this, you can safely ignore this email.\n\n"
            f"— {company_profile.company_name()}"
        )
        from email_service import send_email_via_sendgrid
        await send_email_via_sendgrid(email, subject, body)
    return {"ok": True}


def _reset_token_hash(token: str) -> str:
    """Reset tokens are stored hashed, so a database read can't be turned
    into a password reset."""
    import hashlib
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _reset_expired(expires_at, now: Optional[datetime] = None) -> bool:
    """True when a reset record has no usable expiry or it has passed.
    Mongo hands datetimes back naive (UTC) unless the client is tz-aware."""
    if not isinstance(expires_at, datetime):
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) > expires_at


@api.post("/auth/reset-password")
async def reset_password(payload: ResetPasswordPayload):
    """Validate the reset token and update the user's password."""
    from auth_service import MIN_PASSWORD_LENGTH
    token_hash = _reset_token_hash(payload.token)
    record = await db.password_reset_tokens.find_one({"token_hash": token_hash})
    if not record:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link.")
    if _reset_expired(record.get("expires_at")):
        await db.password_reset_tokens.delete_one({"token_hash": token_hash})
        raise HTTPException(status_code=400, detail="Reset link has expired. Please request a new one.")
    if len(payload.new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    new_hash = hash_password(payload.new_password)
    # password_changed_at signs out every session issued before now (deps.current_user).
    await db.users.update_one(
        {"email": record["email"]},
        {"$set": {"password_hash": new_hash, "password_changed_at": datetime.now(timezone.utc).isoformat()}},
    )
    await db.password_reset_tokens.delete_many({"email": record["email"]})
    return {"ok": True}


@api.get("/auth/me")
async def me(user: dict = Depends(current_user)):
    # Return the *real* authenticated user (not the enriched owner). `auth_user_id`
    # is the login's own id; `id` in `user` has been swapped to owner_id for data
    # queries elsewhere in the app.
    out = {k: v for k, v in user.items() if k != "password_hash"}
    out["id"] = user.get("auth_user_id") or user.get("id")
    out["owner_id"] = user["id"]  # the tenant/super-admin id
    return out


# ===== Pipelines =====
@api.get("/pipelines", response_model=List[Pipeline])
async def list_pipelines(user: dict = Depends(current_user)):
    q = {"user_id": user["id"]}
    if not is_super_admin(user):
        q["id"] = {"$in": user.get("pipeline_ids") or []}
    # Analysts only need office names for the report picker. The full doc has
    # interview meeting links, the office address and phone numbers.
    proj = ({"_id": 0, "id": 1, "user_id": 1, "name": 1, "description": 1,
             "public_slug": 1, "cg1_office_key": 1, "created_at": 1}
            if is_analyst(user) else {"_id": 0})
    rows = await db.pipelines.find(q, proj).to_list(200)
    return rows


@api.post("/pipelines", response_model=Pipeline)
async def create_pipeline(payload: PipelineCreate, user: dict = Depends(current_user)):
    require_super_admin(user)
    slug = (payload.public_slug or payload.name or "").lower().replace(",", "").replace(" ", "-").strip("-")
    p = Pipeline(
        user_id=user["id"], name=payload.name, description=payload.description or "",
        twilio_phone_number=payload.twilio_phone_number or "",
        public_slug=slug or new_id()[:8],
    )
    # Auto-provision a dedicated ElevenLabs agent cloned from the tenant's default
    # Screen Call Agent so every new office gets its own isolated voice + prompt.
    try:
        from voice_service import clone_agent_for_pipeline
        settings = await get_or_create_settings(user["id"])
        result = clone_agent_for_pipeline(settings, p.name)
        if result.get("agent_id"):
            p.elevenlabs_agent_id_override = result["agent_id"]
    except Exception as e:
        logger.warning(f"pipeline auto-clone agent failed: {e}")
    # Default the outbound-calling number to TWILIO_PHONE_NUMBER unless the
    # recruiter explicitly set one (can be overridden later per-pipeline).
    if not p.twilio_phone_number:
        from company_profile import default_twilio_number
        p.twilio_phone_number = default_twilio_number()
    await db.pipelines.insert_one(p.model_dump())
    # Immediately clone the current settings into a per-pipeline doc so this
    # office starts fully independent (its own company name, prompts, etc.)
    # rather than inheriting lazily from whatever global looks like later.
    await get_or_create_settings(user["id"], p.id)
    return p


@api.put("/pipelines/{pipeline_id}", response_model=Pipeline)
async def update_pipeline(pipeline_id: str, payload: Dict[str, Any] = Body(...), user: dict = Depends(current_user)):
    require_mover(user)  # read-only accounts can't change an office's settings
    await assert_pipeline_access(user, pipeline_id)
    # Drop fields the model doesn't know about to keep the doc clean.
    allowed = set(Pipeline.model_fields.keys()) - {"id", "user_id", "created_at"}
    update = {k: v for k, v in payload.items() if k in allowed}
    if "name" in update or "public_slug" in update:
        update["public_slug"] = (update.get("public_slug") or update.get("name") or "").lower().replace(",", "").replace(" ", "-").strip("-")
    # Recruiters MUST NOT overwrite the deeper infra plumbing — picking the
    # wrong elevenlabs_agent_id or calling_mode can break outbound calls
    # entirely. They CAN change their own caller-ID number and pick a different
    # voice for their pipeline (those are reversible cosmetic choices).
    if not is_super_admin(user):
        for k in ("elevenlabs_agent_id_override", "elevenlabs_phone_number_id_override",
                  "calling_mode"):
            update.pop(k, None)
    # SAFETY NET: refuse to silently overwrite availability_rules /
    # availability_blackouts with a value identical to the current DB value.
    # Several frontend forms historically did `{...active, ...draft}` which
    # ALSO sent stale slot arrays back, silently undoing any slot edits made
    # in another tab between page-load and save. Now: if the client sends a
    # value that's identical to what's already stored, we drop it from the
    # update (no-op). If the client sends something DIFFERENT, we honour it
    # (the AvailabilityManager genuinely edits these fields).
    PROTECTED_SLOT_FIELDS = ("availability_rules", "availability_blackouts")
    if any(k in update for k in PROTECTED_SLOT_FIELDS):
        current = await db.pipelines.find_one(
            {"id": pipeline_id, "user_id": user["id"]},
            {"_id": 0, **{k: 1 for k in PROTECTED_SLOT_FIELDS}},
        ) or {}
        for k in PROTECTED_SLOT_FIELDS:
            if k in update and update[k] == current.get(k):
                # Identical → almost certainly a stale snapshot echo, not an
                # intentional edit. Drop it so we don't risk undoing edits
                # from another tab.
                update.pop(k, None)
    if not update:
        raise HTTPException(400, "Nothing to update")
    res = await db.pipelines.find_one_and_update(
        {"id": pipeline_id, "user_id": user["id"]},
        {"$set": update},
        return_document=True,
        projection={"_id": 0},
    )
    if not res:
        raise HTTPException(404, "Pipeline not found")
    # Auto-sync the pipeline's dedicated ElevenLabs agent so prompt-override changes
    # take effect on the next call without the recruiter having to click Sync.
    try:
        await _autosync_pipeline_agent(user["id"], res)
    except Exception as e:
        logger.warning(f"pipeline auto-sync failed: {e}")
    return res


from email_service import office_maps_link, office_apple_maps_link  # per-office map links (company profile)


def _starter_date_override(settings: Dict[str, Any], training_start_at: str) -> Dict[str, Any]:
    """Starter-template fields that apply only to one specific intake date.

    A one-off change to a single cohort's training hours (an office event, a
    client visit) used to mean editing the pipeline's starter template and
    remembering to put it back — and anyone booked onto a LATER start date in
    the meantime silently inherited the exception. Keying it on the start date
    means it only ever reaches the cohort it was written for, and expires on
    its own once that date is past.

    Shape, on the settings doc (global or per-pipeline):
        starter_date_overrides: {"2026-09-21": {"tuesday_start": "9:30 AM"}}

    The date key is the ET calendar date. `training_start_at` is UTC-aware from
    some booking doors and naive-local from others, so it goes through
    `_parse_training_ts` — the same normaliser the email body uses — rather
    than being sliced, or the two would disagree across midnight.
    """
    overrides = (settings or {}).get("starter_date_overrides") or {}
    if not overrides or not training_start_at:
        return {}
    try:
        key = _parse_training_ts(training_start_at).strftime("%Y-%m-%d")
    except Exception:
        key = training_start_at[:10]
    return {k: v for k, v in (overrides.get(key) or {}).items() if v}


async def _send_starter_email_direct(
    user_id: str,
    candidate_id: str,
    training_start_at: str = "",
    extra_template: Optional[dict] = None,
) -> Dict[str, Any]:
    """Send the starter/training email directly from CGRecruit using the pipeline's
    Starter Email Template from Settings. No dependency on CG1 being up."""
    try:
        cand = await db.candidates.find_one({"id": candidate_id}, {"_id": 0})
        if not cand or not cand.get("email"):
            return {"status": "skipped", "reason": "no candidate email"}

        pipeline_id = cand.get("pipeline_id")
        settings = await resolve_settings(user_id, pipeline_id)
        stored_tpl = (settings.get("starter_template") or {})
        # Fall back to global template for any field that's blank in the pipeline
        # override — handles pipelines that were saved before a new field was added.
        if pipeline_id and settings.get("pipeline_id"):
            global_settings = await db.settings.find_one(
                {"user_id": user_id, "pipeline_id": None}, {"_id": 0}
            ) or {}
            global_tpl = global_settings.get("starter_template") or {}
            stored_tpl = {**global_tpl, **{k: v for k, v in stored_tpl.items() if v}}
        extra = extra_template or {}
        # Use hardcoded defaults as baseline so any unset field has a sensible value.
        pipe_doc = await db.pipelines.find_one({"id": pipeline_id}, {"_id": 0, "cg1_office_key": 1, "public_slug": 1, "name": 1}) or {}
        _okey = company_profile.office_key_for_pipeline(pipe_doc, fallback=company_profile.default_office_key())
        _defaults = _template_defaults(_okey)
        tpl = {**_defaults, **{k: v for k, v in stored_tpl.items() if v}, **{k: v for k, v in extra.items() if v}}
        # A dated exception outranks the per-person values too. The Training
        # modal fills its four time boxes from the pipeline template when it
        # opens — before the start date has been picked — and posts them back
        # as `extra`, so anything layered below it would be re-asserted to the
        # normal hours by every booking made through the kanban.
        tpl.update(_starter_date_override(settings, training_start_at))

        # Snapshot the exact template used for this send so the SMS AI can
        # reference what this specific person was told (not the generic pipeline default).
        await db.candidates.update_one(
            {"id": candidate_id},
            {"$set": {"starter_email_snapshot": tpl}},
        )

        profile = (settings.get("recruiter_profile") or {})
        company = profile.get("company_name") or company_profile.company_name()
        recruiter_email = profile.get("recruiter_email") or ""
        recruiter_phone = profile.get("phone") or ""
        office_address = tpl.get("office_address") or ""

        # Parse start date/time.  Older mobile app builds sent UTC (Z suffix);
        # newer builds send a naive local ISO (no suffix).  Always normalise to ET.
        start_date_str, start_time_str = "", ""
        if training_start_at:
            try:
                dt = _parse_training_ts(training_start_at)
                start_date_str = dt.strftime("%A, %B %-d, %Y")
                start_time_str = dt.strftime("%-I:%M %p")
            except Exception:
                start_date_str = training_start_at[:10]

        first_name = cand.get("first_name") or "there"
        import html as _html_mod
        _first_name_html = _html_mod.escape(first_name, quote=True)  # candidate-typed
        to_email = cand.get("email")
        subject = f"Start Date Confirmation — {company}"

        logo_url = profile.get("logo_url") or company_profile.get("logo_url")
        job_role = profile.get("job_role") or company_profile.get("role_title", "Sales Representative")
        _logo_img = (
            f'<img src="{logo_url}" alt="{company}" width="80" height="80" '
            'style="display:block;margin:0 auto 18px;border-radius:14px;background:#1e293b;" />'
        ) if logo_url else ""

        d1s = tpl.get("monday_start", "")
        d1e = tpl.get("monday_end", "")
        d2s = tpl.get("tuesday_start", "")
        d2e = tpl.get("tuesday_end", "")
        reg = tpl.get("regular_schedule", "")
        # Spelled-out note under the schedule table: "Wed onwards" alone read
        # as ambiguous — make the rest of the first week explicit.
        _sched_note = (
            '<div style="font-size:12.5px;color:#64748b;margin-top:10px;line-height:1.7;">'
            "After Tuesday&rsquo;s training sessions you&rsquo;re on the normal schedule &mdash; "
            f"<b>{reg}</b>. "
            "That&rsquo;s your regular working week from then on.</div>"
        ) if reg else ""

        custom_block = ""
        if tpl.get("custom_notes", "").strip():
            custom_block = f'<div style="background:#fef3c7;border-left:4px solid #F59E0B;padding:12px 14px;border-radius:8px;margin:14px 0"><p style="color:#78350f;font-size:14px;margin:0;line-height:1.6">{tpl["custom_notes"].strip()}</p></div>'

        _has_schedule = d1s or d2s or reg
        _bring_rows = ""
        if tpl.get("dress_code"):
            _bring_rows += f'<tr><td style="width:32px;padding:10px 0 10px;vertical-align:top;font-size:18px;">&#128084;</td><td style="padding:10px 0 10px;color:#334155;font-size:14px;line-height:1.6;border-bottom:1px solid #f1f5f9;"><strong style="color:#0f172a;">Dress code:</strong> {tpl["dress_code"]}</td></tr>'
        if tpl.get("id_text"):
            _bring_rows += f'<tr><td style="width:32px;padding:10px 0 10px;vertical-align:top;font-size:18px;">&#128282;</td><td style="padding:10px 0 10px;color:#334155;font-size:14px;line-height:1.6;border-bottom:1px solid #f1f5f9;">{tpl["id_text"]}</td></tr>'
        if tpl.get("research_text"):
            _bring_rows += f'<tr><td style="width:32px;padding:10px 0 10px;vertical-align:top;font-size:18px;">&#128218;</td><td style="padding:10px 0 10px;color:#334155;font-size:14px;line-height:1.6;border-bottom:1px solid #f1f5f9;">{tpl["research_text"]}</td></tr>'
        if tpl.get("food_text"):
            _bring_rows += f'<tr><td style="width:32px;padding:10px 0 10px;vertical-align:top;font-size:18px;">&#127869;</td><td style="padding:10px 0 10px;color:#334155;font-size:14px;line-height:1.6;border-bottom:1px solid #f1f5f9;">{tpl["food_text"]}</td></tr>'
        if tpl.get("parking_text"):
            _bring_rows += f'<tr><td style="width:32px;padding:10px 0 10px;vertical-align:top;font-size:18px;">&#128663;</td><td style="padding:10px 0 10px;color:#334155;font-size:14px;line-height:1.6;">{tpl["parking_text"]}</td></tr>'
        # Optional team-app install block. Shown only when the company profile
        # names a field app with an install page (field_app.name/install_url).
        # Installing ahead of Day 1 is encouraged, but login details arrive on
        # the first day of training — the copy sets that expectation.
        _field_app = company_profile.PROFILE.get("field_app") or {}
        _app_name = (_field_app.get("name") or "").strip()
        _app_url = (_field_app.get("install_url") or "").strip()
        _app_section = f"""
  <!-- Get the team app -->
  <tr><td style="padding:0 32px 8px;">
    <div style="background:#f5f3ff;border-radius:12px;padding:16px 18px;margin:6px 0;">
      <div style="font-size:13px;font-weight:700;color:#5b21b6;letter-spacing:0.1em;text-transform:uppercase;margin-bottom:8px;">&#128241; Get the {_app_name} App before Day 1</div>
      <div style="font-size:14px;color:#334155;line-height:1.7;">
        {_app_name} is our team app &mdash; your schedule and training live there.<br>
        Open <a href="{_app_url}" style="color:#7c3aed;font-weight:700;">{_app_url}</a> on your phone, then add it to your home screen:<br>
        <b>iPhone:</b> in Safari, tap Share &rarr; Add to Home Screen<br>
        <b>Android:</b> in Chrome, tap &#8942; &rarr; Add to Home screen<br>
        No need to sign up &mdash; you&rsquo;ll receive your personal login details on your first day of training, so just have the app installed and ready.
      </div>
    </div>
  </td></tr>""" if (_app_name and _app_url) else ""

        _bring_section = f"""
      <!-- What to bring -->
      <tr><td style="padding:28px 36px 8px;">
        <div style="font-size:13px;font-weight:700;color:#64748b;letter-spacing:0.1em;text-transform:uppercase;margin-bottom:12px;">What to Bring &amp; Know</div>
        <table width="100%" cellpadding="0" cellspacing="0" border="0">
          {_bring_rows}
        </table>
      </td></tr>""" if _bring_rows else ""

        _contact_section = ""
        if recruiter_phone or recruiter_email:
            _contact_val = recruiter_phone or recruiter_email
            _contact_section = f"""
      <!-- Contact -->
      <tr><td style="padding:0 36px 28px;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f0f9ff;border-radius:10px;">
          <tr><td style="padding:16px 20px;">
            <span style="font-size:14px;color:#0369a1;line-height:1.6;">
              &#128172; Questions or can&#39;t find us? Reach out directly on <strong>{_contact_val}</strong>.
            </span>
          </td></tr>
        </table>
      </td></tr>"""

        _custom_section = ""
        if tpl.get("custom_notes", "").strip():
            _custom_section = f"""
      <tr><td style="padding:0 36px 24px;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#fffbeb;border-left:3px solid #f59e0b;border-radius:0 8px 8px 0;">
          <tr><td style="padding:14px 16px;font-size:14px;color:#78350f;line-height:1.6;">{tpl["custom_notes"].strip()}</td></tr>
        </table>
      </td></tr>"""

        _maps_link = office_maps_link(_okey)
        _apple_link = office_apple_maps_link(_okey)
        _maps_line = (
            f'<div style="margin-top:8px;"><a href="{_maps_link}" style="font-size:13px;font-weight:600;color:#c2410c;text-decoration:none;">&#128506;&#65039; Get directions on Google Maps &rarr;</a></div>'
            if _maps_link else ""
        )
        if _apple_link:
            # Some map apps pin a typed address on the wrong building; an
            # office's apple_maps_link (company profile) can target the pin
            # that is actually your door.
            _maps_line += (
                f'<div style="margin-top:6px;"><a href="{_apple_link}" style="font-size:13px;font-weight:600;color:#c2410c;text-decoration:none;">&#63743; Get directions on Apple Maps &rarr;</a></div>'
            )
        _addr_block = ""
        if office_address or _maps_link:
            _addr_block = f"""
            <tr><td style="padding:0 24px 20px;">
              <div style="height:1px;background:#fed7aa;margin-bottom:16px;"></div>
              <div style="font-size:11px;font-weight:700;color:#c2410c;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:6px;">&#128205; Office Address</div>
              <div style="font-size:15px;font-weight:600;color:#0f172a;">{office_address}</div>
              {_maps_line}
            </td></tr>"""

        _year = datetime.utcnow().year
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="format-detection" content="telephone=no,date=no,address=no,email=no,url=no">
  <meta name="x-apple-disable-message-reformatting">
  <title>Welcome to {company}</title>
</head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:Arial,'Helvetica Neue',Helvetica,sans-serif;">
<!-- Preheader -->
<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">Congratulations {_first_name_html} — your start date with {company} is confirmed. We can&#39;t wait to meet you!</div>

<table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation" style="background:#f1f5f9;padding:32px 16px;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" border="0" role="presentation" style="max-width:600px;width:100%;">

  <!-- Header -->
  <tr>
    <td style="background:#0f172a;border-radius:16px 16px 0 0;padding:36px 36px 32px;text-align:center;">
      {_logo_img}
      <div style="font-size:11px;font-weight:700;color:#64748b;letter-spacing:0.18em;text-transform:uppercase;margin-bottom:10px;">Offer Confirmed</div>
      <div style="font-size:26px;font-weight:700;color:#ffffff;line-height:1.25;letter-spacing:-0.02em;">
        You&#39;re hired, {_first_name_html}! &#127881;
      </div>
      <div style="font-size:14px;color:#94a3b8;margin-top:8px;">Welcome to the {company} team</div>
    </td>
  </tr>

  <!-- Intro -->
  <tr>
    <td style="background:#ffffff;padding:32px 36px 24px;border-left:1px solid #e2e8f0;border-right:1px solid #e2e8f0;">
      <p style="margin:0 0 16px;font-size:15px;color:#1e293b;line-height:1.7;">
        Hi <strong>{_first_name_html}</strong>,
      </p>
      <p style="margin:0 0 16px;font-size:15px;color:#334155;line-height:1.7;">
        Congratulations on being selected for the <strong style="color:#0f172a;">{job_role}</strong> position at <strong style="color:#0f172a;">{company}</strong>. We were genuinely impressed throughout the process and we&#39;re excited to have you on board.
      </p>
    </td>
  </tr>

  <!-- Start date card -->
  <tr>
    <td style="background:#ffffff;padding:0 36px 28px;border-left:1px solid #e2e8f0;border-right:1px solid #e2e8f0;">
      <table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation"
             style="background:#fff7ed;border:1px solid #fed7aa;border-radius:12px;">
        <tr>
          <td style="padding:20px 24px 20px;">
            <div style="font-size:11px;font-weight:700;color:#c2410c;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:10px;">&#128197; Start Date &amp; Time</div>
            <div style="font-size:24px;font-weight:700;color:#0f172a;line-height:1.2;">{start_date_str}</div>
            <div style="font-size:18px;font-weight:600;color:#ea580c;margin-top:4px;">{start_time_str}</div>
          </td>
        </tr>
        {_addr_block}
      </table>
    </td>
  </tr>

  {'<!-- Schedule --><tr><td style="background:#ffffff;padding:0 36px 28px;border-left:1px solid #e2e8f0;border-right:1px solid #e2e8f0;"><div style="font-size:13px;font-weight:700;color:#64748b;letter-spacing:0.1em;text-transform:uppercase;margin-bottom:12px;">&#128198; Your First Few Days</div><table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation" style="border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;"><tr style="background:#eff6ff;"><td style="padding:14px 18px;border-bottom:1px solid #e2e8f0;"><div style="font-size:14px;font-weight:700;color:#1d4ed8;">Monday</div><div style="font-size:12px;color:#60a5fa;margin-top:2px;">Orientation &amp; Training</div></td><td align="right" style="padding:14px 18px;border-bottom:1px solid #e2e8f0;white-space:nowrap;"><span style="font-size:14px;font-weight:700;color:#0f172a;">' + d1s + '&nbsp;&ndash;&nbsp;' + d1e + '</span></td></tr><tr style="background:#ffffff;"><td style="padding:14px 18px;border-bottom:1px solid #e2e8f0;"><div style="font-size:14px;font-weight:700;color:#1d4ed8;">Tuesday</div><div style="font-size:12px;color:#60a5fa;margin-top:2px;">Orientation &amp; Training</div></td><td align="right" style="padding:14px 18px;border-bottom:1px solid #e2e8f0;white-space:nowrap;"><span style="font-size:14px;font-weight:700;color:#0f172a;">' + d2s + '&nbsp;&ndash;&nbsp;' + d2e + '</span></td></tr><tr style="background:#f0fdf4;"><td style="padding:14px 18px;"><div style="font-size:14px;font-weight:700;color:#15803d;">Wednesday &ndash; Friday</div><div style="font-size:12px;color:#4ade80;margin-top:2px;">Normal working days</div></td><td align="right" style="padding:14px 18px;white-space:nowrap;"><span style="font-size:14px;font-weight:700;color:#0f172a;">' + reg + '</span></td></tr></table>' + _sched_note + '</td></tr>' if _has_schedule else ''}

  {_bring_section}

  {_app_section}

  {_custom_section}

  {_contact_section}

  <!-- Closing -->
  <tr>
    <td style="background:#ffffff;padding:28px 36px 32px;border-left:1px solid #e2e8f0;border-right:1px solid #e2e8f0;border-top:1px solid #e2e8f0;">
      <p style="margin:0 0 20px;font-size:15px;color:#334155;line-height:1.7;">
        Once again — congratulations. We look forward to meeting you in person on your first day!
      </p>
      <p style="margin:0;font-size:15px;color:#334155;line-height:1.7;">
        Warm regards,<br/>
        <strong style="color:#0f172a;">{company}</strong>
      </p>
    </td>
  </tr>

  <!-- Footer -->
  <tr>
    <td style="background:#f8fafc;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 16px 16px;padding:20px 36px;text-align:center;">
      <p style="margin:0 0 6px;font-size:12px;color:#94a3b8;line-height:1.6;">You&#39;re receiving this because you applied for a role at {company}.</p>
      <p style="margin:0;font-size:12px;color:#cbd5e1;">&copy; {_year} {company}</p>
    </td>
  </tr>

</table>
</td></tr>
</table>
</body>
</html>"""

        plain = (
            f"Hi {first_name},\n\nCongratulations — you're hired!\n\n"
            f"Start Date: {start_date_str}\nStart Time: {start_time_str}\n"
        )
        if tpl.get("monday_start"): plain += f"Day 1: {tpl['monday_start']} – {tpl.get('monday_end','')}\n"
        if tpl.get("tuesday_start"): plain += f"Day 2: {tpl['tuesday_start']} – {tpl.get('tuesday_end','')}\n"
        if tpl.get("regular_schedule"): plain += f"From Wednesday (normal schedule): {tpl['regular_schedule']}\n"
        if tpl.get("dress_code"): plain += f"\nDress Code: {tpl['dress_code']}\n"
        if tpl.get("id_text"): plain += f"\nID & Documents: {tpl['id_text']}\n"
        if tpl.get("research_text"): plain += f"\nPre-Training Research: {tpl['research_text']}\n"
        if tpl.get("custom_notes"): plain += f"\nNotes: {tpl['custom_notes']}\n"
        if office_address: plain += f"\nOffice Address: {office_address}\n"
        if _maps_link: plain += f"Directions (Google Maps): {_maps_link}\n"
        if _apple_link:
            plain += f"Directions (Apple Maps): {_apple_link}\n"
        if recruiter_email or recruiter_phone:
            plain += f"\nQuestions? {recruiter_email or ''} {recruiter_phone or ''}\n"
        plain += f"\nThe {company} team"

        import os
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail
        sg_key = os.environ.get("SENDGRID_API_KEY", "")
        from_addr = (os.environ.get("SENDGRID_FROM_EMAIL") or "").strip()
        from_name = (settings.get("recruiter_profile") or {}).get("company_name") or os.environ.get("SENDGRID_FROM_NAME") or company_profile.company_name()
        if not sg_key:
            return {"status": "skipped", "reason": "SENDGRID_API_KEY not configured"}
        if not from_addr:
            return {"status": "skipped", "reason": "SENDGRID_FROM_EMAIL not configured"}
        msg = Mail(
            from_email=(from_addr, from_name),
            to_emails=to_email,
            subject=subject,
            plain_text_content=plain,
            html_content=html,
        )
        reply_domain = company_profile.email_reply_domain()
        if reply_domain:
            from sendgrid.helpers.mail import ReplyTo
            msg.reply_to = ReplyTo(f"reply+{candidate_id}@{reply_domain}")
        sg = SendGridAPIClient(sg_key)
        # sg.send() is a blocking HTTP call; run it off the event loop so it
        # doesn't stall every other request (dialer, other API calls) while it waits.
        import asyncio as _asyncio
        resp = await _asyncio.get_event_loop().run_in_executor(None, lambda: sg.send(msg))
        result = {"status": "sent", "code": resp.status_code}
        await log_communication(
            candidate_id=candidate_id, user_id=user_id, type_="email",
            template_key="starter", subject=subject, body=plain,
            to_address=to_email, status="sent",
        )
        return result
    except Exception as e:
        logger.warning(f"_send_starter_email_direct failed for {candidate_id}: {e}")
        return {"status": "failed", "error": str(e)}


async def _fire_cg1_training_webhook(
    user_id: str,
    candidate_id: str,
    training_start_at: str = "",
    extra_template: Optional[dict] = None,
) -> Optional[str]:
    """POST the new-starter payload to CG1 when a candidate is moved to Training.
    Loads the pipeline's starter_template from settings (stored/edited in CGRecruit)
    and passes all fields so CG1 renders the exact email content we specify.
    Returns the CG1 starter_email_id on success, or None if not configured."""
    import os, httpx as _httpx
    cg1_url = os.getenv("CG1_BACKEND_URL", "").rstrip("/")
    secret = os.getenv("CG1_WEBHOOK_SECRET", "")
    if not cg1_url:
        return None
    try:
        cand = await db.candidates.find_one({"id": candidate_id}, {"_id": 0})
        if not cand:
            return None
        pipe = await db.pipelines.find_one({"id": cand.get("pipeline_id"), "user_id": user_id}, {"_id": 0}) or {}
        office_key = company_profile.office_key_for_pipeline(pipe)
        if not office_key:
            logger.warning(f"CG1 webhook: no office_key for pipeline {pipe.get('id')} — skipping")
            return None

        # Load the pipeline's stored starter template (edited in CGRecruit settings).
        pipeline_id = cand.get("pipeline_id")
        stored_settings = await db.settings.find_one(
            {"user_id": user_id, "pipeline_id": pipeline_id},
            {"_id": 0, "starter_template": 1, "starter_date_overrides": 1},
        ) or {}
        stored_tpl = stored_settings.get("starter_template") or {}
        defaults = _template_defaults(office_key)
        # Priority: per-send override > pipeline stored template > office defaults
        tpl = {**defaults, **stored_tpl, **(extra_template or {})}
        # ...and a dated exception above all three, so CG1's roster shows the
        # same hours the candidate was emailed. A pipeline with no override doc
        # of its own still gets the tenant-wide exceptions.
        _dated = _starter_date_override(stored_settings, training_start_at)
        if not _dated:
            _global_settings = await db.settings.find_one(
                {"user_id": user_id, "pipeline_id": None}, {"_id": 0, "starter_date_overrides": 1}
            ) or {}
            _dated = _starter_date_override(_global_settings, training_start_at)
        tpl.update(_dated)

        # Parse training_start_at into date + time strings for CG1.
        # Normalise to ET regardless of whether the value is UTC-aware or naive local.
        start_date, start_time = "", ""
        if training_start_at:
            try:
                dt = _parse_training_ts(training_start_at)
                start_date = dt.strftime("%Y-%m-%d")
                start_time = dt.strftime("%-I:%M %p")
            except Exception:
                start_date = training_start_at[:10]
                start_time = ""

        backend_base = company_profile.backend_api_url()
        callback_url = f"{backend_base.rstrip('/')}/webhooks/cg1/attended"

        payload = {
            "name": f"{cand.get('first_name', '')} {cand.get('last_name', '')}".strip(),
            "email": cand.get("email") or "",
            "phone": cand.get("phone") or "",
            "start_date": start_date,
            "start_time": start_time,
            "office": office_key,
            "send_email": False,
            "cgrecruit_candidate_id": candidate_id,
            "cgrecruit_callback_url": callback_url,
            # Full template fields — CG1 uses these in priority over its own stored values.
            "monday_start":     tpl.get("monday_start", ""),
            "monday_end":       tpl.get("monday_end", ""),
            "tuesday_start":    tpl.get("tuesday_start", ""),
            "tuesday_end":      tpl.get("tuesday_end", ""),
            "regular_schedule": tpl.get("regular_schedule", ""),
            "dress_code":       tpl.get("dress_code", ""),
            "id_text":          tpl.get("id_text", ""),
            "research_text":    tpl.get("research_text", ""),
            "food_text":        tpl.get("food_text", ""),
            "parking_text":     tpl.get("parking_text", ""),
            "custom_notes":     tpl.get("custom_notes", ""),
        }
        async with _httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{cg1_url}/api/webhooks/cgrecruit/new-starter",
                json=payload,
                headers={"x-webhook-secret": secret},
            )
            r.raise_for_status()
            cg1_id = r.json().get("starter_email_id")
        if cg1_id:
            await db.candidates.update_one(
                {"id": candidate_id},
                {"$set": {"cg1_starter_email_id": cg1_id, "cg1_webhook_status": "sent", "updated_at": now_iso()}},
            )
        logger.info(f"CG1 training webhook fired for {candidate_id} → starter_email_id={cg1_id}")
        return cg1_id
    except Exception as e:
        logger.warning(f"CG1 training webhook failed for {candidate_id}: {e}")
        await db.candidates.update_one(
            {"id": candidate_id},
            {"$set": {"cg1_webhook_status": f"failed:{e}", "updated_at": now_iso()}},
        )
        return None


async def _autosync_pipeline_agent(user_id: str, pipeline: Dict[str, Any]) -> None:
    """If the pipeline has its own ElevenLabs agent override, push prompt-override
    changes to ElevenLabs immediately. Silently no-ops for pipelines that share
    the tenant's master agent (sync those via the main /elevenlabs/agent/sync)."""
    agent_id = pipeline.get("elevenlabs_agent_id_override")
    if not agent_id:
        return
    # Must use resolve_settings with the pipeline_id so that any pipeline-specific
    # screen_call_agent override wins over the global settings doc. The old
    # get_or_create_settings(user_id) call was always reading global-only, which
    # meant a pipeline PATCH would overwrite a dedicated agent with the global scripts.
    settings = await resolve_settings(user_id, pipeline.get("id"))
    sca = dict(settings.get("screen_call_agent") or {})
    booking_prefs = settings.get("booking_preferences") or {}
    if pipeline.get("agent_name_override"):
        sca["agent_name"] = pipeline["agent_name_override"]
    if pipeline.get("first_message_override"):
        sca["opening_message"] = pipeline["first_message_override"]
    if pipeline.get("additional_context_override"):
        existing = sca.get("additional_context", "")
        sca["additional_context"] = (
            f"{existing}\n\n# Location-Specific Context ({pipeline.get('name', '')})\n{pipeline['additional_context_override']}"
        ).strip()
    voice_id = pipeline.get("voice_id_override") or sca.get("voice_id") or ""
    sync_agent_to_elevenlabs(agent_id, sca, voice_id=voice_id, booking_prefs=booking_prefs)


@api.delete("/pipelines/{pipeline_id}")
async def delete_pipeline(pipeline_id: str, user: dict = Depends(current_user)):
    require_super_admin(user)
    await db.pipelines.delete_one({"id": pipeline_id, "user_id": user["id"]})
    await db.candidates.delete_many({"pipeline_id": pipeline_id, "user_id": user["id"]})
    await db.jobs.delete_many({"pipeline_id": pipeline_id, "user_id": user["id"]})
    # Also strip this pipeline from any recruiter's assignment list.
    await db.users.update_many(
        {"parent_user_id": user["id"]},
        {"$pull": {"pipeline_ids": pipeline_id}},
    )
    return {"ok": True}


@api.post("/pipelines/{pipeline_id}/duplicate", response_model=Pipeline)
async def duplicate_pipeline(
    pipeline_id: str,
    payload: Dict[str, Any] = Body(...),
    user: dict = Depends(current_super_admin),
):
    """One-click clone: copy availability rules, prompt overrides, calling mode,
    appointment settings, and active jobs into a new pipeline. Auto-provisions
    a fresh ElevenLabs agent (cloned from the source pipeline's override or the
    tenant's default). New name comes from `payload.name`."""
    src = await db.pipelines.find_one({"id": pipeline_id, "user_id": user["id"]}, {"_id": 0})
    if not src:
        raise HTTPException(404, "Pipeline not found")
    new_name = (payload.get("name") or "").strip()
    if not new_name:
        raise HTTPException(400, "name is required")
    if new_name.lower() == (src.get("name") or "").lower():
        raise HTTPException(400, "Pick a different name for the new office")
    slug = (payload.get("public_slug") or new_name).lower().replace(",", "").replace(" ", "-").strip("-")
    # Make sure slug is unique per tenant.
    existing = await db.pipelines.find_one({"user_id": user["id"], "public_slug": slug}, {"_id": 0, "id": 1})
    if existing:
        slug = f"{slug}-{new_id()[:6]}"

    # Clone-able fields. Skip user_id/id/public_slug/name (we set those) and the
    # ElevenLabs agent ID (we provision a new one) and Twilio number override.
    skip_fields = {"_id", "id", "user_id", "public_slug", "name", "elevenlabs_agent_id_override",
                   "elevenlabs_phone_number_id_override", "twilio_phone_number", "created_at"}
    inherited = {k: v for k, v in src.items() if k not in skip_fields}

    p = Pipeline(
        user_id=user["id"],
        name=new_name,
        public_slug=slug,
        twilio_phone_number=(payload.get("twilio_phone_number") or company_profile.default_twilio_number()),
        **inherited,
    )

    # Auto-provision a fresh ElevenLabs agent. Prefer cloning from the source
    # pipeline's agent if it has an override, else fall back to the tenant default.
    try:
        from voice_service import clone_agent_for_pipeline
        settings = await get_or_create_settings(user["id"])
        # If source has its own agent override, use that as the master config so the
        # duplicated office inherits the source's prompt tweaks.
        if src.get("elevenlabs_agent_id_override"):
            settings = {**settings,
                        "screen_call_agent": {**(settings.get("screen_call_agent") or {}),
                                              "elevenlabs_agent_id": src["elevenlabs_agent_id_override"]}}
        result = clone_agent_for_pipeline(settings, p.name)
        if result.get("agent_id"):
            p.elevenlabs_agent_id_override = result["agent_id"]
    except Exception as e:
        logger.warning(f"duplicate_pipeline: agent clone failed: {e}")

    await db.pipelines.insert_one(p.model_dump())

    # Clone every job from the source pipeline so the new office is ready to receive
    # applicants. Preserve `is_active` state — recruiter can flip it later if needed.
    src_jobs = await db.jobs.find(
        {"pipeline_id": pipeline_id, "user_id": user["id"]},
        {"_id": 0},
    ).to_list(50)
    for j in src_jobs:
        nj = Job(
            user_id=user["id"],
            pipeline_id=p.id,
            title=j.get("title", ""),
            description=j.get("description", ""),
            category=j.get("category", "Customer Services"),
            city=new_name.split(",")[0].strip() if "," in new_name else (j.get("city") or ""),
            country=j.get("country", "USA"),
            is_active=bool(j.get("is_active", True)),
        )
        await db.jobs.insert_one(nj.model_dump())

    return p


# ===== Jobs =====
@api.get("/jobs", response_model=List[Job])
async def list_jobs(pipeline_id: Optional[str] = None, user: dict = Depends(current_user)):
    q = {"user_id": user["id"]}
    if pipeline_id:
        q["pipeline_id"] = pipeline_id
    if not is_super_admin(user):
        q["pipeline_id"] = {"$in": user.get("pipeline_ids") or []}
    rows = await db.jobs.find(q, {"_id": 0}).to_list(500)
    return rows


@api.post("/jobs", response_model=Job)
async def create_job(payload: JobCreate, user: dict = Depends(current_user)):
    # Jobs show on the public apply page, so only accounts that may edit an
    # office, and only for an office they hold.
    require_mover(user)
    await assert_pipeline_in_tenant(user, payload.pipeline_id)
    j = Job(user_id=user["id"], **payload.model_dump())
    await db.jobs.insert_one(j.model_dump())
    return j


@api.put("/jobs/{job_id}", response_model=Job)
async def update_job(job_id: str, payload: JobCreate, user: dict = Depends(current_user)):
    require_mover(user)
    await assert_pipeline_in_tenant(user, payload.pipeline_id)
    existing_job = await db.jobs.find_one({"id": job_id, "user_id": user["id"]}, {"_id": 0, "pipeline_id": 1})
    if not existing_job:
        raise HTTPException(404, "Job not found")
    await assert_pipeline_access(user, existing_job.get("pipeline_id") or "")
    update = payload.model_dump()
    res = await db.jobs.find_one_and_update(
        {"id": job_id, "user_id": user["id"]},
        {"$set": update},
        return_document=True,
        projection={"_id": 0},
    )
    if not res:
        raise HTTPException(404, "Job not found")
    return res


@api.delete("/jobs/{job_id}")
async def delete_job(job_id: str, user: dict = Depends(current_user)):
    require_mover(user)
    existing_job = await db.jobs.find_one({"id": job_id, "user_id": user["id"]}, {"_id": 0, "pipeline_id": 1})
    if not existing_job:
        return {"ok": True}
    await assert_pipeline_access(user, existing_job.get("pipeline_id") or "")
    await db.jobs.delete_one({"id": job_id, "user_id": user["id"]})
    return {"ok": True}


# ===== Candidates =====
# NOTE: deliberately NO response_model here. Validating DB reads against the
# Candidate write-model means ONE legacy/odd doc (e.g. email=None from an old
# contact edit) 500s the WHOLE kanban, and it silently strips newer fields
# (revival_*, etc.) the drawer needs. The FE null-guards every field.
@api.get("/candidates")
async def list_candidates(
    pipeline_id: Optional[str] = None,
    include_archived: bool = False,
    archived_only: bool = False,
    user: dict = Depends(current_user),
):
    """List candidates for the kanban / dashboard.

    By default soft-rejected candidates (`archived_at IS NOT NULL`) are
    HIDDEN — they live in their own filter view that recruiters opt into.

    - `include_archived=true` — show everything including archived
    - `archived_only=true` — show ONLY archived candidates (the "Rejected"
      filter view)"""
    q: Dict[str, Any] = {"user_id": user["id"]}
    if pipeline_id:
        q["pipeline_id"] = pipeline_id
    if not is_super_admin(user):
        if "pipeline_id" in q and isinstance(q["pipeline_id"], str):
            # Recruiter/viewer asked for a specific pipeline — assert access.
            if q["pipeline_id"] not in (user.get("pipeline_ids") or []):
                return []
        else:
            q["pipeline_id"] = {"$in": user.get("pipeline_ids") or []}
    # Viewers only see candidates they personally added.
    ownership = candidate_ownership_filter(user)
    if ownership:
        q.update(ownership)
    # Archived-state filter — default hides archived; opt-in to show them.
    if archived_only:
        q["archived_at"] = {"$ne": None}
    elif not include_archived:
        # Match docs that lack the field entirely (legacy) OR have it set to null.
        q["$or"] = [{"archived_at": None}, {"archived_at": {"$exists": False}}]
    # Strip large fields not needed for kanban cards — the detail drawer
    # re-fetches the full record on click so nothing is lost.
    rows = await db.candidates.find(
        q,
        {"_id": 0, "chat_log": 0, "resume_text": 0},
    ).sort("created_at", -1).to_list(2000)
    return rows


@api.patch("/candidates/{candidate_id}", dependencies=[Depends(candidate_write_access)])
async def patch_candidate(candidate_id: str, payload: Dict[str, Any] = Body(...), user: dict = Depends(current_user)):
    """Correct editable metadata fields: referred_by, job_id, rating.
    Blocked for viewers (read-only). Does not trigger any stage emails or dialer jobs."""
    require_mover(user)
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")
    PATCHABLE = {"referred_by", "job_id", "rating", "first_name", "last_name", "email", "phone"}
    update: Dict[str, Any] = {"updated_at": now_iso()}
    for k in PATCHABLE:
        if k in payload:
            val = payload[k]
            if k == "referred_by":
                update[k] = (val or "").strip() or None
            elif k == "rating":
                update[k] = max(0, min(5, int(val or 0)))
            elif k == "email":
                # "" not None — the Candidate model types these as str, and a
                # None here used to 500 the whole kanban via response validation.
                update[k] = (val or "").strip().lower()
            elif k in ("first_name", "last_name", "phone"):
                update[k] = (val or "").strip()
            else:
                update[k] = val or None
    if len(update) == 1:
        raise HTTPException(400, f"No patchable fields provided. Allowed: {PATCHABLE}")
    await db.candidates.update_one({"id": candidate_id, "user_id": user["id"]}, {"$set": update})
    return await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})


@api.get("/candidates/{candidate_id}")
async def get_candidate(candidate_id: str, user: dict = Depends(current_user)):
    row = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    await assert_candidate_access(user, row)  # tenant alone isn't access control
    return row


@api.post("/candidates/direct-book")
async def direct_book_candidate(payload: Dict[str, Any] = Body(...), user: dict = Depends(current_user)):
    """Create a candidate directly at APPOINTMENT stage (bypass AI screening).
    Used by the Kanban + button on the Appointment column.

    Body: { pipeline_id, first_name, last_name?, email?, phone?, appointment_at,
            job_id?, referred_by?, send_confirmation? }
    """
    pipeline_id    = (payload.get("pipeline_id") or "").strip()
    first_name     = (payload.get("first_name") or "").strip()
    last_name      = (payload.get("last_name") or "").strip()
    email          = (payload.get("email") or "").strip().lower()
    phone          = (payload.get("phone") or "").strip()
    appointment_at = (payload.get("appointment_at") or "").strip()
    job_id         = (payload.get("job_id") or "").strip() or None
    referred_by    = (payload.get("referred_by") or "").strip() or None
    send_conf      = payload.get("send_confirmation", True)

    if not (pipeline_id and first_name and appointment_at):
        raise HTTPException(400, "pipeline_id, first_name, appointment_at required")

    # Viewers may add candidates (they then see only their own); analysts
    # are aggregates-only. Either way, only into an office they hold.
    if is_analyst(user):
        raise HTTPException(403, "Analyst accounts are read-only")
    await assert_pipeline_access(user, pipeline_id)
    pipe = await db.pipelines.find_one({"id": pipeline_id, "user_id": user["id"]}, {"_id": 0})
    if not pipe:
        raise HTTPException(404, "Pipeline not found")

    from deps import find_duplicate_candidate
    dup = await find_duplicate_candidate(pipeline_id, email, phone)
    if dup:
        raise HTTPException(409, f"A candidate with this email or phone is already in this pipeline (stage: {dup.get('stage', '?')})")

    from availability_service import resolve_slot_link, resolve_slot_recruiter
    _settings = await resolve_settings(user["id"], pipeline_id)
    _tz = (_settings.get("region_language") or {}).get("timezone") or default_tz_name()
    cand = Candidate(
        user_id=user["id"], pipeline_id=pipeline_id, job_id=job_id,
        first_name=first_name, last_name=last_name,
        email=email, phone=phone,
        stage="APPOINTMENT", appointment_at=appointment_at,
        appointment_link=resolve_slot_link(pipe, appointment_at, _tz) or pipe.get("appointment_link") or None,
        appointment_recruiter=resolve_slot_recruiter(pipe, appointment_at, _tz) or pipe.get("appointment_recruiter") or None,
        referred_by=referred_by or (user.get("name") if not is_super_admin(user) else None),
        added_by_user_id=user.get("auth_user_id"),
    )
    doc = cand.model_dump()
    await db.candidates.insert_one(doc)
    doc.pop("_id", None)  # Mongo's ObjectId isn't JSON; the response uses `id`
    from screening_outcome import ensure_screening_outcome_later
    ensure_screening_outcome_later(db, cand.id)

    # Fire reminders + confirmation comms in the background so the HTTP response
    # returns immediately after the insert. SendGrid/Twilio calls can take several
    # seconds and the client would otherwise timeout and show "failed" even though
    # the candidate was successfully created.
    async def _background_comms():
        try:
            from auto_dialer import schedule_appointment_reminders
            await schedule_appointment_reminders(user["id"], cand.id, appointment_at)
        except Exception as e:
            logger.warning(f"direct-book reminders failed: {e}")
        if send_conf and (email or phone):
            # (email or phone), not email alone: send_stage_comms degrades
            # per-channel, and the old email-only gate meant a phone-only
            # walk-in was booked, told nothing, and then got the "are you
            # still good?" chaser about a booking they were never sent.
            try:
                from deps import send_stage_comms as _ssc
                # A hand-booked candidate usually has no screening behind them
                # — same honesty rule as the portal door: never claim "your
                # screening was successful" to someone who was never screened.
                _key = (
                    "approval"
                    if ((doc.get("verdict") or "") not in ("", "incomplete") or doc.get("call_summary"))
                    else "approval_unscreened"
                )
                await _ssc(user["id"], doc, _key)
            except Exception as e:
                logger.warning(f"direct-book approval comms failed: {e}")

    import asyncio as _asyncio
    _asyncio.create_task(_background_comms())

    return {"ok": True, "candidate": doc}


@api.post("/candidates", response_model=Candidate)
async def create_candidate(payload: CandidateCreate, user: dict = Depends(current_user)):
    require_mover(user)
    await assert_pipeline_in_tenant(user, payload.pipeline_id)
    from deps import find_duplicate_candidate
    dup = await find_duplicate_candidate(
        payload.pipeline_id, (payload.email or "").lower(), payload.phone or ""
    )
    if dup:
        raise HTTPException(409, f"A candidate with this email or phone already exists in this pipeline (stage: {dup.get('stage', '?')})")
    # Auto-attribute to the recruiter/viewer who added them so they appear in
    # "My Recruits" — only for sub-accounts (super-admin is the org, not a referrer).
    auto_referred_by = user.get("name") if not is_super_admin(user) else None
    c = Candidate(
        user_id=user["id"],
        added_by_user_id=user.get("auth_user_id"),
        referred_by=auto_referred_by,
        **payload.model_dump(),
    )
    doc = c.model_dump()
    await db.candidates.insert_one(doc)
    if not payload.skip_warmup:
        from screening_start import start_screening
        settings = await resolve_settings(user["id"], c.pipeline_id)
        await start_screening(user["id"], doc, settings)
    return c


_MAX_CV_BYTES = 10 * 1024 * 1024


@api.post("/candidates/upload")
async def upload_candidate_resume(
    file: UploadFile = File(...),
    pipeline_id: str = Form(...),
    job_id: Optional[str] = Form(None),
    referred_by: Optional[str] = Form(None),
    user: dict = Depends(current_user),
):
    """Upload a CV (PDF/DOCX/TXT). Extract text, parse with Claude, optionally smart-score against job."""
    if is_analyst(user):
        raise HTTPException(403, "Analyst accounts are read-only")
    await assert_pipeline_in_tenant(user, pipeline_id)
    content = await file.read(_MAX_CV_BYTES + 1)
    if len(content) > _MAX_CV_BYTES:
        raise HTTPException(413, "That file is too large (10 MB maximum).")
    text = extract_resume_text(file.filename, content)
    candidate_id = new_id()

    # Limit concurrent AI parses so bulk uploads don't exhaust memory/rate-limits
    async with _upload_parse_sem:
        parsed = await parse_resume(text, candidate_id)
        # Smart-score inside the semaphore to keep concurrent AI calls bounded
        smart_score_result = None
        if job_id:
            job = await db.jobs.find_one({"id": job_id, "user_id": user["id"]}, {"_id": 0})
            if job:
                smart_score_result = await smart_score(parsed, job, candidate_id)

    first = parsed.get("first_name") or "Unknown"
    last = parsed.get("last_name") or ""
    email = parsed.get("email") or ""
    phone = parsed.get("phone") or ""
    cand = Candidate(
        id=candidate_id,
        user_id=user["id"],
        pipeline_id=pipeline_id,
        job_id=job_id,
        first_name=first,
        last_name=last,
        email=email,
        phone=phone,
        resume_text=text[:20000],
        parsed_resume=parsed,
        referred_by=(referred_by or "").strip() or (user.get("name") if not is_super_admin(user) else None),
        added_by_user_id=user.get("auth_user_id"),
    )
    if smart_score_result:
        cand.smart_score = smart_score_result.get("score")
        cand.smart_score_rationale = smart_score_result.get("rationale", "")

    from deps import find_duplicate_candidate
    dup = await find_duplicate_candidate(pipeline_id, email, phone)
    if dup:
        logger.info(f"upload: silently skipping duplicate candidate email={email} phone={phone} pipeline={pipeline_id}")
        return JSONResponse(status_code=200, content={"duplicate": True, "existing_id": dup["id"]})
    doc = cand.model_dump()
    await db.candidates.insert_one(doc)

    # Fire warmup comms + schedule call in the background so the HTTP response
    # returns immediately — critical when many CVs are uploaded at once.
    async def _post_upload_tasks():
        from screening_start import start_screening
        settings = await resolve_settings(user["id"], pipeline_id)
        await start_screening(user["id"], doc, settings)

    asyncio.create_task(_post_upload_tasks())

    # Re-fetch without _id to avoid ObjectId serialization issues
    return await db.candidates.find_one({"id": cand.id}, {"_id": 0})


@api.get("/pipelines/{pipeline_id}/slots")
async def admin_pipeline_slots(pipeline_id: str, days: int = 21, user: dict = Depends(current_user)):
    """Returns available appointment slots for a pipeline. Used by the Kanban
    drag-to-APPOINTMENT slot picker so the recruiter can override-set a candidate's
    appointment time."""
    await assert_pipeline_access(user, pipeline_id)
    pipe = await db.pipelines.find_one({"id": pipeline_id, "user_id": user["id"]}, {"_id": 0})
    if not pipe:
        raise HTTPException(404, "Pipeline not found")
    settings = await resolve_settings(user["id"], pipeline_id)
    from availability_service import compute_available_slots
    booked_docs = await db.candidates.find(
        {"pipeline_id": pipeline_id, "appointment_at": {"$ne": None}},
        {"_id": 0, "appointment_at": 1, "id": 1},
    ).to_list(1000)
    booked_isos = [b.get("appointment_at") for b in booked_docs if b.get("appointment_at")]
    appt_settings = settings.get("appointments") or {}
    default_cap = int(appt_settings.get("applicant_limit") or 50)
    tz_name = pipe.get("timezone") or (settings.get("region_language") or {}).get("timezone") or default_tz_name()
    return {
        "pipeline_id": pipeline_id,
        "timezone": tz_name,
        "slots": compute_available_slots(
            pipe, booked_isos, days_ahead=max(1, min(days, 90)),
            tz_name=tz_name, default_capacity=default_cap,
        ),
    }


# APPLICANT stays a legacy alias the read paths still tolerate, but the board has
# no column for it: anyone moved there loses their card, and with it the drawer,
# the checkbox and every way back. Destinations are limited to the stages the
# board can render.
MOVABLE_STAGES = [s for s in STAGES if s != "APPLICANT"]


@api.post("/candidates/{candidate_id}/move", dependencies=[Depends(candidate_write_access)])
async def move_candidate(candidate_id: str, payload: CandidateMove, user: dict = Depends(current_user)):
    require_mover(user)
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")
    update: Dict[str, Any] = {"updated_at": now_iso()}
    for k in ["stage", "screening_status", "appointment_at", "appointment_recruiter", "appointment_link", "rating"]:
        v = getattr(payload, k, None)
        if v is not None:
            update[k] = v
    # Only a MOVE to a legacy stage is refused. A candidate already sitting in one
    # can still re-post it — the drawer's stage stepper leaves the current chip
    # clickable — and that has always been a no-op, not an error.
    if "stage" in update and update["stage"] not in MOVABLE_STAGES and update["stage"] != cand.get("stage"):
        raise HTTPException(400, f"stage must be one of {MOVABLE_STAGES}")
    prev_appointment_at = cand.get("appointment_at")
    prev_stage = cand.get("stage")

    # ── the lapsed-interview gate ────────────────────────────────────────────
    # Moving on from an interview that has already happened, without saying what
    # happened, is how the outcome gets lost: the buttons that set it live only in
    # the APPOINTMENT stage, so the move both discards the answer and removes the
    # means of giving it. Ask for it here instead — attendance_status on the move
    # is the intended path for API callers; the UI marks it first through
    # POST /candidates/{id}/attendance, which satisfies the same gate.
    _outcome = (payload.attendance_status or "").strip() or None
    _leaving_appointment = prev_stage == "APPOINTMENT" and update.get("stage") not in (None, "APPOINTMENT")
    if _leaving_appointment and not cand.get("attendance_status") and not _outcome:
        if appointment_has_lapsed(prev_appointment_at):
            raise HTTPException(
                409,
                "Say whether they attended before moving them on — their interview "
                "time has passed and no outcome has been recorded.",
            )
    if _outcome:
        if _outcome not in ATTENDANCE_VALUES:
            raise HTTPException(400, f"attendance_status must be one of {sorted(ATTENDANCE_VALUES)}")
        update["attendance_status"] = _outcome
        update["attendance_recorded_at"] = now_iso()
        update["attendance_recorded_by"] = user.get("id")
    # If appointment_at is being set/changed but appointment_link wasn't provided:
    # a slot-level link override always wins (the slot dictates its link); else
    # fall back to the pipeline's Zoom link so reminder emails never have a blank link.
    if update.get("appointment_at") and "appointment_link" not in update:
        pipe = await db.pipelines.find_one(
            {"id": cand.get("pipeline_id"), "user_id": user["id"]},
            {"_id": 0, "appointment_link": 1, "appointment_recruiter": 1, "availability_rules": 1},
        ) or {}
        from availability_service import resolve_slot_link, resolve_slot_recruiter
        _settings = await resolve_settings(user["id"], cand.get("pipeline_id"))
        _tz = (_settings.get("region_language") or {}).get("timezone") or default_tz_name()
        _slot_link = resolve_slot_link(pipe, update["appointment_at"], _tz)
        if _slot_link:
            update["appointment_link"] = _slot_link
        elif pipe.get("appointment_link"):
            # Slot has no override — reset to the pipeline default rather than
            # leaving the OLD slot's override on the candidate.
            update["appointment_link"] = pipe["appointment_link"]
        # Same override-or-default rule for who runs the slot.
        if "appointment_recruiter" not in update:
            _slot_rec = resolve_slot_recruiter(pipe, update["appointment_at"], _tz)
            if _slot_rec:
                update["appointment_recruiter"] = _slot_rec
            elif pipe.get("appointment_recruiter"):
                update["appointment_recruiter"] = pipe["appointment_recruiter"]
    # Permanent milestone timestamps — set only on first arrival, never overwritten.
    _new_stage_for_milestone = update.get("stage", prev_stage)
    _ts = now_iso()
    if _new_stage_for_milestone == "FORM" and not cand.get("moved_to_form_at"):
        update["moved_to_form_at"] = _ts
    elif _new_stage_for_milestone == "CLOSE" and not cand.get("moved_to_close_at"):
        update["moved_to_close_at"] = _ts
    elif _new_stage_for_milestone == "TRAINING" and not cand.get("moved_to_training_at"):
        update["moved_to_training_at"] = _ts
    await db.candidates.update_one({"id": candidate_id, "user_id": user["id"]}, {"$set": update})
    cand.update(update)
    # If a stage change happened, the recruiter has effectively intervened —
    # cancel any pending v32 auto-promote so it doesn't fire later and clobber
    # the manual decision.
    if "stage" in update or "screening_status" in update:
        try:
            from auto_dialer import cancel_auto_promote, cancel_pending_retry_calls
            cancel_auto_promote(candidate_id, reason="manual_move")
            if "stage" in update and update["stage"] != prev_stage:
                # Cancel any pending screening call or pre-call SMS so a fast-tracked
                # candidate doesn't receive the warmup text after leaving SCREENING.
                cancel_pending_retry_calls(candidate_id)
        except Exception:
            pass
    # Optional: trigger stage email + SMS. An explicit send_email_template is
    # always honoured, including when the stage doesn't actually change — bulk
    # moves land on a stage some of the batch already sit in, and the card's
    # "Queue call" warmup posts no stage at all. Callers that must not double-send
    # omit the field rather than relying on a stage-changed test here.
    email_status = None
    if payload.send_email_template:
        tpl_key = payload.send_email_template
        # Stamp outcome timestamps so the Kanban card can show the right badge.
        if tpl_key == "form_decline":
            await db.candidates.update_one(
                {"id": candidate_id, "user_id": user["id"]},
                {"$set": {"form_decline_at": now_iso()}},
            )
        elif tpl_key == "close_success":
            await db.candidates.update_one(
                {"id": candidate_id, "user_id": user["id"]},
                {"$set": {"close_success_at": now_iso()}},
            )
        try:
            email_status = await _send_stage_email(user_id=user["id"], candidate=cand, template_key=tpl_key)
        except Exception as _email_exc:
            logger.warning(f"stage email ({tpl_key}) failed in /move: {_email_exc}")
            email_status = {"status": "failed", "error": str(_email_exc)}
        # For warmup templates when moving to SCREENING, skip the inline SMS —
        # schedule_call_with_window (below) schedules a pre-call SMS N minutes
        # before the AI dial, so firing both would duplicate the text.
        _is_warmup_to_screening = tpl_key in ("warmup", "warmup_offhours") and update.get("stage") == "SCREENING"
        if cand.get("phone") and not _is_warmup_to_screening:
            try:
                from email_service import get_template_for_key, build_template_vars, render_template
                from sms_service import send_candidate_sms
                _settings = await resolve_settings(user["id"], cand.get("pipeline_id"))
                live_tpl = get_template_for_key(_settings, tpl_key) or {}
                if not (live_tpl.get("sms_enabled") is False or live_tpl.get("enabled") is False):
                    sms_body_tpl = live_tpl.get("sms_body") or ""
                    if sms_body_tpl:
                        job = await db.jobs.find_one({"id": cand["job_id"]}, {"_id": 0}) if cand.get("job_id") else None
                        body = render_template(sms_body_tpl, build_template_vars(cand, _settings, job))
                        await send_candidate_sms(db, _settings, cand, body, template_key=tpl_key)
            except Exception as _e:
                logger.warning(f"stage sms ({tpl_key}) failed: {_e}")
    # Reconcile appointment-reminder jobs to the new state. There are three cases:
    # 1) appointment_at was just set/changed → cancel old jobs, arm new ones
    # 2) stage moved AWAY from APPOINTMENT (and appointment_at didn't change) →
    #    cancel pending reminders so we don't ping the candidate about a
    #    meeting they're no longer in the funnel for. We do NOT auto-clear
    #    `appointment_at` itself (that's the recruiter's call) — clearing the
    #    APScheduler jobs is sufficient because `send_appointment_reminder`
    #    also defensively skips when stage != APPOINTMENT.
    # 3) Otherwise — leave the existing jobs alone (e.g. a recruiter just
    #    edited rating).
    new_appointment_at = update.get("appointment_at")
    new_stage = update.get("stage", prev_stage)

    # Entering SCREENING by drag goes through start_screening — the SAME
    # mode-aware entry point every creation route uses. This block used to
    # call the dialer directly with the legacy warmup delay, so a recruiter
    # re-firing a candidate on a chat-first pipeline got the OLD process: a
    # "we'll call you in 5 minutes" email and a call countdown, no arrival
    # text. Found live when a resume-only candidate had her contact details
    # filled in and was dragged out and back to start her screening.
    # Entering FORM by drag gets the same 2-hour completion reminder the
    # attendance buttons arm. This was one office's mystery: their workflow
    # dragged cards to FORM on the kanban, which sent the form comms and never
    # scheduled the chaser — every dragged candidate (attendance_status None)
    # went unreminded while every buttoned one (attended_form) got theirs.
    if new_stage == "FORM" and prev_stage != "FORM":
        try:
            from auto_dialer import schedule_form_reminder
            await schedule_form_reminder(user["id"], candidate_id)
        except Exception as e:
            logger.warning(f"form-reminder scheduling on /move failed: {e}")

    auto_call_scheduled = None
    if new_stage == "SCREENING" and prev_stage != "SCREENING":
        try:
            settings = await resolve_settings(user["id"], cand.get("pipeline_id"))
            from screening_start import start_screening
            fresh_cand = await db.candidates.find_one(
                {"id": candidate_id, "user_id": user["id"]}, {"_id": 0}) or cand
            result = await start_screening(user["id"], fresh_cand, settings)
            auto_call_scheduled = result.get("dial")
            logger.info(f"screening (re)started via /move for {candidate_id}: mode={result.get('mode')}")
        except Exception as e:
            logger.warning(f"auto-schedule call on move-to-SCREENING failed: {e}")
    try:
        from auto_dialer import cancel_appointment_reminders, schedule_appointment_reminders, cancel_pending_retry_calls
        if new_appointment_at and new_appointment_at != prev_appointment_at:
            cancel_appointment_reminders(candidate_id)
            await schedule_appointment_reminders(user["id"], candidate_id, new_appointment_at)
            cancel_pending_retry_calls(candidate_id)
        elif "stage" in update and (
            (prev_stage == "APPOINTMENT" and new_stage != "APPOINTMENT")
            or new_stage == "TRAINING"
        ):
            removed = cancel_appointment_reminders(candidate_id)
            if removed:
                logger.info(
                    f"cancelled {removed} reminder job(s) for {candidate_id} — "
                    f"stage move {prev_stage}→{new_stage}"
                )
    except Exception as e:
        logger.warning(f"reminder reschedule on /move failed: {e}")

    # When moved to TRAINING: send starter email directly from CGRecruit, then
    # also fire the CG1 webhook for roster sync (independently — email no longer
    # depends on CG1 being up).
    cg1_starter_email_id = None
    if new_stage == "TRAINING":
        training_start_at = getattr(payload, "training_start_at", None) or ""
        skip_notifications = bool(getattr(payload, "skip_notifications", False))
        if training_start_at:
            sms_fields: dict = {}
            if skip_notifications:
                # Mark email as sent and suppress the 3h SMS for past-date legacy imports.
                # Only suppress SMS if their start time has already passed.
                from training_sms import _parse_training_dt as _parse_tdt
                import datetime as _dt_mod
                from zoneinfo import ZoneInfo as _ZI
                tdt = _parse_tdt(training_start_at)
                now_et_local = _dt_mod.datetime.now(app_zone())
                if tdt and tdt < now_et_local:
                    sms_fields["sms_confirmation_sent_at"] = now_iso()
                    sms_fields["sms_confirmation_status"] = "migrated"
            await db.candidates.update_one(
                {"id": candidate_id},
                {"$set": {"training_start_at": training_start_at, "email_sent": skip_notifications, "updated_at": now_iso(), **sms_fields}},
            )
        # Per-person schedule overrides from the Training modal.
        extra_tpl = {
            k: getattr(payload, k, None)
            for k in ("monday_start", "monday_end", "tuesday_start", "tuesday_end")
            if getattr(payload, k, None)
        }
        if not skip_notifications:
            # 1. Send the starter email directly from CGRecruit.
            starter_email_status = await _send_starter_email_direct(
                user_id=user["id"], candidate_id=candidate_id,
                training_start_at=training_start_at,
                extra_template=extra_tpl or None,
            )
            if starter_email_status.get("status") == "sent":
                email_status = starter_email_status
        # 2. Always notify CG1 for roster sync — this creates the new-hire record
        # so the trainee can access assessments. Fires regardless of skip_notifications
        # since roster sync is independent of email/SMS notification sending.
        cg1_starter_email_id = await _fire_cg1_training_webhook(
            user_id=user["id"], candidate_id=candidate_id,
            training_start_at=training_start_at,
            extra_template=extra_tpl or None,
        )
        # 3. Append to NEW HIRES Google Sheet (non-fatal if Sheets is down).
        if not skip_notifications:
            try:
                from sheets_service import append_new_hire
                pipe = await db.pipelines.find_one({"id": cand.get("pipeline_id")}, {"_id": 0}) or {}
                office_key = company_profile.office_key_for_pipeline(pipe)
                if office_key:
                    await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: append_new_hire(
                            office_key=office_key,
                            name=f"{cand.get('first_name','')} {cand.get('last_name','')}".strip(),
                            email=cand.get("email") or "",
                            phone=cand.get("phone") or "",
                            training_start_at=training_start_at,
                            interviewer=user.get("name") or user.get("email") or "",
                        )
                    )
            except Exception as _se:
                logger.warning(f"sheets_service append failed (non-fatal): {_se}")

    if update.get("appointment_at"):
        from screening_outcome import ensure_screening_outcome_later
        ensure_screening_outcome_later(db, candidate_id)
    await broadcast(user["id"])
    return {"ok": True, "candidate": cand, "email": email_status, "auto_call_scheduled": auto_call_scheduled, "cg1_starter_email_id": cg1_starter_email_id}


@api.post("/candidates/{candidate_id}/reschedule-training", dependencies=[Depends(candidate_write_access)])
async def reschedule_training(candidate_id: str, payload: Dict[str, Any] = Body(...), user: dict = Depends(current_user)):
    """Reschedule a training start date for a candidate already in TRAINING.

    Body: { "training_start_at": "<ISO 8601>" }

    Updates training_start_at on the CGRecruit candidate, then calls CG1 to
    update the linked starter_email record and re-fire the starter email.
    """
    import os, httpx as _httpx
    training_start_at = (payload.get("training_start_at") or "").strip()
    if not training_start_at:
        raise HTTPException(400, "training_start_at is required")

    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")
    if cand.get("stage") != "TRAINING":
        raise HTTPException(400, "Candidate is not in the TRAINING stage")

    # Parse ISO → date/time strings for CG1
    start_date, start_time = "", ""
    try:
        dt = datetime.fromisoformat(training_start_at.replace("Z", "+00:00"))
        start_date = dt.strftime("%Y-%m-%d")
        start_time = dt.strftime("%-I:%M %p")
    except Exception:
        start_date = training_start_at[:10]
        start_time = ""

    await db.candidates.update_one(
        {"id": candidate_id},
        {
            "$set": {
                "training_start_at": training_start_at,
                # Count every channel's reschedules in one place — the CG1
                # repeat-reschedule threshold below reads this same field the
                # SMS and phone paths increment.
                "sms_reschedule_count": int(cand.get("sms_reschedule_count") or 0) + 1,
                "updated_at": now_iso(),
            },
            "$unset": {"sms_confirmation_sent_at": ""},
        },
    )

    # No CG1 page for repeat reschedules (owner's rule, 2026-08-18): a
    # reschedule is a status change, not trainee correspondence, and the CG1
    # roster reflects the new date on its own.

    # CGRecruit is the authoritative sender of the starter/logistics email — CG1's
    # own /new-starter webhook is now a no-op on their side ("CGRecruit sends the
    # email itself"), so the CG1 reschedule webhook this used to call had nothing
    # to update for almost every candidate (no cg1_starter_email_id ever gets set).
    # Send the real email directly, the same function the initial TRAINING move uses.
    starter_email_status = await _send_starter_email_direct(
        user_id=user["id"], candidate_id=candidate_id, training_start_at=training_start_at,
    )
    email_status = starter_email_status.get("status", "failed")
    if email_status != "sent":
        logger.warning(f"reschedule-training: starter email not sent for {candidate_id}: {starter_email_status}")

    # CG1 roster sync stays independent of the email send — still worth notifying
    # so the Live Roster reflects the new date, even though CG1 no longer emails.
    cg1_url = os.getenv("CG1_BACKEND_URL", "").rstrip("/")
    secret = os.getenv("CG1_WEBHOOK_SECRET", "")
    cg1_status = "not_configured"
    cg1_starter_email_id = cand.get("cg1_starter_email_id") or ""
    if cg1_starter_email_id and cg1_url:
        try:
            async with _httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    f"{cg1_url}/api/webhooks/cgrecruit/starter/{cg1_starter_email_id}/reschedule",
                    json={"start_date": start_date, "start_time": start_time},
                    headers={"x-webhook-secret": secret},
                )
                r.raise_for_status()
            cg1_status = "updated"
        except Exception as e:
            logger.warning(f"CG1 reschedule webhook failed for {candidate_id}: {e}")
            cg1_status = f"failed:{e}"

    # Update Google Sheet WE date for this candidate (non-fatal)
    try:
        import asyncio as _asyncio
        from sheets_service import update_hire_start_date as _update_hire_start_date
        pipe = await db.pipelines.find_one({"id": cand.get("pipeline_id")}, {"_id": 0}) or {}
        office_key = company_profile.office_key_for_pipeline(pipe)
        if office_key:
            cand_name = f"{cand.get('first_name', '')} {cand.get('last_name', '')}".strip()
            await _asyncio.get_event_loop().run_in_executor(
                None,
                lambda: _update_hire_start_date(
                    office_key=office_key,
                    email=cand.get("email") or "",
                    name=cand_name,
                    training_start_at=training_start_at,
                ),
            )
    except Exception as _se:
        logger.warning(f"sheets_service update_hire_start_date failed (non-fatal): {_se}")

    fresh = await db.candidates.find_one({"id": candidate_id}, {"_id": 0})
    return {"ok": True, "candidate": fresh, "cg1_status": cg1_status, "email_status": email_status}


@api.post("/candidates/{candidate_id}/resync-cg1", dependencies=[Depends(candidate_write_access)])
async def resync_cg1(candidate_id: str, user: dict = Depends(current_user)):
    """Re-fire the CG1 new-starter webhook for a TRAINING candidate who is missing
    a new-hire record in CG1 (e.g. was moved with skip_notifications=True)."""
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")
    if cand.get("stage") != "TRAINING":
        raise HTTPException(400, "Candidate is not in the TRAINING stage")
    training_start_at = cand.get("training_start_at") or ""
    cg1_id = await _fire_cg1_training_webhook(
        user_id=user["id"], candidate_id=candidate_id,
        training_start_at=training_start_at,
    )
    if not cg1_id:
        raise HTTPException(502, "CG1 sync failed — check CG1_BACKEND_URL config or server logs")
    return {"ok": True, "cg1_starter_email_id": cg1_id}


@api.post("/candidates/{candidate_id}/resend-starter-email", dependencies=[Depends(candidate_write_access)])
async def resend_starter_email(
    candidate_id: str,
    payload: Dict[str, Any] = Body(default={}),
    user: dict = Depends(current_user),
):
    """Re-send the starter/logistics email to someone already booked into TRAINING.

    `/reschedule-training` also re-sends this email, but only as a side effect of
    moving the start date: it rewrites training_start_at, bumps the reschedule
    counter, clears the confirmation-SMS flag, pokes CG1 and rewrites the NEW
    HIRES sheet row. When the date has not moved and only the *details* have —
    an intake whose day-two hours changed after they were booked — every one of
    those is a wrong write. This door sends the email and nothing else.

    Body (all optional): any starter-template field to layer over this one send,
    e.g. {"tuesday_start": "9:30 AM", "custom_notes": "..."}. Dated exceptions in
    `starter_date_overrides` still win over these, so a resend cannot quietly
    contradict what a new booking for the same day would say.
    """
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")
    if cand.get("stage") != "TRAINING":
        raise HTTPException(400, f"Candidate is not in the TRAINING stage (current: {cand.get('stage')})")
    if cand.get("archived_at"):
        raise HTTPException(400, "Candidate is archived — un-archive them before re-sending their starter email")
    training_start_at = cand.get("training_start_at") or ""
    if not training_start_at:
        raise HTTPException(400, "Candidate has no training start date")
    overrides = {k: str(payload[k]) for k in _TEMPLATE_KEYS if payload.get(k)}
    result = await _send_starter_email_direct(
        user_id=user["id"], candidate_id=candidate_id,
        training_start_at=training_start_at,
        extra_template=overrides or None,
    )
    if result.get("status") != "sent":
        raise HTTPException(502, f"Starter email not sent: {result}")
    return {"ok": True, "training_start_at": training_start_at, "email": result}


@api.post("/candidates/{candidate_id}/reschedule", dependencies=[Depends(candidate_write_access)])
async def reschedule_candidate(candidate_id: str, payload: Dict[str, Any] = Body(...), user: dict = Depends(current_user)):
    """Recruiter-initiated reschedule of a booked appointment.

    Mirrors the candidate-side reschedule (`/api/public/applicant/{token}/reschedule`)
    but driven from the recruiter's drawer. Updates appointment_at, optionally
    appointment_link / appointment_recruiter, cancels old reminder jobs, schedules
    new ones for the new slot, and fires the `appointment_rescheduled` email + SMS
    template (falling back to `confirmation` if the dedicated template is empty).

    Body:
        appointment_at: ISO 8601 string (required)
        appointment_link: optional new Zoom/Meet link
        appointment_recruiter: optional new recruiter name shown on the email
        send_notification: bool, default True. If False, just updates the slot
            silently (useful for "I already messaged them on WhatsApp" cases).
    """
    new_at = (payload.get("appointment_at") or "").strip()
    if not new_at:
        raise HTTPException(400, "appointment_at is required")
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")
    if cand.get("stage") not in ("APPOINTMENT", "FORM"):
        raise HTTPException(
            400,
            f"Reschedule only available when candidate is on the Appointment funnel (current: {cand.get('stage')}). "
            f"Move them back to Appointment first to assign a new slot.",
        )

    # Validate the new slot is parseable; we do NOT enforce capacity caps here
    # (recruiter manual override = trust the human).
    try:
        from datetime import datetime as _dt
        _parsed_at = _dt.fromisoformat(new_at.replace("Z", "+00:00"))
    except Exception:
        raise HTTPException(400, "appointment_at must be a valid ISO 8601 datetime")
    # A naive timestamp used to slip through here and crash reminder scheduling
    # later (aware-vs-naive compare) — swallowed by the caller, so the candidate
    # simply got zero reminders. Same rule the portal doors already enforce.
    if _parsed_at.tzinfo is None:
        raise HTTPException(400, "appointment_at must include a timezone offset (e.g. ...T14:00:00-04:00 or ...Z)")

    prev_at = cand.get("appointment_at")
    update: Dict[str, Any] = {
        "appointment_at": new_at,
        "previous_appointment_at": prev_at,
        "rescheduled": True,
        "stage": "APPOINTMENT",   # safety: in case stage was FORM, snap them back
        "updated_at": now_iso(),
        "attendance_status": None,             # allow reminders for rescheduled no-shows
        "appointment_sms_confirmed": None,     # reset so chaser fires for the new slot
        "appointment_sms_confirmed_at": None,
        # Clear any "can't make it" flag from the OLD slot — the reminder engine
        # skips every send while appointment_cancelled_at is set, so a recruiter
        # rebooking a cancelled candidate was silently muting all their reminders.
        "appointment_cancelled_at": None,
        "appointment_cancel_reason": None,
    }
    if payload.get("appointment_link") is not None:
        update["appointment_link"] = payload["appointment_link"]
    else:
        # No explicit link: a slot-level override on the NEW slot wins (their
        # stored link belongs to the old slot); else keep the candidate's link;
        # else pull from pipeline so reminders never send blank.
        pipe = await db.pipelines.find_one(
            {"id": cand.get("pipeline_id"), "user_id": user["id"]},
            {"_id": 0, "appointment_link": 1, "appointment_recruiter": 1, "availability_rules": 1},
        ) or {}
        from availability_service import resolve_slot_link, resolve_slot_recruiter
        _settings = await resolve_settings(user["id"], cand.get("pipeline_id"))
        _tz = (_settings.get("region_language") or {}).get("timezone") or default_tz_name()
        _slot_link = resolve_slot_link(pipe, new_at, _tz)
        if _slot_link:
            update["appointment_link"] = _slot_link
        elif pipe.get("appointment_link"):
            # Slot has no override — reset to the pipeline default rather than
            # leaving the OLD slot's override on the candidate.
            update["appointment_link"] = pipe["appointment_link"]
        # Same override-or-default rule for who runs the NEW slot.
        if payload.get("appointment_recruiter") is None:
            _slot_rec = resolve_slot_recruiter(pipe, new_at, _tz)
            if _slot_rec:
                update["appointment_recruiter"] = _slot_rec
            elif pipe.get("appointment_recruiter"):
                update["appointment_recruiter"] = pipe["appointment_recruiter"]
    if payload.get("appointment_recruiter") is not None:
        update["appointment_recruiter"] = payload["appointment_recruiter"]

    # Audit trail in chat_log so the drawer's Comms tab shows what happened.
    chat = list(cand.get("chat_log") or [])
    chat.append({
        "role": "agent",
        "text": (
            f"Recruiter ({user.get('name') or user.get('email')}) rescheduled appointment "
            f"from {prev_at or 'unset'} → {new_at}."
        ),
        "at": now_iso(),
    })
    update["chat_log"] = chat

    # Keep the outcome being cleared: the two-strike no-show cap and every
    # attendance report read attendance_history, and this door was destroying
    # the recorded answer instead of preserving it (the other doors already do).
    from screening_outcome import preserve_attendance
    _write: Dict[str, Any] = {"$set": update}
    _keep = preserve_attendance(cand)
    if _keep:
        _write["$push"] = _keep
    await db.candidates.update_one({"id": candidate_id, "user_id": user["id"]}, _write)
    fresh = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})

    # ---- Comms ----
    notification_results: Dict[str, Any] = {}
    if payload.get("send_notification", True):
        # Pick `appointment_rescheduled` if defined+enabled, else fall back to
        # `approval` (the actively-fired booking template). The dedicated
        # `confirmation` key was retired in v34.5 — it was vestigial.
        from email_service import get_template_for_key, build_template_vars, render_template
        from sms_service import send_candidate_sms
        settings = await resolve_settings(user["id"], fresh.get("pipeline_id"))
        tpl_key = "appointment_rescheduled"
        tpl = (settings.get("applicant_comms") or {}).get("templates", {}).get(tpl_key) or {}
        if not tpl.get("body") or tpl.get("enabled") is False:
            tpl_key = "approval"  # fallback
        # Email
        try:
            email_res = await _send_stage_email(user_id=user["id"], candidate=fresh, template_key=tpl_key)
            notification_results["email"] = email_res
        except Exception as e:
            logger.warning(f"reschedule email failed: {e}")
            notification_results["email"] = {"status": "failed", "error": str(e)}
        # SMS (mirrors the no-show reschedule flow)
        if fresh.get("phone"):
            try:
                live_tpl = get_template_for_key(settings, tpl_key) or {}
                if live_tpl.get("sms_enabled") is False or live_tpl.get("enabled") is False:
                    notification_results["sms"] = {"status": "skipped", "reason": "sms channel off"}
                else:
                    sms_body_tpl = live_tpl.get("sms_body") or ""
                    if not sms_body_tpl:
                        notification_results["sms"] = {"status": "skipped", "reason": "no sms_body"}
                    else:
                        job = None
                        if fresh.get("job_id"):
                            job = await db.jobs.find_one({"id": fresh["job_id"], "user_id": user["id"]}, {"_id": 0})
                        body = render_template(sms_body_tpl, build_template_vars(fresh, settings, job))
                        notification_results["sms"] = await send_candidate_sms(
                            db, settings, fresh, body, template_key=tpl_key,
                        )
            except Exception as e:
                logger.warning(f"reschedule sms failed: {e}")
                notification_results["sms"] = {"status": "failed", "error": str(e)}

    # ---- Reminder jobs: cancel old, schedule new ----
    try:
        from auto_dialer import cancel_appointment_reminders, schedule_appointment_reminders
        cancel_appointment_reminders(candidate_id)
        await schedule_appointment_reminders(user["id"], candidate_id, new_at)
    except Exception as e:
        logger.warning(f"reschedule reminder reset failed: {e}")

    from screening_outcome import ensure_screening_outcome_later
    ensure_screening_outcome_later(db, candidate_id)

    return {
        "ok": True,
        "candidate": fresh,
        "previous_appointment_at": prev_at,
        "appointment_at": new_at,
        "notification": notification_results,
    }



@api.post("/candidates/{candidate_id}/auto-promote/cancel", dependencies=[Depends(candidate_write_access)])
async def cancel_candidate_auto_promote(candidate_id: str, user: dict = Depends(current_user)):
    """Cancel the pending v32 delayed auto-promote for this candidate. The
    candidate keeps screening_status=approved + appointment_at set so the
    Approve/Deny pills remain — we just stop the 2-hour timer."""
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")
    from auto_dialer import cancel_auto_promote
    cancel_auto_promote(candidate_id, reason="user_cancelled")
    # Also clear the FE-visible fields so the countdown disappears immediately.
    await db.candidates.update_one(
        {"id": candidate_id, "user_id": user["id"]},
        {"$set": {
            "auto_promote_at": None,
            "auto_promote_status": "cancelled:user",
            "updated_at": now_iso(),
        }},
    )
    fresh = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    return {"ok": True, "candidate": fresh}


@api.post("/candidates/{candidate_id}/score", dependencies=[Depends(candidate_write_access)])
async def score_candidate(candidate_id: str, user: dict = Depends(current_user)):
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")
    if not cand.get("job_id"):
        raise HTTPException(400, "Candidate has no job_id assigned")
    job = await db.jobs.find_one({"id": cand["job_id"], "user_id": user["id"]}, {"_id": 0})
    if not job:
        raise HTTPException(404, "Job not found")
    parsed = cand.get("parsed_resume") or {}
    res = await smart_score(parsed, job, candidate_id)
    await db.candidates.update_one(
        {"id": candidate_id, "user_id": user["id"]},
        {"$set": {"smart_score": res.get("score"), "smart_score_rationale": res.get("rationale", ""), "updated_at": now_iso()}},
    )
    return res


@api.delete("/candidates/{candidate_id}", dependencies=[Depends(candidate_write_access)])
async def delete_candidate(candidate_id: str, user: dict = Depends(current_user)):
    """Hard-delete a candidate + all their related rows. Also cancels every
    pending APScheduler job tied to them so they don't get phantom reminders
    / retry calls / auto-promotes after they're gone."""
    require_mover(user)
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")
    # Cancel all scheduled jobs for this candidate before tearing down the doc.
    # Each helper is idempotent (no-ops if the job doesn't exist).
    try:
        from auto_dialer import (
            cancel_appointment_reminders,
            cancel_pending_retry_calls,
            cancel_auto_promote,
        )
        cancel_appointment_reminders(candidate_id)
        cancel_pending_retry_calls(candidate_id)
        cancel_auto_promote(candidate_id, reason="candidate_deleted")
    except Exception as e:
        logger.warning(f"failed to cancel scheduled jobs for {candidate_id}: {e}")
    # Wipe the candidate's child collections too.
    await db.candidates.delete_one({"id": candidate_id, "user_id": user["id"]})
    await db.communications.delete_many({"candidate_id": candidate_id, "user_id": user["id"]})
    await db.conversations.delete_many({"candidate_id": candidate_id, "user_id": user["id"]})
    return {"ok": True, "candidate_id": candidate_id}


@api.post("/candidates/{candidate_id}/archive", dependencies=[Depends(candidate_write_access)])
async def archive_candidate(
    candidate_id: str,
    payload: dict = Body(default={}),
    user: dict = Depends(current_user),
):
    """Soft-reject a candidate. Hides them from the dashboard, drops them
    from auto-dial / retry queues, and cancels their scheduled jobs — but
    KEEPS the doc + transcripts + communications history so the recruiter
    can restore later. Pair with POST /restore to undo.

    Use this in place of hard-delete unless the recruiter really wants the
    data gone (GDPR right-to-erasure, etc.)."""
    require_mover(user)
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")
    if cand.get("archived_at"):
        return {"ok": True, "already_archived": True}
    reason = (payload or {}).get("reason", "manual")
    # Cancel anything queued — they shouldn't get follow-up calls/SMS while archived.
    try:
        from auto_dialer import (
            cancel_appointment_reminders, cancel_pending_retry_calls, cancel_auto_promote,
        )
        cancel_appointment_reminders(candidate_id)
        cancel_pending_retry_calls(candidate_id)
        cancel_auto_promote(candidate_id, reason="candidate_archived")
    except Exception as e:
        logger.warning(f"archive: cancel scheduled jobs failed for {candidate_id}: {e}")
    await db.candidates.update_one(
        {"id": candidate_id, "user_id": user["id"]},
        {"$set": {
            "archived_at": now_iso(),
            "archived_reason": str(reason)[:120],
            "auto_dial": False,
            "updated_at": now_iso(),
        }},
    )
    return {"ok": True, "candidate_id": candidate_id, "archived_at": now_iso()}


@api.post("/candidates/{candidate_id}/reject", dependencies=[Depends(candidate_write_access)])
async def reject_candidate(
    candidate_id: str,
    payload: dict = Body(default={}),
    user: dict = Depends(current_user),
):
    """Reject a TO CLOSE candidate: send the rejection email (email only — no SMS)
    and soft-archive them so they drop off the board and are no longer progressed.
    Mirrors /archive's job cancellation + soft-reject, adding the rejection email
    and a `close_rejection_at` stamp. Reversible via /restore."""
    require_mover(user)
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")
    if cand.get("archived_at"):
        return {"ok": True, "already_archived": True}
    # 1. Send the rejection email (email only). Don't let a mail hiccup block the
    #    archive — log and continue so the candidate is still removed from the board.
    email_res = {"status": "skipped"}
    try:
        email_res = await _send_stage_email(user_id=user["id"], candidate=cand, template_key="close_rejection")
    except Exception as e:
        logger.warning(f"reject: rejection email failed for {candidate_id}: {e}")
        email_res = {"status": "failed", "error": str(e)}
    # 2. Cancel anything queued — no follow-up calls/SMS after rejection.
    try:
        from auto_dialer import (
            cancel_appointment_reminders, cancel_pending_retry_calls, cancel_auto_promote,
        )
        cancel_appointment_reminders(candidate_id)
        cancel_pending_retry_calls(candidate_id)
        cancel_auto_promote(candidate_id, reason="candidate_rejected")
    except Exception as e:
        logger.warning(f"reject: cancel scheduled jobs failed for {candidate_id}: {e}")
    # 3. Soft-archive with reason='rejected' + stamp close_rejection_at.
    ts = now_iso()
    await db.candidates.update_one(
        {"id": candidate_id, "user_id": user["id"]},
        {"$set": {
            "archived_at": ts,
            "archived_reason": "rejected",
            "close_rejection_at": ts,
            "auto_dial": False,
            "updated_at": ts,
        }},
    )
    return {"ok": True, "candidate_id": candidate_id, "archived_at": ts, "email": email_res}


@api.post("/candidates/{candidate_id}/restore", dependencies=[Depends(candidate_write_access)])
async def restore_candidate(candidate_id: str, user: dict = Depends(current_user)):
    """Undo a soft-reject. Brings the candidate back into the dashboard with
    their stage / screening_status / appointment intact. Does NOT
    automatically re-queue calls — the recruiter can re-trigger from the
    drawer if needed (avoids surprise dialing after a long-archived
    candidate is restored)."""
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")
    if not cand.get("archived_at"):
        return {"ok": True, "already_active": True}
    await db.candidates.update_one(
        {"id": candidate_id, "user_id": user["id"]},
        {"$set": {"archived_at": None, "archived_reason": None, "updated_at": now_iso()}},
    )
    return {"ok": True, "candidate_id": candidate_id}


@api.post("/candidates/bulk-unarchive-retry")
async def bulk_unarchive_retry(
    payload: dict = Body(...),
    user: dict = Depends(current_user),
):
    """Restore a batch of archived candidates and re-queue each for a fresh
    screening call — resets call_attempts/verdict/DND state so the retry
    cadence starts over like a first attempt, then schedules the calls
    staggered within the pipeline's call window (mirrors /batch-dial's
    stagger so we don't blow the concurrency cap dialing everyone at once).

    SMS opt-outs (STOP replies) live in the global `sms_optouts` collection
    and are enforced unconditionally by sms_service.send_candidate_sms — this
    endpoint re-enables calls/emails for the candidate but can never cause a
    text to reach a number that actually opted out."""
    require_mover(user)
    ids = [str(i) for i in (payload.get("candidate_ids") or []) if i]
    if not ids:
        raise HTTPException(400, "candidate_ids required")

    interval_cache: Dict[str, int] = {}
    results = []
    queued_idx = 0
    for candidate_id in ids:
        cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
        if cand:
            try:
                await assert_candidate_access(user, cand)
            except HTTPException:
                cand = None  # out of this user's scope: report it like a missing id
        if not cand:
            results.append({"candidate_id": candidate_id, "status": "not_found"})
            continue

        await db.candidates.update_one(
            {"id": candidate_id, "user_id": user["id"]},
            {
                "$set": {
                    "archived_at": None,
                    "archived_reason": None,
                    "auto_dial": True,
                    "stage": "SCREENING",
                    "screening_status": "queued",
                    "call_attempts": 0,
                    "verdict": None,
                    "updated_at": now_iso(),
                },
                "$unset": {"dnd_retry_active": "", "dnd_retry_for_attempt": ""},
            },
        )

        if not cand.get("phone"):
            results.append({"candidate_id": candidate_id, "status": "restored_no_phone"})
            continue

        pipeline_id = cand.get("pipeline_id")
        if pipeline_id not in interval_cache:
            settings = await resolve_settings(user["id"], pipeline_id)
            interval_cache[pipeline_id] = int((settings.get("auto_dialer") or {}).get("batch_interval_seconds", 30))
        delay_min = (queued_idx * interval_cache[pipeline_id]) // 60
        queued_idx += 1
        sched = await schedule_call_with_window(user["id"], candidate_id, base_delay_minutes=delay_min)
        results.append({"candidate_id": candidate_id, "status": "queued", **sched})

    await broadcast(user["id"])
    queued = sum(1 for r in results if r["status"] == "queued")
    return {"ok": True, "queued": queued, "total": len(ids), "results": results}


@api.post("/candidates/{candidate_id}/resend-screening-link", dependencies=[Depends(candidate_write_access)])
async def resend_screening_link(candidate_id: str, user: dict = Depends(current_user)):
    """Manually re-fire the screening retry email + SMS (self-schedule link).
    Bypasses the idempotency stamp so it always sends fresh."""
    require_mover(user)
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")
    # Clear the retry stamp so maybe_fire_screening_retry won't skip it
    await db.candidates.update_one(
        {"id": candidate_id},
        {"$unset": {"screening_retry_sent_at": ""}, "$set": {"updated_at": now_iso()}},
    )
    from deps import maybe_fire_screening_retry
    result = await maybe_fire_screening_retry(user["id"], candidate_id)
    if result.get("status") == "skipped":
        raise HTTPException(400, result.get("reason", "Could not send — check candidate status"))
    return {"ok": True, "result": result}


@api.post("/candidates/{candidate_id}/merge", dependencies=[Depends(candidate_write_access)])
async def merge_candidate(
    candidate_id: str,
    payload: dict = Body(...),
    user: dict = Depends(current_user),
):
    """Merge `source_id` INTO `candidate_id`. The candidate path here is the
    *winner* — the source is archived after its data is folded in.

    Merge rules:
      - winner keeps its own stage / appointment / screening_status (those
        often differ across pipelines and we don't want to clobber a strong
        record with a weaker one)
      - winner picks up `chat_log`, `form_responses` from the source if its
        own slots are empty (one-way fill)
      - communications + conversations are re-pointed to the winner so the
        full timeline lives on one record
      - source candidate is soft-archived with reason='merged_into:{winner_id}'

    Recruiters use this when cross-pipeline duplicate detection flags two
    candidates that are actually the same person who applied to multiple
    offices."""
    source_id = (payload or {}).get("source_id")
    if not source_id or source_id == candidate_id:
        raise HTTPException(400, "source_id is required and must differ from the target")
    winner = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    source = await db.candidates.find_one({"id": source_id, "user_id": user["id"]}, {"_id": 0})
    if not winner or not source:
        raise HTTPException(404, "Candidate(s) not found")
    # The winner is checked by the route dependency; the source must be in
    # the caller's scope too, or a merge would pull in someone else's record.
    await assert_candidate_access(user, source)
    # One-way fill of empty slots on the winner from the source.
    fill: Dict[str, Any] = {}
    for k in ("phone", "email", "last_name", "resume_url", "resume_text", "parsed_resume"):
        if not winner.get(k) and source.get(k):
            fill[k] = source[k]
    if not (winner.get("chat_log") or []) and source.get("chat_log"):
        fill["chat_log"] = source["chat_log"]
    if not winner.get("form_responses") and source.get("form_responses"):
        fill["form_responses"] = source["form_responses"]
    fill["updated_at"] = now_iso()
    if fill:
        await db.candidates.update_one({"id": candidate_id}, {"$set": fill})
    # Re-point child docs onto the winner.
    await db.communications.update_many(
        {"candidate_id": source_id}, {"$set": {"candidate_id": candidate_id}}
    )
    await db.conversations.update_many(
        {"candidate_id": source_id}, {"$set": {"candidate_id": candidate_id}}
    )
    # Cancel any scheduled jobs on the source so they don't fire post-merge.
    try:
        from auto_dialer import (
            cancel_appointment_reminders, cancel_pending_retry_calls, cancel_auto_promote,
        )
        cancel_appointment_reminders(source_id)
        cancel_pending_retry_calls(source_id)
        cancel_auto_promote(source_id, reason="merged")
    except Exception as e:
        logger.warning(f"merge: cancel jobs on source {source_id} failed: {e}")
    # Soft-archive the source — leaves a paper trail.
    await db.candidates.update_one(
        {"id": source_id, "user_id": user["id"]},
        {"$set": {
            "archived_at": now_iso(),
            "archived_reason": f"merged_into:{candidate_id}",
            "auto_dial": False,
            "updated_at": now_iso(),
        }},
    )
    return {"ok": True, "winner_id": candidate_id, "archived_source_id": source_id}


@api.get("/duplicates")
async def list_duplicate_groups(user: dict = Depends(current_user)):
    """Return groups of cross-pipeline duplicate candidates for the merge UI.

    A group = all active candidates of this tenant that share an email OR a
    last-10-digit phone fingerprint AND span more than one pipeline.
    Soft-archived and CLOSE-stage candidates are excluded.

    Endpoint NOTE: deliberately registered at `/duplicates` (NOT
    `/candidates/duplicates`) because FastAPI matches routes in declaration
    order — the existing `/candidates/{candidate_id}` registered earlier
    would steal a path-param call and 404 with "Candidate not found"."""
    q: Dict[str, Any] = {
        "user_id": user["id"],
        "stage": {"$nin": ["CLOSE"]},
        "$or": [{"archived_at": None}, {"archived_at": {"$exists": False}}],
    }
    if not is_super_admin(user):
        q["pipeline_id"] = {"$in": user.get("pipeline_ids") or []}
    cands = await db.candidates.find(
        q, {"_id": 0, "id": 1, "first_name": 1, "last_name": 1, "email": 1, "phone": 1,
             "pipeline_id": 1, "stage": 1, "screening_status": 1, "created_at": 1, "appointment_at": 1},
    ).to_list(5000)
    # Bucket by email AND phone-last10 separately, then union groups that share a member.
    from collections import defaultdict
    by_email: Dict[str, list] = defaultdict(list)
    by_phone: Dict[str, list] = defaultdict(list)
    for c in cands:
        if c.get("email"):
            by_email[c["email"].lower().strip()].append(c)
        if c.get("phone"):
            digits = "".join(ch for ch in c["phone"] if ch.isdigit())
            last10 = digits[-10:]
            if last10:
                by_phone[last10].append(c)
    seen_ids: set = set()
    groups: List[Dict[str, Any]] = []
    for matches in list(by_email.values()) + list(by_phone.values()):
        # Only flag as a duplicate group if there are 2+ candidates spanning 2+ pipelines.
        if len(matches) < 2:
            continue
        pipeline_ids_in_group = {m.get("pipeline_id") for m in matches}
        if len(pipeline_ids_in_group) < 2:
            continue
        # Use the FIRST candidate's id as the group key to dedupe across email/phone.
        key = sorted(m["id"] for m in matches)[0]
        if key in seen_ids:
            continue
        seen_ids.update(m["id"] for m in matches)
        groups.append({
            "group_key": key,
            "candidates": matches,
        })
    return {"groups": groups, "count": len(groups)}


# ===== Calls (ElevenLabs + Twilio) =====
@api.post("/candidates/{candidate_id}/call", dependencies=[Depends(candidate_write_access)])
async def call_candidate(candidate_id: str, user: dict = Depends(current_user)):
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")
    if not cand.get("phone"):
        raise HTTPException(400, "Candidate has no phone number")
    settings = await resolve_settings(user["id"], cand.get("pipeline_id"))
    sca = settings.get("screen_call_agent", {}) or {}
    profile = settings.get("recruiter_profile", {}) or {}
    pipe = await db.pipelines.find_one({"id": cand.get("pipeline_id"), "user_id": user["id"]}, {"_id": 0}) or {}
    job = None
    if cand.get("job_id"):
        job = await db.jobs.find_one({"id": cand["job_id"], "user_id": user["id"]}, {"_id": 0})

    # Resolve agent config — pipeline override > settings default
    agent_id = pipe.get("elevenlabs_agent_id_override") or sca.get("elevenlabs_agent_id", "")
    phone_number_id = pipe.get("elevenlabs_phone_number_id_override") or sca.get("elevenlabs_phone_number_id", "")
    voice_id = pipe.get("voice_id_override") or sca.get("voice_id", "")
    _ = voice_id  # voice baked at sync time, not sent at runtime
    agent_name = pipe.get("agent_name_override") or sca.get("agent_name", "Olivia")

    # Build dynamic vars
    full_name = f"{cand.get('first_name', '')} {cand.get('last_name', '')}".strip()
    dyn = {
        "first_name": cand.get("first_name", ""),
        "full_name": full_name,
        "role": (job or {}).get("title", ""),
        "company": profile.get("company_name", ""),
        "city": pipe.get("name", "").split(",")[0].strip(),
        "agent_name": agent_name,
        "phone": cand.get("phone", ""),
        "email": cand.get("email", ""),
        "pipeline_slug": pipe.get("public_slug", ""),
    }

    # NOTE: We deliberately DO NOT send conversation_config_override at runtime.
    # The user's ElevenLabs agent has overrides disabled (platform_settings.overrides
    # all set to false), and sending an override silently breaks the call.
    # Instead, per-pipeline FAQs are baked into each pipeline's agent at sync time
    # via /api/elevenlabs/agent/sync — runtime customization is via dynamic_variables only.
    overrides: Dict[str, Any] = {}

    # Decide calling mode: "twiml_bridge" lets us call FROM any verified Twilio caller ID
    # (not just Twilio-owned numbers imported into ElevenLabs). The audio is bridged via
    # our backend WebSocket to ElevenLabs Conv AI. See /app/backend/twiml_bridge.py.
    calling_mode = (pipe.get("calling_mode") or "managed").lower()

    if calling_mode == "twiml_bridge":
        from twiml_bridge import place_outbound_call_via_twiml
        from_number = (
            (pipe.get("twilio_phone_number") or "").strip()
            or (sca.get("custom_caller_id") or "").strip()
            or company_profile.default_twilio_number()  # TWILIO_PHONE_NUMBER
        )
        if not from_number:
            raise HTTPException(400, "TwiML bridge mode needs a 'From' number — set the pipeline's Twilio Phone Number")
        if not agent_id:
            raise HTTPException(400, "TwiML bridge mode needs an ElevenLabs agent_id (set in Settings → Screen Call Agent)")
        # Pre-create the conversation row so the WebSocket handler can find it
        conv = Conversation(
            candidate_id=candidate_id, user_id=user["id"],
            status="initiated",
            agent_id=agent_id,
            calling_mode="twiml_bridge",
            dynamic_variables=dyn,
        )
        await db.conversations.insert_one(conv.model_dump())
        public_base = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
        result = place_outbound_call_via_twiml(
            from_number=from_number,
            to_number=cand["phone"],
            conversation_id=conv.id,
            public_base_url=public_base,
        )
        # Patch in the call_sid
        await db.conversations.update_one(
            {"id": conv.id, "user_id": user["id"]},
            {"$set": {
                "twilio_call_sid": result.get("call_sid"),
                "status": "initiated" if result.get("status") == "initiated" else "failed",
            }},
        )
        await db.candidates.update_one(
            {"id": candidate_id, "user_id": user["id"]},
            {"$set": {"stage": "SCREENING", "screening_status": "in_progress" if result.get("status") == "initiated" else "queued", "updated_at": now_iso()}},
        )
        await broadcast(user["id"])
        return {"result": result, "conversation_id": conv.id, "agent_id": agent_id, "calling_mode": "twiml_bridge", "from_number": from_number}

    result = initiate_elevenlabs_outbound_call(
        candidate_phone=cand["phone"],
        agent_id=agent_id,
        agent_phone_number_id=phone_number_id,
        dynamic_variables=dyn,
        conversation_config_override=overrides or None,
    )
    # ElevenLabs answers 2xx for requests it never turns into a call, and the row
    # that leaves behind carries no conversation_id — which is the key the
    # post-call webhook and the auto-sync sweep both match on, so nothing can ever
    # end it. A dial we cannot identify is a failed dial, not a live one.
    if result.get("status") == "initiated" and not result.get("conversation_id"):
        result = {**result, "status": "failed",
                  "error": "ElevenLabs accepted the request but returned no conversation_id"}
    # Persist conversation row
    conv = Conversation(
        candidate_id=candidate_id, user_id=user["id"],
        elevenlabs_conversation_id=result.get("conversation_id"),
        twilio_call_sid=result.get("call_sid"),
        status="initiated" if result.get("status") == "initiated" else "failed",
    )
    await db.conversations.insert_one(conv.model_dump())
    # Update candidate — reset call_attempts so manual retry isn't blocked by the auto-dialer cap
    await db.candidates.update_one(
        {"id": candidate_id, "user_id": user["id"]},
        {"$set": {"stage": "SCREENING", "screening_status": "in_progress" if result.get("status") == "initiated" else "queued", "call_attempts": 0, "updated_at": now_iso()}},
    )
    await broadcast(user["id"])
    return {"result": result, "conversation_id": conv.id, "agent_id": agent_id, "overrides_applied": False}


@api.post("/candidates/{candidate_id}/find-audio", dependencies=[Depends(candidate_write_access)])
async def find_missing_audio(candidate_id: str, user: dict = Depends(current_user)):
    """Backfill: for a conversation that's missing its ElevenLabs conversation_id
    (TwiML Bridge calls placed before we started persisting it on connect), query
    ElevenLabs for recent conversations on the same agent and match by timestamp +
    duration. If exactly one candidate matches, persist the id so audio playback works."""
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    await assert_candidate_access(user, cand)
    convs = await db.conversations.find(
        {"candidate_id": candidate_id, "user_id": user["id"], "elevenlabs_conversation_id": {"$in": [None, ""]}},
        {"_id": 0},
    ).sort("created_at", -1).to_list(20)
    if not convs:
        return {"matched": 0, "reason": "no missing-id conversations"}

    from voice_service import list_recent_elevenlabs_conversations

    matched = 0
    matches: List[Dict[str, Any]] = []
    for conv in convs:
        agent_id = conv.get("agent_id")
        # ElevenLabs lets us filter by agent — much faster than scanning everything.
        candidates_on_el = list_recent_elevenlabs_conversations(agent_id=agent_id, page_size=100)
        if not candidates_on_el:
            continue
        # Parse our conversation's start time (UTC).
        try:
            start_local = datetime.fromisoformat((conv.get("created_at") or "").replace("Z", "+00:00"))
            start_secs = int(start_local.timestamp())
        except Exception:
            continue
        local_dur = int(conv.get("duration_seconds") or 0)

        # Score each EL conversation: closer in time + closer in duration → higher score.
        scored: List[Dict[str, Any]] = []
        for el in candidates_on_el:
            el_start = el.get("start_time_unix_secs") or el.get("start_time") or el.get("created_at_unix")
            if not el_start:
                continue
            try: el_start = int(el_start)
            except Exception: continue
            time_diff = abs(el_start - start_secs)
            if time_diff > 600:  # more than 10 minutes off → almost certainly not the same call
                continue
            el_dur = int(el.get("call_duration_secs") or el.get("duration_seconds") or 0)
            dur_diff = abs(el_dur - local_dur) if local_dur else 0
            scored.append({
                "el_conversation_id": el.get("conversation_id") or el.get("id"),
                "score": time_diff + dur_diff * 2,
                "time_diff": time_diff,
                "dur_diff": dur_diff,
            })

        if not scored:
            continue
        scored.sort(key=lambda x: x["score"])
        best = scored[0]
        # Auto-accept if we have a clear winner: top match within 90s of our timestamp
        # AND either the only candidate, or the second-best is significantly worse.
        # Duration tolerance is loose (Twilio includes ringing, ElevenLabs doesn't).
        ambiguous = (
            best["time_diff"] > 90
            or len(scored) > 1 and (scored[1]["score"] - best["score"]) < 30
        )
        if ambiguous:
            matches.append({"conversation_id": conv["id"], "candidate_match": best, "ambiguous": True})
            continue
        await db.conversations.update_one(
            {"id": conv["id"], "user_id": user["id"]},
            {"$set": {"elevenlabs_conversation_id": best["el_conversation_id"]}},
        )
        matched += 1
        matches.append({"conversation_id": conv["id"], "el_conversation_id": best["el_conversation_id"], **best})

    return {"matched": matched, "candidates_checked": len(convs), "details": matches}


@api.post("/candidates/{candidate_id}/sync-conversation", dependencies=[Depends(candidate_write_access)])
async def sync_conversation(candidate_id: str, user: dict = Depends(current_user)):
    """Pull the latest ElevenLabs conversation transcript and store summary + score."""
    conv = await db.conversations.find_one(
        {"candidate_id": candidate_id, "user_id": user["id"]},
        {"_id": 0}, sort=[("created_at", -1)],
    )
    if not conv or not conv.get("elevenlabs_conversation_id"):
        raise HTTPException(404, "No conversation found for candidate")
    data = await asyncio.get_event_loop().run_in_executor(
        None, fetch_elevenlabs_conversation, conv["elevenlabs_conversation_id"]
    )
    if not data or data.get("error"):
        raise HTTPException(502, f"ElevenLabs fetch failed: {data.get('error', 'unknown')}")
    # Map transcript: ElevenLabs returns {transcript: [{role, message, ...}]}
    raw_transcript = data.get("transcript") or []
    transcript = [{"role": x.get("role", ""), "text": x.get("message", "") or x.get("text", "")} for x in raw_transcript]
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    job = None
    if cand and cand.get("job_id"):
        job = await db.jobs.find_one({"id": cand["job_id"], "user_id": user["id"]}, {"_id": 0})
    duration = data.get("metadata", {}).get("call_duration_secs")
    metadata = data.get("metadata") or {}
    termination_reason = (metadata.get("termination_reason") or "").lower()
    el_status = (data.get("status") or "").lower()
    candidate_spoke = any(t.get("role") in ("user", "human") for t in transcript)
    # Misfire-aware voicemail verdict — see call_classification.resolve_voicemail.
    from call_classification import resolve_voicemail
    voicemail, _vm_misfired = resolve_voicemail("voicemail" in termination_reason, transcript)
    is_complete = candidate_spoke and (duration or 0) > 30 and not voicemail
    conv_status = "completed" if is_complete else "no_answer"
    summary = await summarize_call_transcript(transcript, f"{cand.get('first_name', '')}", (job or {}).get("title", ""), duration_seconds=duration)
    update = {
        "transcript": transcript,
        "summary": summary.get("summary", ""),
        "suitability_score": summary.get("suitability_score"),
        "status": conv_status,
        "duration_seconds": duration,
    }
    await db.conversations.update_one({"id": conv["id"], "user_id": user["id"]}, {"$set": update})
    # Mirror verdict + summary onto the candidate for prominent UI display
    cand_update: Dict[str, Any] = {"updated_at": now_iso()}
    if summary.get("verdict") and is_complete:
        cand_update["verdict"] = summary.get("verdict")
        cand_update["call_summary"] = summary.get("summary", "")
    # Never let syncing an OLD failed call downgrade someone who has since
    # booked or been finalised — "no_answer" is about the call, not about them.
    if not is_complete and cand.get("screening_status") not in ("approved", "rejected") and not cand.get("appointment_at"):
        cand_update["screening_status"] = "no_answer"
    if cand_update:
        await db.candidates.update_one(
            {"id": candidate_id, "user_id": user["id"]},
            {"$set": cand_update},
        )
    # The transcript just landed — finish any paperwork that was deferred on
    # it (a booked candidate flagged "unscreened" while their call sat
    # unsynced in ElevenLabs heals here without anyone doing anything).
    from screening_outcome import ensure_screening_outcome_later
    ensure_screening_outcome_later(db, candidate_id)
    return {"ok": True, "conversation_id": conv["id"], "summary": summary, "transcript": transcript}


@api.get("/candidates/{candidate_id}/conversations")
async def candidate_conversations(candidate_id: str, user: dict = Depends(current_user)):
    await assert_candidate_access(user, await db.candidates.find_one(
        {"id": candidate_id, "user_id": user["id"]}, {"_id": 0, "pipeline_id": 1, "added_by_user_id": 1}))
    rows = await db.conversations.find(
        {"candidate_id": candidate_id, "user_id": user["id"]}, {"_id": 0}
    ).sort("created_at", -1).to_list(50)
    return rows


@api.get("/conversations/{conversation_id}/audio")
async def conversation_audio(conversation_id: str, user: dict = Depends(current_user)):
    """Stream the call audio from ElevenLabs. Auth required."""
    conv = await db.conversations.find_one(
        {"elevenlabs_conversation_id": conversation_id, "user_id": user["id"]}, {"_id": 0}
    )
    if not conv:
        # Also accept our internal conversation id
        conv = await db.conversations.find_one({"id": conversation_id, "user_id": user["id"]}, {"_id": 0})
    if not conv or not conv.get("elevenlabs_conversation_id"):
        raise HTTPException(404, "Conversation not found")
    if not is_super_admin(user):
        await assert_candidate_access(user, await db.candidates.find_one(
            {"id": conv.get("candidate_id"), "user_id": user["id"]}, {"_id": 0, "pipeline_id": 1, "added_by_user_id": 1}))
    res = fetch_elevenlabs_conversation_audio(conv["elevenlabs_conversation_id"])
    if res.get("error") or not res.get("audio"):
        raise HTTPException(502, f"ElevenLabs: {res.get('error', 'no audio')}")
    return Response(content=res["audio"], media_type=res.get("content_type", "audio/mpeg"))


# ===== SMS =====
@api.post("/candidates/{candidate_id}/sms", dependencies=[Depends(candidate_write_access)])
async def sms_candidate(candidate_id: str, body: str = Body(..., embed=True), user: dict = Depends(current_user)):
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")
    if not cand.get("phone"):
        raise HTTPException(400, "Candidate has no phone")
    settings = await resolve_settings(user["id"], cand.get("pipeline_id"))
    from sms_service import send_candidate_sms
    return await send_candidate_sms(db, settings, cand, body, template_key="manual")


# ===== Stage Emails =====
@api.post("/candidates/{candidate_id}/email", dependencies=[Depends(candidate_write_access)])
async def email_candidate(
    candidate_id: str,
    template_key: str = Body(..., embed=True),
    user: dict = Depends(current_user),
):
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")
    return await _send_stage_email(user["id"], cand, template_key)


@api.post("/templates/preview")
async def preview_template(
    payload: Dict[str, Any] = Body(...),
    user: dict = Depends(current_user),
):
    """Render a template through the styled HTML shell so the user can see how it looks before sending.
    Body: {template_key?, subject?, body?, candidate_id?, pipeline_id?} — if template_key given we look up the saved template."""
    from email_service import build_email_html
    from link_redaction import REDACTED_TOKEN
    cand = None
    if payload.get("candidate_id"):
        if not isinstance(payload["candidate_id"], str):
            raise HTTPException(400, "candidate_id must be a string")
        cand = await db.candidates.find_one({"id": payload["candidate_id"], "user_id": user["id"]}, {"_id": 0})
        # Rendering a real candidate shows their contact details and their
        # portal / reschedule links: the same scope as reading the candidate.
        await assert_candidate_access(user, cand)
        if is_demo(user):
            # A trial account may see the message, but not the candidate's real
            # portal token. With it, anyone can rebook that person.
            cand = {**cand, "public_token": REDACTED_TOKEN}
    elif payload.get("pipeline_id"):
        if not isinstance(payload["pipeline_id"], str):
            raise HTTPException(400, "pipeline_id must be a string")
        await assert_pipeline_access(user, payload["pipeline_id"])
    # Resolve settings against the candidate's pipeline (or an explicitly passed pipeline_id)
    # so the preview reflects per-pipeline overrides.
    pipeline_for_preview = (cand or {}).get("pipeline_id") or payload.get("pipeline_id")
    settings = await resolve_settings(user["id"], pipeline_for_preview)
    if not settings:
        settings = await get_or_create_settings(user["id"])
    if not cand:
        # synthetic candidate so the preview always has something to show
        cand = {
            "first_name": "Sam",
            "last_name": "Sample",
            "email": "sam@example.com",
            "phone": "+15551234567",
            "appointment_at": "2026-05-15T14:30:00+00:00",
            "appointment_link": "https://zoom.us/j/123456789",
        }
    job = None
    if cand.get("job_id"):
        job = await db.jobs.find_one({"id": cand["job_id"], "user_id": user["id"]}, {"_id": 0})

    tpl_key = payload.get("template_key", "")
    if tpl_key:
        tpl = get_template_for_key(settings, tpl_key)
        subject_in = payload.get("subject") or tpl.get("subject", "")
        body_in = payload.get("body") or tpl.get("body", "")
    else:
        subject_in = payload.get("subject", "")
        body_in = payload.get("body", "")

    vars_map = build_template_vars(cand, settings, job)
    subject = render_template(subject_in, vars_map)
    body = render_template(body_in, vars_map)
    html = build_email_html(body, cand, settings, template_key=tpl_key)
    return {"subject": subject, "body": body, "html": html}


@api.get("/candidates/{candidate_id}/communications")
async def candidate_communications(candidate_id: str, user: dict = Depends(current_user)):
    await assert_candidate_access(user, await db.candidates.find_one(
        {"id": candidate_id, "user_id": user["id"]}, {"_id": 0, "pipeline_id": 1, "added_by_user_id": 1}))
    rows = await db.communications.find(
        {"candidate_id": candidate_id, "user_id": user["id"]}, {"_id": 0}
    ).sort("created_at", -1).to_list(200)
    return rows


# ===== Settings =====
@api.get("/settings")
async def get_settings(pipeline_id: Optional[str] = Query(None), user: dict = Depends(current_user)):
    if pipeline_id:
        await assert_pipeline_access(user, pipeline_id)
    # Read-only: don't auto-create the per-pipeline override doc on GET. The
    # override is created by the first PUT — that way 'Reset to global default'
    # actually sticks (DELETE override → next GET returns global with
    # pipeline_id=null, banner reverts).
    s = await resolve_settings(user["id"], pipeline_id)
    if not s:
        # First-ever request: lazily create the global doc only.
        s = await get_or_create_settings(user["id"])
    # Credentials never leave in this response, whatever the role: the intake
    # token has its own owner-only endpoint and the CG1 secret is write-only.
    s = strip_settings_secrets(s)
    if is_analyst(user):
        return settings_view_for_analyst(s)
    # A demo account is a super-admin so it can SEE the whole product, but the
    # unredacted doc carries live infrastructure config — including the CG1
    # webhook URL, which would let a trial post hires straight into
    # CG1 with none of this app's guards in the way. Redact it like a recruiter:
    # everything recruiter-useful stays visible, the infra keys go.
    if not is_super_admin(user) or is_demo(user):
        s = redact_settings_for_recruiter(s)
    return s


@api.delete("/settings/override")
async def delete_settings_override(
    pipeline_id: str = Query(...),
    user: dict = Depends(current_user),
):
    """Drop a pipeline's settings override doc — the pipeline reverts to global defaults."""
    require_super_admin(user)
    res = await db.settings.delete_one({"user_id": user["id"], "pipeline_id": pipeline_id})
    return {"ok": True, "deleted": res.deleted_count}


@api.get("/settings/override-status")
async def settings_override_status(user: dict = Depends(current_user)):
    """Returns the list of pipeline_ids that currently have a settings override
    doc, so the FE can render an 'Override active' badge in the pipeline switcher."""
    rows = await db.settings.find(
        {"user_id": user["id"], "pipeline_id": {"$ne": None}},
        {"_id": 0, "pipeline_id": 1},
    ).to_list(200)
    return {"overrides": [r["pipeline_id"] for r in rows if r.get("pipeline_id")]}


@api.put("/settings/recruiter-profile")
async def update_recruiter_profile(
    payload: RecruiterProfile,
    pipeline_id: Optional[str] = Query(None),
    user: dict = Depends(current_user),
):
    # Recruiters can edit the recruiter_profile for THEIR pipeline only.
    # Tenant-wide global save (pipeline_id=None) stays super-admin only —
    # changing the global company branding affects every office's
    # outbound emails, so a single office's recruiter shouldn't be able to do it.
    pipeline_id = await settings_write_scope(user, pipeline_id)
    await get_or_create_settings(user["id"], pipeline_id)
    await db.settings.update_one(
        {"user_id": user["id"], "pipeline_id": pipeline_id},
        {"$set": {"recruiter_profile": payload.model_dump(), "updated_at": now_iso()}},
    )
    return {"ok": True, "recruiter_profile": payload.model_dump()}


@api.put("/settings/screen-call-agent")
async def update_screen_call_agent(
    payload: ScreenCallAgentSettings,
    pipeline_id: Optional[str] = Query(None),
    user: dict = Depends(current_user),
):
    # Recruiters can edit screen-call-agent settings for THEIR pipeline only.
    # Tenant-wide global (pipeline_id=None) is still super-admin only.
    pipeline_id = await settings_write_scope(user, pipeline_id)
    await get_or_create_settings(user["id"], pipeline_id)
    await db.settings.update_one(
        {"user_id": user["id"], "pipeline_id": pipeline_id},
        {"$set": {"screen_call_agent": payload.model_dump(), "updated_at": now_iso()}},
    )
    return {"ok": True}


@api.put("/settings/applicant-comms")
async def update_applicant_comms(
    payload: ApplicantCommsSettings,
    pipeline_id: Optional[str] = Query(None),
    user: dict = Depends(current_user),
):
    # These templates are the branded email and text every candidate gets.
    # The global set is the owner's; an office's set is for its writers.
    pipeline_id = await settings_write_scope(user, pipeline_id)
    await get_or_create_settings(user["id"], pipeline_id)
    serial = {k: v.model_dump() if hasattr(v, "model_dump") else v for k, v in (payload.templates or {}).items()}
    await db.settings.update_one(
        {"user_id": user["id"], "pipeline_id": pipeline_id},
        {"$set": {"applicant_comms.templates": serial, "updated_at": now_iso()}},
    )
    return {"ok": True}


@api.put("/settings/region-language")
async def update_region_language(
    payload: RegionLanguageSettings,
    pipeline_id: Optional[str] = Query(None),
    user: dict = Depends(current_user),
):
    pipeline_id = await settings_write_scope(user, pipeline_id)
    await get_or_create_settings(user["id"], pipeline_id)
    await db.settings.update_one(
        {"user_id": user["id"], "pipeline_id": pipeline_id},
        {"$set": {"region_language": payload.model_dump(), "updated_at": now_iso()}},
    )
    return {"ok": True}


@api.put("/settings/appointments")
async def update_appointments(
    payload: Dict[str, Any] = Body(...),
    pipeline_id: Optional[str] = Query(None),
    user: dict = Depends(current_user),
):
    # Partial merge, not a full-model replace. The old `payload: AppointmentSettings`
    # form meant any client that omitted a field silently reset it to the model
    # default — every Booking & Slots save was knocking booking_days_offered
    # (the candidate-facing lead-time window) from the tuned value back to 7.
    # Now only the fields the client actually sent are written; unknown keys are
    # dropped and known keys are validated against the model.
    pipeline_id = await settings_write_scope(user, pipeline_id)
    known = set(AppointmentSettings.model_fields.keys())
    sent = {k: v for k, v in (payload or {}).items() if k in known}
    if not sent:
        raise HTTPException(400, "Nothing to update")
    current = (await get_or_create_settings(user["id"], pipeline_id)).get("appointments") or {}
    merged = AppointmentSettings(**{**current, **sent})
    validated = merged.model_dump()
    await db.settings.update_one(
        {"user_id": user["id"], "pipeline_id": pipeline_id},
        {"$set": {**{f"appointments.{k}": validated[k] for k in sent}, "updated_at": now_iso()}},
    )
    return {"ok": True}


@api.put("/settings/booking-preferences")
async def update_booking_preferences(
    payload: BookingPreferences,
    pipeline_id: Optional[str] = Query(None),
    user: dict = Depends(current_user),
):
    pipeline_id = await settings_write_scope(user, pipeline_id)
    await get_or_create_settings(user["id"], pipeline_id)
    await db.settings.update_one(
        {"user_id": user["id"], "pipeline_id": pipeline_id},
        {"$set": {"booking_preferences": payload.model_dump(), "updated_at": now_iso()}},
    )
    return {"ok": True}


@api.get("/settings/preview-slots")
async def preview_slots(
    pipeline_id: Optional[str] = Query(None),
    user: dict = Depends(current_user),
):
    """Returns the exact slot list the ElevenLabs agent sees when it calls
    get_available_slots, annotated with which are primary vs fallback."""
    if not pipeline_id:
        raise HTTPException(400, "pipeline_id required")
    await assert_pipeline_access(user, pipeline_id)
    pipe = await db.pipelines.find_one({"id": pipeline_id, "user_id": user["id"]}, {"_id": 0})
    if not pipe:
        raise HTTPException(404, "Pipeline not found")

    from availability_service import compute_available_slots
    settings = await resolve_settings(user["id"], pipeline_id)
    region = settings.get("region_language") or {}
    tz_name = region.get("timezone") or default_tz_name()
    appt_settings = settings.get("appointments") or {}
    booking_prefs = settings.get("booking_preferences") or {}

    days = 60  # always look 60 days ahead; slot count (slots_primary/fallback) controls what's offered
    default_capacity = int(appt_settings.get("applicant_limit") or 50)
    slots_primary = int(booking_prefs.get("slots_primary") or 2)
    slots_fallback = int(booking_prefs.get("slots_fallback") or 1)

    booked = await db.candidates.find(
        {"pipeline_id": pipeline_id, "appointment_at": {"$ne": None}},
        {"_id": 0, "appointment_at": 1},
    ).to_list(2000)
    booked_isos = [c.get("appointment_at") for c in booked if c.get("appointment_at")]

    all_slots = compute_available_slots(
        pipe, booked_isos, days_ahead=days,
        tz_name=tz_name, default_capacity=default_capacity,
    )

    return {
        "pipeline": pipe.get("name"),
        "timezone": tz_name,
        "days_ahead": days,
        "slots_primary": slots_primary,
        "slots_fallback": slots_fallback,
        "total_available": len(all_slots),
        "primary_slots": all_slots[:slots_primary],
        "fallback_slots": all_slots[slots_primary:slots_primary + slots_fallback],
        "all_slots": all_slots,
    }


@api.put("/settings/custom-form")
async def update_custom_form(
    payload: CustomFormSettings,
    pipeline_id: Optional[str] = Query(None),
    user: dict = Depends(current_user),
):
    pipeline_id = await settings_write_scope(user, pipeline_id)
    await get_or_create_settings(user["id"], pipeline_id)
    await db.settings.update_one(
        {"user_id": user["id"], "pipeline_id": pipeline_id},
        {"$set": {"custom_form": payload.model_dump(), "updated_at": now_iso()}},
    )
    return {"ok": True}


@api.put("/settings/no-show-revival")
async def update_no_show_revival(
    payload: dict = Body(...),
    pipeline_id: Optional[str] = Query(None),
    user: dict = Depends(current_user),
):
    """No-show revival config — AI courtesy calls to archived no-shows.
    Recruiters can edit their own pipeline's config; global is super-admin only."""
    pipeline_id = await settings_write_scope(user, pipeline_id)
    from models import NoShowRevivalSettings
    obj = NoShowRevivalSettings(**payload)
    await get_or_create_settings(user["id"], pipeline_id)
    await db.settings.update_one(
        {"user_id": user["id"], "pipeline_id": pipeline_id},
        {"$set": {"no_show_revival": obj.model_dump(), "updated_at": now_iso()}},
    )
    # Script edits (opener / extra instructions) only take effect once the
    # ElevenLabs agent is re-synced — do it here so saving is enough. Only
    # touches pipelines that already HAVE a revival agent; creation stays
    # behind the explicit "Create & sync" button. Best-effort: a sync hiccup
    # must not fail the settings save.
    resync: List[Dict[str, Any]] = []
    try:
        pipe_q: Dict[str, Any] = {"user_id": user["id"], "revival_agent_id": {"$nin": [None, ""]}}
        if pipeline_id:
            pipe_q["id"] = pipeline_id
        pipes = await db.pipelines.find(pipe_q, {"_id": 0}).to_list(100)
        if pipes:
            from routes.revival import _sync_one_pipeline
            from voice_service import upsert_elevenlabs_tools
            public_base = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
            tool_map = upsert_elevenlabs_tools(public_base)
            tool_ids = [tid for k, tid in (tool_map or {}).items() if tid and k != "error"]
            for pipe in pipes:
                resync.append(await _sync_one_pipeline(user["id"], pipe, tool_ids))
    except Exception as e:
        logger.warning(f"no-show-revival save: agent re-sync failed: {e}")
        resync.append({"status": "failed", "error": str(e)})
    return {"ok": True, "agent_resync": resync}


@api.put("/settings/auto-dialer")
async def update_auto_dialer(
    payload: dict = Body(...),
    pipeline_id: Optional[str] = Query(None),
    user: dict = Depends(current_user),
):
    # Recruiters can edit their own pipeline's auto-dialer config.
    # Tenant-wide global (pipeline_id=None) stays super-admin only.
    pipeline_id = await settings_write_scope(user, pipeline_id)
    from models import AutoDialerSettings
    obj = AutoDialerSettings(**payload)
    await get_or_create_settings(user["id"], pipeline_id)
    await db.settings.update_one(
        {"user_id": user["id"], "pipeline_id": pipeline_id},
        {"$set": {"auto_dialer": obj.model_dump(), "updated_at": now_iso()}},
    )
    return {"ok": True, "auto_dialer": obj.model_dump()}


# ── Starter-email templates (per pipeline) ────────────────────────────────────
# Office defaults for the starter email live in the company profile
# (backend/company_profile.json → starter_template_defaults, plus each office's
# own "starter_template"). Anything saved in Settings → Starter Email wins.
_TEMPLATE_KEYS = ["monday_start", "monday_end", "tuesday_start", "tuesday_end",
                  "regular_schedule", "dress_code", "id_text", "research_text",
                  "food_text", "parking_text", "custom_notes", "office_address",
                  "confirmation_sms_body", "confirmation_sms_lead_hours",
                  "confirmation_sms_instructions"]


def _template_defaults(office_key: str) -> dict:
    from company_profile import starter_template_defaults
    return starter_template_defaults(office_key)


@api.get("/company-profile/offices")
async def company_profile_offices(user: dict = Depends(current_user)):
    """Offices from backend/company_profile.json, for the Settings office pickers."""
    return {"offices": company_profile.public_offices(), "default_office": company_profile.default_office_key()}


@api.get("/settings/starter-template")
async def get_starter_template(pipeline_id: Optional[str] = Query(None), user: dict = Depends(current_user)):
    """Return the starter email template for a pipeline (or global fallback)."""
    require_super_admin(user)
    pipeline_id = pipeline_id or None
    if pipeline_id:
        await assert_pipeline_access(user, pipeline_id)
    stored = await db.settings.find_one(
        {"user_id": user["id"], "pipeline_id": pipeline_id},
        {"_id": 0, "starter_template": 1, "starter_date_overrides": 1},
    ) or {}
    tpl = stored.get("starter_template") or {}
    # Dated exceptions ride along so the Training modal can show the hours that
    # will actually be emailed for the date being picked. Without them the modal
    # pre-fills the normal hours, the recruiter sees 11:00 AM for a day the
    # email will say 9:30 AM, and "correcting" it achieves nothing — the send
    # path applies the exception above the modal's values either way.
    date_overrides = stored.get("starter_date_overrides") or {}
    if not date_overrides and pipeline_id:
        _global = await db.settings.find_one(
            {"user_id": user["id"], "pipeline_id": None}, {"_id": 0, "starter_date_overrides": 1}
        ) or {}
        date_overrides = _global.get("starter_date_overrides") or {}
    # Determine default set from pipeline's cg1_office_key
    office_key = ""
    if pipeline_id:
        pipe = await db.pipelines.find_one({"id": pipeline_id, "user_id": user["id"]}, {"_id": 0})
        if pipe:
            office_key = company_profile.office_key_for_pipeline(pipe)
    defaults = _template_defaults(office_key)
    return {**defaults, **tpl, "office_key": office_key, "date_overrides": date_overrides}


@api.put("/settings/starter-template")
async def save_starter_template(
    payload: dict = Body(...),
    pipeline_id: Optional[str] = Query(None),
    user: dict = Depends(current_user),
):
    """Save the starter email template for a pipeline."""
    require_super_admin(user)
    pipeline_id = await settings_write_scope(user, pipeline_id)
    tpl = {k: str(payload.get(k) or "") for k in _TEMPLATE_KEYS}
    await get_or_create_settings(user["id"], pipeline_id)
    await db.settings.update_one(
        {"user_id": user["id"], "pipeline_id": pipeline_id},
        {"$set": {"starter_template": tpl, "updated_at": now_iso()}},
    )
    return {"ok": True, "starter_template": tpl}


@api.post("/settings/starter-template/reset")
async def reset_starter_template(
    pipeline_id: Optional[str] = Query(None),
    user: dict = Depends(current_user),
):
    """Reset the starter email template for a pipeline to defaults."""
    require_super_admin(user)
    pipeline_id = await settings_write_scope(user, pipeline_id)
    pipe = await db.pipelines.find_one({"id": pipeline_id, "user_id": user["id"]}, {"_id": 0}) if pipeline_id else {}
    office_key = company_profile.office_key_for_pipeline(pipe)
    defaults = _template_defaults(office_key)
    await db.settings.update_one(
        {"user_id": user["id"], "pipeline_id": pipeline_id},
        {"$set": {"starter_template": defaults, "updated_at": now_iso()}},
        upsert=True,
    )
    return {"ok": True, "starter_template": defaults}


@api.put("/settings/ai-stage-prompts")
async def update_ai_stage_prompts(
    payload: dict = Body(...),
    pipeline_id: Optional[str] = Query(None),
    user: dict = Depends(current_user),
):
    require_super_admin(user)
    pipeline_id = await settings_write_scope(user, pipeline_id)
    await get_or_create_settings(user["id"], pipeline_id)
    prompts = payload.get("ai_stage_prompts", payload)
    await db.settings.update_one(
        {"user_id": user["id"], "pipeline_id": pipeline_id},
        {"$set": {"ai_stage_prompts": prompts, "updated_at": now_iso()}},
    )
    return {"ok": True, "ai_stage_prompts": prompts}


@api.put("/settings/integrations")
async def update_integrations(payload: dict = Body(...), user: dict = Depends(current_user)):
    require_super_admin(user)
    from models import IntegrationsSettings
    obj = IntegrationsSettings(**payload)
    # Integrations are tenant-wide infra — never per-pipeline.
    current = await get_or_create_settings(user["id"])
    # The secret is write-only: GET /settings never returns it, so the screen
    # sends it back blank. Blank means "keep the saved one".
    if not obj.cg1_webhook_secret:
        obj.cg1_webhook_secret = ((current.get("integrations") or {}).get("cg1_webhook_secret") or "")
    await db.settings.update_one(
        {"user_id": user["id"], "pipeline_id": None},
        {"$set": {"integrations": obj.model_dump(), "updated_at": now_iso()}},
    )
    return {"ok": True, "integrations": strip_settings_secrets(obj.model_dump())}


@api.post("/settings/integrations/cg1/test")
async def test_cg1_webhook(payload: dict = Body(...), user: dict = Depends(current_user)):
    """Send a synthetic 'hire' payload to the CG1 webhook for connectivity testing.
    Uses the URL/secret in the request body so admins can validate before saving."""
    require_super_admin(user)
    import httpx
    url = (payload.get("cg1_webhook_url") or "").strip()
    secret = payload.get("cg1_webhook_secret") or ""
    if not secret:
        # Write-only secret: the screen can't send it back, so test with the
        # saved one unless a new one was typed.
        _g = await db.settings.find_one({"user_id": user["id"], "pipeline_id": None}, {"_id": 0, "integrations": 1}) or {}
        secret = (_g.get("integrations") or {}).get("cg1_webhook_secret") or ""
    if not url:
        raise HTTPException(400, "cg1_webhook_url required")
    test_payload = {
        "candidate_id": "test-candidate-id",
        "first_name": "Test",
        "last_name": "Candidate",
        "email": "test@example.com",
        "phone": "+10000000000",
        "appointment_at": now_iso(),
        "hired_at": now_iso(),
        "company": (await db.settings.find_one({"user_id": user["id"], "pipeline_id": None}, {"_id": 0}) or {}).get("recruiter_profile", {}).get("company_name", ""),
        "_test": True,
    }
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Webhook-Secret"] = secret
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=test_payload, headers=headers)
        return {
            "status": "sent" if r.status_code < 400 else "failed",
            "status_code": r.status_code,
            "body": (r.text or "")[:500],
        }
    except Exception as e:
        return {"status": "failed", "error": str(e)}


@api.post("/pipelines/{pipeline_id}/batch-dial")
async def batch_dial(pipeline_id: str, user: dict = Depends(current_user)):
    # Places billable calls: no read-only accounts, and only your own offices.
    require_mover(user)
    await assert_pipeline_access(user, pipeline_id)
    pipe = await db.pipelines.find_one({"id": pipeline_id, "user_id": user["id"]}, {"_id": 0})
    if not pipe:
        raise HTTPException(404, "Pipeline not found")
    return await batch_dial_pipeline(user["id"], pipeline_id)


@api.get("/pipelines/{pipeline_id}/call-queue")
async def get_call_queue(pipeline_id: str, user: dict = Depends(current_user)):
    """Return the live + queued + paused call list for this pipeline. Used by
    the Call Queue page in the Dashboard top bar.

    - live: candidates whose AI screening call is in progress right now.
            Defined as: candidate.screening_status='in_progress' AND there
            is at least one Conversation doc in an active status created
            inside the ghost cutoff (see dialer.live_count). The conversation
            join exists because candidates can get stuck at
            screening_status='in_progress' if EL's voicemail/end-call
            webhook never re-flows through us — we don't want to surface
            those as fake live calls. A reconciliation sweep
            (`POST /pipelines/{id}/call-queue/reconcile`) cleans up the
            stale candidate docs.
    - queued: everyone the dialer still intends to call — next_call_at is set
              and the screening is neither finished, already dialling, nor
              paused. Keyed off next_call_at rather than
              screening_status='queued' because every retry path (no_answer,
              incomplete_info) arms a real dial without ever moving the
              candidate back to 'queued', so the whole retry backlog used to
              be invisible here. Sorted by next_call_at asc.
    - paused: candidates that the recruiter explicitly paused
              (screening_status='paused'). Re-queue surfaces the Resume btn.

    `counts` are true totals; the row arrays are capped for rendering and
    `truncated` says which of them were cut short.
    """
    await assert_pipeline_access(user, pipeline_id)
    # archived_at also matches docs that never had the field, so soft-rejected
    # candidates can't sit in Paused forever behind a Re-queue button the dialer
    # is going to refuse.
    base = {"user_id": user["id"], "pipeline_id": pipeline_id, "archived_at": None}
    fields = {
        "_id": 0, "id": 1, "first_name": 1, "last_name": 1, "phone": 1, "email": 1,
        "stage": 1, "screening_status": 1, "next_call_at": 1, "call_attempts": 1,
        "verdict": 1, "appointment_at": 1, "smart_score": 1,
        "auto_dial": 1, "auto_promote_at": 1,
    }
    from dialer.live_count import filter_live_call_ids, count_tenant_live, count_pipeline_live
    # ---- LIVE — only candidates with a conversation that is itself active.
    # Ids for EVERY in_progress row, not a page: counts.live and effective_cap
    # are derived from this set, and a truncated sample under-reports both.
    in_progress_ids = [c["id"] for c in await db.candidates.find(
        {**base, "screening_status": "in_progress"}, {"_id": 0, "id": 1},
    ).to_list(2000)]
    # Ghost-aware live set — shared with the concurrency cap so the "N/X dialling"
    # badge and the dialer's own cap can never disagree. See dialer.live_count.
    live_ids = await filter_live_call_ids(db, in_progress_ids)
    live = []
    if live_ids:
        # updated_at is stamped at dial time by place_call._mark_call_initiated;
        # the previous sort key (last_call_at) is written by nothing in the repo,
        # so every doc tied on a missing key and the retained page was arbitrary.
        live = await db.candidates.find(
            {**base, "id": {"$in": list(live_ids)}}, fields,
        ).sort("updated_at", -1).limit(50).to_list(50)
    queued_q = {
        **base,
        "next_call_at": {"$ne": None},
        "screening_status": {"$nin": ["approved", "rejected", "in_progress", "paused"]},
    }
    paused_q = {**base, "screening_status": "paused"}
    queued = await db.candidates.find(queued_q, fields).sort("next_call_at", 1).limit(200).to_list(200)
    paused = await db.candidates.find(paused_q, fields).sort("updated_at", -1).limit(100).to_list(100)
    # Counted over the row queries' own filters — a tile that disagrees with the
    # list under it is worse than no tile.
    queued_total = await db.candidates.count_documents(queued_q)
    paused_total = await db.candidates.count_documents(paused_q)
    # A stuck in_progress row with NO conversation at all was never dialed — it
    # is a text screening in flight (reopen_screening sets in_progress), which
    # reconcile deliberately leaves alone. Counting those here made the badge
    # advertise ghosts the button will never clear.
    ghost_ids = set(in_progress_ids) - live_ids
    stale_in_progress = 0
    if ghost_ids:
        # distinct, not a capped find: ghosts are exactly the candidates the retry
        # ladder dialled several times over, so several rows each is normal and a
        # row-count ceiling drops whole candidates from the badge in Mongo's
        # natural order — telling the recruiter to reset fewer than reconcile will.
        ever_called = await db.conversations.distinct(
            "candidate_id", {"candidate_id": {"$in": list(ghost_ids)}},
        )
        stale_in_progress = len(ghost_ids & set(ever_called))
    # Concurrency caps for the FE progress indicator. Effective cap is the lower
    # of (pipeline cap, remaining tenant budget) so pipelines don't oversubscribe
    # the ElevenLabs plan ceiling.
    settings_resolved = await resolve_settings(user["id"], pipeline_id)
    ad = (settings_resolved.get("auto_dialer") or {}) if settings_resolved else {}
    pipe_cap = int(ad.get("max_concurrent_calls") or 5)
    tenant_cap = int((settings_resolved or {}).get("tenant_max_concurrent_calls") or 5)
    # Conversation-first and ghost-aware, so revival calls — which burn a real EL
    # line but never set screening_status — count against the tenant ceiling here
    # exactly as they do in the dialer's own cap. Inbound is NOT counted: its
    # conversation row is only materialised post-call, so an inbound screening in
    # flight is invisible to both this meter and the dialer's gate. The ceiling is
    # outbound-only; a busy inbound line can still oversubscribe the EL plan.
    tenant_live = await count_tenant_live(db, user["id"])
    # Both cap terms have to be measured the way the dialer's own gate measures
    # them, or the meter and `_enforce_concurrency_cap` disagree about who is
    # holding a line. `live_ids` is screening-only and archived-excluded, so it
    # is NOT the right subtrahend for pipe_cap: revival dials this pipeline's
    # archived no-shows and spends pipe_cap without ever setting
    # screening_status. count_pipeline_live is the same number is_blocked sees.
    pipeline_live = await count_pipeline_live(db, user["id"], pipeline_id)
    # The FE compares effective_cap against counts.live (screening-only), so
    # express the ceiling in those units: what this pipeline is visibly running,
    # plus the headroom the dialer would actually grant. Everything else holding
    # a line — other offices' calls and this pipeline's own revival dials — is
    # already inside pipeline_live/tenant_live and comes off the caps there.
    headroom = min(pipe_cap - pipeline_live, tenant_cap - tenant_live)
    return {
        "live": live,
        "queued": queued,
        "paused": paused,
        "counts": {"live": len(live_ids), "queued": queued_total, "paused": paused_total},
        # Rows are capped for rendering — tell the FE when it's showing a page.
        "truncated": {
            "live": len(live_ids) > len(live),
            "queued": queued_total > len(queued),
            "paused": paused_total > len(paused),
        },
        "concurrency": {
            "pipeline_cap": pipe_cap,
            "tenant_cap": tenant_cap,
            "tenant_live": tenant_live,
            # Effective ceiling for THIS pipeline at this moment — useful for the
            # "N / X agents are dialling now" badge. The outer min only bites
            # when a pipeline is already over its cap (manual dials bypass the
            # gate), where the badge is on either way.
            "effective_cap": min(pipe_cap, tenant_cap, len(live_ids) + max(0, headroom)),
        },
        # Surface stale-candidate count so the FE can offer a one-click cleanup button.
        "stale_in_progress": stale_in_progress,
    }


@api.post("/pipelines/{pipeline_id}/call-queue/reconcile")
async def reconcile_call_queue(pipeline_id: str, user: dict = Depends(current_user)):
    """Sweep candidates that are stuck at `screening_status='in_progress'`
    but have NO live conversation, and reset them to a sensible terminal
    state based on what we DO know:
      - Latest conversation completed → 'incomplete_info' (verdict missing)
      - Latest conversation no_answer/failed → 'no_answer'
      - No conversation at all → left alone (never a phone ghost, see below)

    Why this exists: managed-mode calls go EL ↔ Twilio direct, so our
    Twilio StatusCallback hook (`/api/twiml/status/...`) doesn't fire.
    Without it, the candidate doc is left at in_progress forever and the
    Call Queue page shows ghosts. Recruiters can hit this endpoint to
    refresh the view; an APScheduler nightly job calls it automatically."""
    require_mover(user)
    await assert_pipeline_access(user, pipeline_id)
    base = {"user_id": user["id"], "pipeline_id": pipeline_id}
    stuck = await db.candidates.find(
        {**base, "screening_status": "in_progress"},
        {"_id": 0, "id": 1, "verdict": 1},
    ).to_list(1000)
    fixed = {"to_no_answer": 0, "to_incomplete": 0}
    if not stuck:
        return {"ok": True, "reset": 0, **fixed}
    cand_ids = [c["id"] for c in stuck]
    # The same definition of "live" the page uses, but at 22 minutes instead of
    # its 20 — this sweep must stay strictly more conservative than the display,
    # or it resets a row the recruiter is still watching dial. Re-implementing
    # the status list here with no age bound made the button a guaranteed no-op
    # for exactly the rows the ghost badge flags: a conversation left in an
    # active status forever.
    from dialer.live_count import filter_live_call_ids
    active_cand_ids = await filter_live_call_ids(db, cand_ids, ghost_cutoff_minutes=22)
    for cand in stuck:
        cid = cand["id"]
        if cid in active_cand_ids:
            continue  # genuinely live, skip
        # Look at the most recent conversation to pick a sensible terminal state.
        latest = await db.conversations.find_one(
            {"candidate_id": cid},
            {"_id": 0, "status": 1, "duration_seconds": 1},
            sort=[("created_at", -1)],
        )
        if not latest:
            # No call was ever placed, so this isn't a dropped-webhook phone
            # ghost — it is a text screening in flight (reopen_screening sets
            # in_progress). Resetting it to "pending" yanked the candidate out
            # of the conversation mid-flow. Mirrors dialer.scheduler
            # ._periodic_reconcile, which already skips these.
            continue
        if latest.get("status") in ("no_answer", "failed"):
            new_status = "no_answer"
            fixed["to_no_answer"] += 1
        else:
            new_status = "incomplete_info"
            fixed["to_incomplete"] += 1
        await db.candidates.update_one(
            {"id": cid, "user_id": user["id"]},
            {"$set": {"screening_status": new_status, "updated_at": now_iso()}},
        )
    total = sum(fixed.values())
    logger.info(f"call-queue reconcile pipeline={pipeline_id} reset {total} stale candidates: {fixed}")
    await broadcast(user["id"])
    return {"ok": True, "reset": total, **fixed}


@api.get("/events/stream")
async def events_stream(user: dict = Depends(current_user)):
    """Server-Sent Events stream. The browser connects once; the server pushes
    a 'refresh' message whenever a candidate status changes for this user so
    the UI updates instantly without waiting for the next poll cycle."""
    user_id = user["id"]
    q = subscribe(user_id)

    async def gen():
        try:
            yield "data: connected\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"data: {event}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            unsubscribe(user_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


@api.post("/candidates/{candidate_id}/queue/pause", dependencies=[Depends(candidate_write_access)])
async def pause_candidate_queue(candidate_id: str, user: dict = Depends(current_user)):
    """Pull a candidate out of the dial queue. Cancels every scheduled call +
    pre-call SMS, clears next_call_at, sets screening_status='paused'. The
    candidate stays in the pipeline; click Re-queue to put them back."""
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")
    from auto_dialer import cancel_pending_retry_calls
    cancelled = cancel_pending_retry_calls(candidate_id)
    await db.candidates.update_one(
        {"id": candidate_id, "user_id": user["id"]},
        {"$set": {
            "screening_status": "paused",
            "next_call_at": None,
            "updated_at": now_iso(),
        }},
    )
    fresh = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    await broadcast(user["id"])
    return {"ok": True, "candidate": fresh, "cancelled_jobs": cancelled}


@api.post("/candidates/{candidate_id}/queue/resume", dependencies=[Depends(candidate_write_access)])
async def resume_candidate_queue(
    candidate_id: str,
    delay_minutes: int = Body(0, embed=True),
    user: dict = Depends(current_user),
):
    """Re-add a paused candidate to the dial queue. Schedules a call respecting
    the auto-dialer's call window (or `delay_minutes` if you want to push it
    out further) and resets the attempt counter so this is treated as a fresh
    dial. 400s — leaving the candidate in Paused — if the dialer would refuse
    them anyway or nothing could be booked."""
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")
    # should_skip_call refuses both of these WITHOUT clearing next_call_at, so
    # re-queueing one mints a Queued row that can never be dialed and never
    # leaves the list. Refuse up front instead.
    if cand.get("archived_at"):
        raise HTTPException(400, "Candidate is archived — restore them before re-queueing")
    if not cand.get("auto_dial", True):
        raise HTTPException(400, "Auto-dial is off for this candidate")
    # Lift the pause BEFORE scheduling: schedule_call_with_window re-fetches the
    # candidate and runs should_skip_call, which refuses screening_status='paused'.
    prior_attempts = int(cand.get("call_attempts") or 0)
    await db.candidates.update_one(
        {"id": candidate_id, "user_id": user["id"]},
        {"$set": {"screening_status": "pending", "call_attempts": 0, "updated_at": now_iso()}},
    )
    # A 'pending' row with no next_call_at is in neither the Queued nor the
    # Paused bucket — invisible on the Call Queue with no Re-queue button to try
    # again — so the rollback has to cover the raise as well as the returned
    # failure. It restores call_attempts too: the lift above zeroes it, and a
    # rollback that left it at 0 would silently hand the candidate a fresh set of
    # retries they had already spent.
    try:
        res = await schedule_call_with_window(user["id"], candidate_id, base_delay_minutes=delay_minutes, force=False)
    except Exception:
        await db.candidates.update_one(
            {"id": candidate_id, "user_id": user["id"]},
            {"$set": {"screening_status": "paused", "next_call_at": None,
                      "call_attempts": prior_attempts, "updated_at": now_iso()}},
        )
        raise
    if res.get("status") != "scheduled":
        await db.candidates.update_one(
            {"id": candidate_id, "user_id": user["id"]},
            {"$set": {"screening_status": "paused", "next_call_at": None,
                      "call_attempts": prior_attempts, "updated_at": now_iso()}},
        )
        # 200 + ok:true here is what made the FE toast "Re-queued …" over a
        # candidate who never left Paused; the honest answer is the failure its
        # existing catch branch already renders.
        raise HTTPException(400, res.get("reason") or res.get("error") or "Couldn't re-queue this candidate")
    fresh = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    await broadcast(user["id"])
    return {"ok": True, "candidate": fresh, "scheduled": res}


@api.post("/candidates/{candidate_id}/schedule-call", dependencies=[Depends(candidate_write_access)])
async def schedule_call_endpoint(
    candidate_id: str,
    delay_minutes: int = Body(0, embed=True),
    force: bool = Body(False, embed=True),
    user: dict = Depends(current_user),
):
    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user["id"]}, {"_id": 0})
    if not cand:
        raise HTTPException(404, "Candidate not found")
    return await schedule_call_with_window(user["id"], candidate_id, base_delay_minutes=delay_minutes, force=force)


@api.get("/settings/comms-defaults")
async def comms_defaults():
    return {k: v.model_dump() for k, v in get_default_templates().items()}


# ===== Twilio Verified Caller ID =====
@api.post("/twilio/verify-caller-id")
async def verify_caller(phone_number: str = Body(..., embed=True), user: dict = Depends(current_user)):
    require_super_admin(user)
    return initiate_caller_id_verification(phone_number)


@api.get("/twilio/caller-ids")
async def caller_ids(user: dict = Depends(current_user)):
    """Verified BYO caller IDs. Read-only list — recruiters need this so the
    Pipeline editor's caller-ID dropdown can show their pipeline's verified
    options. Tenant-wide list (no per-pipeline filtering needed)."""
    return list_verified_caller_ids()


@api.delete("/twilio/caller-ids/{sid}")
async def remove_caller_id(sid: str, user: dict = Depends(current_user)):
    require_super_admin(user)
    from voice_service import delete_verified_caller_id
    res = delete_verified_caller_id(sid)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error") or "Failed to delete")
    # If this was the active Voice Caller ID in settings, clear it so calls
    # cleanly fall back to the TWILIO_PHONE_NUMBER default.
    # Caller-ID is tenant-wide (lives in the global screen_call_agent), so read
    # the global doc here.
    settings = await db.settings.find_one({"user_id": user["id"], "pipeline_id": None}, {"_id": 0}) or {}
    sca = settings.get("screen_call_agent") or {}
    removed_number = res.get("phone_number")
    if removed_number and (sca.get("custom_caller_id") == removed_number or sca.get("sms_sender_number") == removed_number):
        sca["custom_caller_id"] = ""
        sca["sms_sender_number"] = ""
        await db.settings.update_one(
            {"user_id": user["id"], "pipeline_id": None},
            {"$set": {"screen_call_agent": sca, "updated_at": now_iso()}},
        )
    return {"ok": True, "phone_number": removed_number}


@api.get("/twilio/phone-numbers")
async def twilio_phone_numbers(user: dict = Depends(current_user)):
    """Twilio-OWNED numbers (the only ones usable for SMS or as the FROM for AI screening calls).
    Recruiters need this to pick a caller-ID for their pipeline; the list is the same for
    everyone in the tenant so no per-pipeline filtering is needed."""
    return list_twilio_phone_numbers()


# ===== ElevenLabs Agent =====
@api.get("/elevenlabs/voices")
async def elevenlabs_voices(user: dict = Depends(current_user)):
    """Available ElevenLabs voices. Read-only — recruiters need this to preview
    + pick a voice for their own pipeline. Returning the global voice library
    doesn't expose any tenant-private data."""
    return list_elevenlabs_voices()


@api.get("/elevenlabs/llm-models")
async def elevenlabs_llm_models(user: dict = Depends(current_user)):
    """Live LLM catalog from ElevenLabs Convai (`GET /v1/convai/llm/list`).

    Filters out deprecated entries and overlays a curated tagline / best_for
    on top so the UI cards stay informative. Falls back to a small static set
    if the live call fails.

    IMPORTANT: only IDs returned by ElevenLabs are valid for sync — using a
    bespoke list (gemini-3-flash, claude-sonnet-4-5-20250929, etc.) gets
    rejected with a 400 at PATCH time.
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    catalog: List[Dict[str, Any]] = []
    try:
        if api_key:
            r = requests.get(
                "https://api.elevenlabs.io/v1/convai/llm/list",
                headers={"xi-api-key": api_key},
                timeout=15,
            )
            if r.status_code < 400:
                catalog = (r.json() or {}).get("llms") or []
    except Exception:
        catalog = []

    # Curated meta — only entries the recruiter actually cares about. Ordered
    # so the recommended default is first.
    meta = {
        "gemini-3.6-flash": {
            "label": "Gemini 3.6 Flash",
            "provider": "Google",
            "tagline": "Fastest replies on a call — stable release.",
            "best_for": "Default for screening. Oct 2026 test: replies start in ~0.6s (runs at minimal reasoning).",
            "latency_ms": 600, "cost_per_1m_in": None, "cost_per_1m_out": None,
            "tier": "balanced", "default": True, "order": 1,
        },
        "gemini-3-flash-preview": {
            "label": "Gemini 3 Flash (preview)",
            "provider": "Google",
            "tagline": "Previous default — cheaper, a little slower.",
            "best_for": "Fallback if 3.6 Flash misbehaves. Preview models can be retired at short notice.",
            "latency_ms": 950, "cost_per_1m_in": 0.20, "cost_per_1m_out": 1.50,
            "tier": "balanced", "order": 1.5,
        },
        "gemini-3.1-pro-preview": {
            "label": "Gemini 3.1 Pro",
            "provider": "Google",
            "tagline": "Top-tier reasoning, mid-tier price.",
            "best_for": "Multi-step screening with branching logic. Sharper than Flash.",
            "latency_ms": 600, "cost_per_1m_in": 1.00, "cost_per_1m_out": 8.00,
            "tier": "premium", "order": 2,
        },
        "gemini-3.1-flash-lite-preview": {
            "label": "Gemini 3.1 Flash Lite",
            "provider": "Google",
            "tagline": "Cheapest Gemini 3 — sub-200ms.",
            "best_for": "Massive volumes where every cent counts.",
            "latency_ms": 180, "cost_per_1m_in": 0.075, "cost_per_1m_out": 0.30,
            "tier": "balanced", "order": 3,
        },
        "gemini-2.5-flash": {
            "label": "Gemini 2.5 Flash",
            "provider": "Google",
            "tagline": "Reliable workhorse — well tested.",
            "best_for": "Stable fallback if Gemini 3 has issues in your region.",
            "latency_ms": 250, "cost_per_1m_in": 0.30, "cost_per_1m_out": 2.50,
            "tier": "balanced", "order": 4,
        },
        "gemini-2.5-flash-lite": {
            "label": "Gemini 2.5 Flash Lite",
            "provider": "Google",
            "tagline": "Cheapest stable Gemini.",
            "best_for": "Lots of low-stakes screening calls per day.",
            "latency_ms": 200, "cost_per_1m_in": 0.075, "cost_per_1m_out": 0.30,
            "tier": "balanced", "order": 5,
        },
        # OpenAI
        "gpt-5.2": {
            "label": "GPT-5.2",
            "provider": "OpenAI",
            "tagline": "OpenAI's latest — smartest + most natural.",
            "best_for": "When you want the best of OpenAI on the phone.",
            "latency_ms": 500, "cost_per_1m_in": 1.25, "cost_per_1m_out": 10.0,
            "tier": "premium", "order": 10,
        },
        "gpt-5.1": {
            "label": "GPT-5.1",
            "provider": "OpenAI",
            "tagline": "GPT-5 series, mature.",
            "best_for": "Stable production OpenAI with deep reasoning.",
            "latency_ms": 500, "cost_per_1m_in": 1.25, "cost_per_1m_out": 10.0,
            "tier": "premium", "order": 11,
        },
        "gpt-5": {
            "label": "GPT-5",
            "provider": "OpenAI",
            "tagline": "Reasoning flagship.",
            "best_for": "Complex multi-turn screenings.",
            "latency_ms": 600, "cost_per_1m_in": 1.25, "cost_per_1m_out": 10.0,
            "tier": "premium", "order": 12,
        },
        "gpt-5-mini": {
            "label": "GPT-5 Mini",
            "provider": "OpenAI",
            "tagline": "Smaller GPT-5, very fast.",
            "best_for": "OpenAI quality without flagship pricing.",
            "latency_ms": 350, "cost_per_1m_in": 0.25, "cost_per_1m_out": 2.00,
            "tier": "balanced", "order": 13,
        },
        "gpt-4.1": {
            "label": "GPT-4.1",
            "provider": "OpenAI",
            "tagline": "Most natural phone manner.",
            "best_for": "Senior roles. Sounds indistinguishable from a human recruiter.",
            "latency_ms": 550, "cost_per_1m_in": 2.00, "cost_per_1m_out": 8.00,
            "tier": "premium", "order": 14,
        },
        "gpt-4.1-mini": {
            "label": "GPT-4.1 Mini",
            "provider": "OpenAI",
            "tagline": "Small + fast.",
            "best_for": "Polished tone without paying full GPT-4.1.",
            "latency_ms": 350, "cost_per_1m_in": 0.40, "cost_per_1m_out": 1.60,
            "tier": "balanced", "order": 15,
        },
        "gpt-4o": {
            "label": "GPT-4o",
            "provider": "OpenAI",
            "tagline": "Last-gen flagship — still excellent.",
            "best_for": "If your prompts are tuned for 4o already.",
            "latency_ms": 600, "cost_per_1m_in": 2.50, "cost_per_1m_out": 10.0,
            "tier": "premium", "order": 16,
        },
        "gpt-4o-mini": {
            "label": "GPT-4o Mini",
            "provider": "OpenAI",
            "tagline": "Cheap, fast, friendly.",
            "best_for": "When Gemini sounds robotic for your accent.",
            "latency_ms": 400, "cost_per_1m_in": 0.15, "cost_per_1m_out": 0.60,
            "tier": "balanced", "order": 17,
        },
        # Anthropic
        "claude-opus-4-7": {
            "label": "Claude Opus 4.7",
            "provider": "Anthropic",
            "tagline": "Anthropic's flagship — most thoughtful.",
            "best_for": "High-stakes interviews where careful reasoning matters.",
            "latency_ms": 950, "cost_per_1m_in": 6.00, "cost_per_1m_out": 30.0,
            "tier": "premium", "order": 20,
        },
        "claude-sonnet-4-6": {
            "label": "Claude Sonnet 4.6",
            "provider": "Anthropic",
            "tagline": "Newest Sonnet — sharper than 4.5.",
            "best_for": "Empathetic phone manner with the latest Anthropic upgrades.",
            "latency_ms": 800, "cost_per_1m_in": 3.00, "cost_per_1m_out": 15.0,
            "tier": "premium", "order": 21,
        },
        "claude-sonnet-4-5": {
            "label": "Claude Sonnet 4.5",
            "provider": "Anthropic",
            "tagline": "Best emotional intelligence + safety.",
            "best_for": "Sensitive industries (healthcare, military hire).",
            "latency_ms": 800, "cost_per_1m_in": 3.00, "cost_per_1m_out": 15.0,
            "tier": "premium", "order": 22,
        },
        "claude-haiku-4-5": {
            "label": "Claude Haiku 4.5",
            "provider": "Anthropic",
            "tagline": "Compact Claude, fast + warm.",
            "best_for": "Friendly tone without paying for Sonnet/Opus.",
            "latency_ms": 350, "cost_per_1m_in": 1.00, "cost_per_1m_out": 5.00,
            "tier": "balanced", "order": 23,
        },
        "claude-3-7-sonnet": {
            "label": "Claude 3.7 Sonnet",
            "provider": "Anthropic",
            "tagline": "Solid mid-tier Claude.",
            "best_for": "Stable fallback if 4.5+ has hiccups.",
            "latency_ms": 800, "cost_per_1m_in": 3.00, "cost_per_1m_out": 15.0,
            "tier": "balanced", "order": 24,
        },
        # Other / advanced
        "grok-beta": {
            "label": "Grok",
            "provider": "xAI",
            "tagline": "xAI's voice agent model.",
            "best_for": "Experimental — Grok-style snappy responses.",
            "latency_ms": 500, "cost_per_1m_in": 5.00, "cost_per_1m_out": 15.0,
            "tier": "premium", "order": 30,
        },
        "custom-llm": {
            "label": "Custom LLM",
            "provider": "Bring your own",
            "tagline": "Point at your own LLM endpoint.",
            "best_for": "Self-hosted / fine-tuned models. Configure URL in advanced settings.",
            "latency_ms": 0, "cost_per_1m_in": 0.0, "cost_per_1m_out": 0.0,
            "tier": "advanced", "order": 100,
        },
    }

    # Build the curated subset from the live catalog. Anything in `meta` AND
    # in the live catalog (and not deprecated) gets surfaced.
    live_ids = {row.get("llm"): row for row in catalog if not row.get("deprecation_info")}
    out: List[Dict[str, Any]] = []
    for llm_id, m in meta.items():
        if catalog and llm_id not in live_ids:
            continue  # ElevenLabs doesn't expose this one anymore — hide it.
        out.append({"id": llm_id, **m})
    out.sort(key=lambda x: x.get("order", 999))
    # If the catalog call failed (no api key, network), still return the
    # curated list so the FE has SOMETHING (we keep ALL meta entries).
    if not catalog:
        out = [{"id": k, **v} for k, v in meta.items()]
        out.sort(key=lambda x: x.get("order", 999))
    return out


@api.post("/elevenlabs/agent/test-call")
async def elevenlabs_agent_test_call(
    payload: dict = Body(...),
    user: dict = Depends(current_user),
):
    """Place a one-off TEST CALL using a specific pipeline's ElevenLabs agent
    so admins/recruiters can audit the prompt, voice, opening line, and
    booking flow without needing a real candidate.

    The call hits the SAME `voice_service.initiate_elevenlabs_outbound_call`
    path real candidate calls use — which means it exercises:
      - The pipeline's resolved agent (`elevenlabs_agent_id_override` →
        global default) + its phone number for caller ID
      - The full dynamic_variables payload (first_name, role, company, city,
        agent_name, etc.) — caller can override `first_name` and `role` in
        the body so the test call addresses them by name
      - Aria's webhook tools (get_available_slots / book_slot) — pointed at
        the SAME pipeline's slug, so the booking flow is real

    Deliberately STATELESS — no candidate doc is created. No kanban
    pollution, no conversation row, no auto-promote scheduling. The
    transcript still lands in the ElevenLabs dashboard if the admin wants
    to review what Aria said after hanging up."""
    # Places a real, billable call to any number: read-only accounts can't.
    require_mover(user)
    pipeline_id = (payload or {}).get("pipeline_id")
    to_phone = ((payload or {}).get("to_phone") or "").strip()
    first_name = ((payload or {}).get("first_name") or "Test").strip() or "Test"
    role_override = ((payload or {}).get("role") or "").strip()

    if not pipeline_id or not to_phone:
        raise HTTPException(400, "pipeline_id and to_phone are required")

    # Recruiters can only test their OWN pipelines — admins can test any.
    pipe = await db.pipelines.find_one({"id": pipeline_id, "user_id": user["id"]}, {"_id": 0})
    if not pipe:
        raise HTTPException(404, "Pipeline not found")
    if not is_super_admin(user) and pipeline_id not in (user.get("pipeline_ids") or []):
        raise HTTPException(403, "You don't have access to this pipeline")

    # Resolve agent (handles per-pipeline override → global default fallback)
    settings = await resolve_settings(user["id"], pipeline_id)
    sca = settings.get("screen_call_agent", {}) or {}
    profile = settings.get("recruiter_profile", {}) or {}
    agent_id = pipe.get("elevenlabs_agent_id_override") or sca.get("elevenlabs_agent_id")
    phone_number_id = (
        pipe.get("elevenlabs_phone_number_id_override")
        or sca.get("elevenlabs_phone_number_id")
    )
    if not agent_id or not phone_number_id:
        raise HTTPException(503, "Agent or phone number not configured for this pipeline")
    agent_name = pipe.get("agent_name_override") or sca.get("agent_name", "Olivia")
    company = profile.get("company_name") or company_profile.company_name()
    city = (pipe.get("name", "") or "").split(",")[0].strip()

    # Build dynamic vars matching the real screening dial path EXACTLY so the
    # agent's `{{required}}` placeholders all resolve. Test calls don't have
    # a real job_id — fall back to a sensible role string the prompt can use.
    test_dyn = {
        "first_name": first_name,
        "last_name": "(test)",
        "full_name": f"{first_name} (test)",
        "role": role_override or "Direct Sales Representative",
        "company": company,
        "city": city,
        "agent_name": agent_name,
        "phone": to_phone,
        "email": "",
        "pipeline_slug": pipe.get("public_slug", ""),
        "previous_context": "",
    }

    from voice_service import initiate_elevenlabs_outbound_call
    result = initiate_elevenlabs_outbound_call(
        candidate_phone=to_phone,
        agent_id=agent_id,
        agent_phone_number_id=phone_number_id,
        dynamic_variables=test_dyn,
    )
    return {
        "status": result.get("status"),
        "call_sid": result.get("call_sid"),
        "conversation_id": result.get("conversation_id"),
        "agent_id": agent_id,
        "agent_name": agent_name,
        "to_phone": to_phone,
        "pipeline_name": pipe.get("name"),
        "error": result.get("error") if result.get("status") != "initiated" else None,
    }


@api.get("/elevenlabs/phone-numbers")
async def elevenlabs_phone_numbers(user: dict = Depends(current_super_admin)):
    return list_elevenlabs_phone_numbers()


@api.post("/elevenlabs/auto-link-pipelines")
async def elevenlabs_auto_link_pipelines(user: dict = Depends(current_super_admin)):
    """For each pipeline with a twilio_phone_number set, look up the matching ElevenLabs
    phone_number record and auto-fill the pipeline's elevenlabs_agent_id_override and
    elevenlabs_phone_number_id_override.
    If a pipeline's Twilio number is NOT yet imported into ElevenLabs, this also imports it
    automatically and assigns it to the pipeline's preferred agent (or the global agent)."""
    from voice_service import import_twilio_number_to_elevenlabs
    pipelines = await db.pipelines.find({"user_id": user["id"]}, {"_id": 0}).to_list(200)
    settings = await get_or_create_settings(user["id"])
    global_agent_id = (settings.get("screen_call_agent") or {}).get("elevenlabs_agent_id", "")
    linked = []
    skipped = []
    imported = []

    for p in pipelines:
        num = (p.get("twilio_phone_number") or "").strip()
        if not num:
            skipped.append({"pipeline": p["name"], "reason": "no twilio_phone_number"})
            continue

        # Refresh ElevenLabs phone-number list each iteration so newly-imported numbers show up
        data = list_elevenlabs_phone_numbers()
        if data.get("error"):
            raise HTTPException(502, f"ElevenLabs error: {data['error']}")
        by_number = {x["phone_number"]: x for x in (data.get("phone_numbers") or [])}

        match = by_number.get(num)
        if not match:
            # Auto-import the number into ElevenLabs
            target_agent = (p.get("elevenlabs_agent_id_override") or global_agent_id or "").strip()
            res = import_twilio_number_to_elevenlabs(num, label=p.get("name") or num, agent_id=target_agent or None)
            if res.get("error"):
                skipped.append({"pipeline": p["name"], "reason": f"import failed: {res['error']}"})
                continue
            imported.append({"pipeline": p["name"], "phone_number": num, "phone_number_id": res.get("phone_number_id")})
            match = {
                "phone_number_id": res.get("phone_number_id"),
                "assigned_agent_id": target_agent,
                "assigned_agent_name": "(just assigned)",
            }

        update = {
            "elevenlabs_phone_number_id_override": match.get("phone_number_id") or "",
            "elevenlabs_agent_id_override": match.get("assigned_agent_id") or global_agent_id or "",
        }
        await db.pipelines.update_one({"id": p["id"], "user_id": user["id"]}, {"$set": update})
        linked.append({
            "pipeline": p["name"],
            "phone_number": num,
            "phone_number_id": update["elevenlabs_phone_number_id_override"],
            "agent_id": update["elevenlabs_agent_id_override"],
            "agent_name": match.get("assigned_agent_name"),
        })
    return {"linked": linked, "skipped": skipped, "imported": imported}


@api.post("/pipelines/{pipeline_id}/set-twilio-number")
async def set_pipeline_twilio_number(
    pipeline_id: str,
    payload: Dict[str, Any] = Body(...),
    user: dict = Depends(current_super_admin),
):
    """Assign a Twilio phone number (owned or verified caller ID) to a pipeline.

    Behavior depends on calling_mode:
    - "managed" (default): the number MUST be Twilio-owned. We auto-import it into
       ElevenLabs and assign it to this pipeline's agent.
    - "twiml_bridge": any number works (owned OR verified caller ID). No ElevenLabs
       import needed — Twilio places the call directly with this caller ID, and we
       bridge audio via WebSocket to ElevenLabs."""
    from voice_service import import_twilio_number_to_elevenlabs, list_twilio_phone_numbers, list_verified_caller_ids
    phone_number = (payload.get("phone_number") or "").strip()
    if not phone_number:
        raise HTTPException(400, "phone_number required")
    pipe = await db.pipelines.find_one({"id": pipeline_id, "user_id": user["id"]}, {"_id": 0})
    if not pipe:
        raise HTTPException(404, "Pipeline not found")

    calling_mode = (payload.get("calling_mode") or pipe.get("calling_mode") or "managed").lower()
    settings = await get_or_create_settings(user["id"])
    global_agent_id = (settings.get("screen_call_agent") or {}).get("elevenlabs_agent_id", "")
    target_agent = (pipe.get("elevenlabs_agent_id_override") or global_agent_id or "").strip()

    update: Dict[str, Any] = {
        "twilio_phone_number": phone_number,
        "calling_mode": calling_mode,
        "elevenlabs_agent_id_override": target_agent,
        "updated_at": now_iso(),
    }

    if calling_mode == "twiml_bridge":
        # Verify it's at least known to Twilio (either owned or verified caller ID)
        owned = {n["phone_number"] for n in list_twilio_phone_numbers()}
        verified = {c["phone_number"] for c in list_verified_caller_ids()}
        if phone_number not in owned and phone_number not in verified:
            raise HTTPException(400, f"{phone_number} is not on this Twilio account (neither owned nor verified). Verify it first in Twilio Console → Phone Numbers → Verified Caller IDs.")
        # No ElevenLabs import needed — twiml_bridge places calls directly via Twilio
        update["elevenlabs_phone_number_id_override"] = ""
    else:
        # Managed mode requires a Twilio-owned number imported into ElevenLabs
        owned = {n["phone_number"] for n in list_twilio_phone_numbers()}
        if phone_number not in owned:
            raise HTTPException(400, f"{phone_number} is not a Twilio-owned number. Either buy/port it in Twilio Console, OR switch this pipeline's Calling Mode to 'TwiML Bridge' to use a verified caller ID.")
        res = import_twilio_number_to_elevenlabs(phone_number, label=pipe.get("name") or phone_number, agent_id=target_agent or None)
        if res.get("error"):
            raise HTTPException(502, f"ElevenLabs import failed: {res['error']}")
        update["elevenlabs_phone_number_id_override"] = res.get("phone_number_id") or ""

    await db.pipelines.update_one({"id": pipeline_id, "user_id": user["id"]}, {"$set": update})
    refreshed = await db.pipelines.find_one({"id": pipeline_id, "user_id": user["id"]}, {"_id": 0})
    return {
        "ok": True,
        "pipeline": refreshed,
        "calling_mode": calling_mode,
        "phone_number_id": update.get("elevenlabs_phone_number_id_override"),
        "agent_id": target_agent,
    }


@api.get("/elevenlabs/agent")
async def elevenlabs_agent_info(user: dict = Depends(current_super_admin)):
    settings = await get_or_create_settings(user["id"])
    sca = settings.get("screen_call_agent", {}) or {}
    agent_id = sca.get("elevenlabs_agent_id", "")
    if not agent_id:
        return {"error": "no agent_id configured", "agent_id": ""}
    info = get_elevenlabs_agent(agent_id)
    return {"agent_id": agent_id, "agent": info}


@api.post("/elevenlabs/agent/test-session")
async def elevenlabs_agent_test_session(
    payload: dict = Body(default={}),
    user: dict = Depends(current_super_admin),
):
    """A short-lived signed URL for the in-browser test widget (Settings →
    Screening Call Agent → Test the agent). Agents are synced with ElevenLabs
    authentication on, so the widget can't open a session from the agent id
    alone. Owner only, and only for an agent this account uses."""
    agent_id = str((payload or {}).get("agent_id") or "").strip()
    if not agent_id:
        raise HTTPException(400, "agent_id is required")
    settings = await get_or_create_settings(user["id"])
    allowed = {((settings.get("screen_call_agent") or {}).get("elevenlabs_agent_id") or "")}
    async for p in db.pipelines.find({"user_id": user["id"]}, {"_id": 0, "elevenlabs_agent_id_override": 1}):
        allowed.add(p.get("elevenlabs_agent_id_override") or "")
    allowed.discard("")
    if agent_id not in allowed:
        raise HTTPException(404, "Agent not found on this account")
    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not api_key:
        raise HTTPException(503, "ELEVENLABS_API_KEY is not set")
    try:
        r = requests.get(
            "https://api.elevenlabs.io/v1/convai/conversation/get-signed-url",
            headers={"xi-api-key": api_key}, params={"agent_id": agent_id}, timeout=15,
        )
        if r.status_code >= 400:
            logger.warning(f"test-session signed url HTTP {r.status_code}")
            raise HTTPException(502, "ElevenLabs refused to start a test session")
        signed_url = (r.json() or {}).get("signed_url") or ""
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"test-session signed url failed: {e}")
        raise HTTPException(502, "Could not reach ElevenLabs")
    if not signed_url:
        raise HTTPException(502, "ElevenLabs returned no signed URL")
    return {"signed_url": signed_url}


@api.get("/elevenlabs/agent/preview-prompt")
async def elevenlabs_preview_prompt(user: dict = Depends(current_super_admin)):
    """Return the system prompt that WILL be synced — for the recruiter to preview before pushing."""
    from voice_service import _convert_placeholders_to_elevenlabs
    settings = await get_or_create_settings(user["id"])
    sca = settings.get("screen_call_agent", {}) or {}
    booking_prefs = settings.get("booking_preferences", {}) or {}
    return {
        "system_prompt": build_agent_system_prompt(sca, booking_prefs=booking_prefs),
        "first_message": _convert_placeholders_to_elevenlabs(sca.get("opening_message") or ""),
        "agent_id": sca.get("elevenlabs_agent_id", ""),
        "voice_id": sca.get("voice_id", ""),
        "language": sca.get("language", "English"),
    }


@api.put("/settings/tenant-concurrency")
async def update_tenant_concurrency(
    payload: dict = Body(...),
    user: dict = Depends(current_super_admin),
):
    """Update the tenant-wide concurrent-call ceiling (matches your ElevenLabs
    plan limit). Pipeline-level caps are clamped by this value at runtime."""
    cap = int(payload.get("tenant_max_concurrent_calls") or 5)
    if cap < 1 or cap > 200:
        raise HTTPException(400, "tenant_max_concurrent_calls must be 1..200")
    await get_or_create_settings(user["id"])
    await db.settings.update_one(
        {"user_id": user["id"], "pipeline_id": None},
        {"$set": {"tenant_max_concurrent_calls": cap, "updated_at": now_iso()}},
    )
    return {"ok": True, "tenant_max_concurrent_calls": cap}


@api.post("/elevenlabs/agent/sync")
async def elevenlabs_sync_agent(
    pipeline_id: Optional[str] = Body(None, embed=True),
    user: dict = Depends(current_user),
):
    """PATCH the global ElevenLabs agent + every per-pipeline override agent.
    Each pipeline's agent gets the base prompt + that pipeline's location-specific FAQs baked in,
    so even without runtime conversation_config_override the agent has the right context.

    When `pipeline_id` is provided in the body, ONLY that pipeline's agent is
    re-synced (using its per-pipeline settings overrides). Without it, the
    global agent + every per-pipeline agent are re-synced. This is what gets
    called automatically when a user saves the Screen Call Agent settings on
    a specific pipeline — so changing the voice on one office only affects that office.

    Recruiters can sync THEIR pipeline's agent (after editing voice/LLM/prompt).
    The global multi-pipeline sync (no pipeline_id) stays super-admin only since
    it touches every pipeline at once."""
    require_mover(user)  # viewers and analysts never push to the live agent
    if pipeline_id:
        await assert_pipeline_access(user, pipeline_id)
    else:
        require_super_admin(user)
    # When syncing a single pipeline, read its OVERRIDE settings so per-pipeline
    # voice / agent name / etc. actually take effect.
    settings = await resolve_settings(user["id"], pipeline_id)
    if not settings:
        settings = await get_or_create_settings(user["id"])
    sca = settings.get("screen_call_agent", {}) or {}
    booking_prefs = settings.get("booking_preferences", {}) or {}
    voice_id = sca.get("voice_id") or None
    base_agent_id = sca.get("elevenlabs_agent_id", "")

    # Step 1: upsert workspace-level webhook tools (get_available_slots + book_slot)
    # so the agent can fetch real availability and book appointments mid-call.
    from voice_service import upsert_elevenlabs_tools
    public_base = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
    tool_map = upsert_elevenlabs_tools(public_base)
    tool_ids = [tid for tid in tool_map.values() if tid]

    results = []
    synced_agents = set()

    if base_agent_id and not pipeline_id:
        # Sync the "global" agent first. When a pipeline shares its
        # `elevenlabs_agent_id_override` with `base_agent_id` (e.g. the
        # first office's agent IS also the tenant default), merge that
        # pipeline's per-location override into the prompt — otherwise the
        # location-specific context (office address, transit stops)
        # would only land on dedicated override agents, and the candidate
        # dialed by the shared agent would hear a generic prompt.
        global_sca = dict(sca)
        sharing_pipeline = await db.pipelines.find_one(
            {"user_id": user["id"], "elevenlabs_agent_id_override": base_agent_id},
            {"_id": 0},
        )
        if sharing_pipeline:
            if sharing_pipeline.get("agent_name_override"):
                global_sca["agent_name"] = sharing_pipeline["agent_name_override"]
            if sharing_pipeline.get("first_message_override"):
                global_sca["opening_message"] = sharing_pipeline["first_message_override"]
            if sharing_pipeline.get("additional_context_override"):
                existing_ctx = global_sca.get("additional_context", "")
                global_sca["additional_context"] = (
                    f"{existing_ctx}\n\n# Location-Specific Context "
                    f"({sharing_pipeline.get('name', '')})\n"
                    f"{sharing_pipeline['additional_context_override']}"
                ).strip()
        global_voice = (
            (sharing_pipeline or {}).get("voice_id_override")
            or global_sca.get("voice_id")
            or voice_id
        )
        r = sync_agent_to_elevenlabs(base_agent_id, global_sca, voice_id=global_voice, tool_ids=tool_ids, booking_prefs=booking_prefs)
        scope_label = (
            f"global ({sharing_pipeline['name']})" if sharing_pipeline
            else "global"
        )
        results.append({"scope": scope_label, "agent_id": base_agent_id, **r})
        synced_agents.add(base_agent_id)

    # Now sync each pipeline's override agent with location-specific context.
    # When `pipeline_id` is set, scope to only that pipeline so the user's voice
    # change on one office doesn't ripple to another.
    pipelines_q: Dict[str, Any] = {"user_id": user["id"]}
    if pipeline_id:
        pipelines_q["id"] = pipeline_id
    pipelines = await db.pipelines.find(pipelines_q, {"_id": 0}).to_list(200)
    from voice_service import _convert_placeholders_to_elevenlabs
    for p in pipelines:
        agent_id = p.get("elevenlabs_agent_id_override", "")
        if not agent_id or agent_id in synced_agents:
            continue
        # Pull THIS pipeline's resolved settings (override merged on global) so
        # voice / prompt / agent name / etc. all reflect that pipeline.
        pipe_settings = await resolve_settings(user["id"], p["id"])
        pipeline_sca = (pipe_settings or {}).get("screen_call_agent", {}) or dict(sca)
        if p.get("agent_name_override"):
            pipeline_sca["agent_name"] = p["agent_name_override"]
        if p.get("first_message_override"):
            pipeline_sca["opening_message"] = p["first_message_override"]
        if p.get("additional_context_override"):
            existing_ctx = pipeline_sca.get("additional_context", "")
            pipeline_sca["additional_context"] = (
                f"{existing_ctx}\n\n# Location-Specific Context ({p.get('name', '')})\n{p['additional_context_override']}"
            ).strip()
        pipe_booking_prefs = (pipe_settings or {}).get("booking_preferences") or {}
        pipe_voice = p.get("voice_id_override") or pipeline_sca.get("voice_id") or voice_id
        r = sync_agent_to_elevenlabs(agent_id, pipeline_sca, voice_id=pipe_voice, tool_ids=tool_ids, booking_prefs=pipe_booking_prefs)
        results.append({"scope": p.get("name"), "agent_id": agent_id, **r})
        synced_agents.add(agent_id)

    # If a specific pipeline was requested but has no override agent, fall back to
    # syncing the global agent with that pipeline's merged settings. Without this,
    # saving settings for a pipeline that shares the global agent syncs nothing.
    if not results and pipeline_id and base_agent_id:
        pipe = next((p for p in pipelines if p["id"] == pipeline_id), None)
        if pipe:
            pipe_settings = await resolve_settings(user["id"], pipeline_id)
            pipeline_sca = (pipe_settings or {}).get("screen_call_agent", {}) or dict(sca)
            if pipe.get("agent_name_override"):
                pipeline_sca["agent_name"] = pipe["agent_name_override"]
            if pipe.get("first_message_override"):
                pipeline_sca["opening_message"] = pipe["first_message_override"]
            if pipe.get("additional_context_override"):
                pipeline_sca["additional_context"] = (
                    f"{pipeline_sca.get('additional_context', '')}\n\n"
                    f"# Location-Specific Context ({pipe.get('name', '')})\n"
                    f"{pipe['additional_context_override']}"
                ).strip()
            pipe_voice = pipe.get("voice_id_override") or pipeline_sca.get("voice_id") or voice_id
            pipe_bp = (pipe_settings or {}).get("booking_preferences") or {}
            r = sync_agent_to_elevenlabs(base_agent_id, pipeline_sca, voice_id=pipe_voice, tool_ids=tool_ids, booking_prefs=pipe_bp)
            results.append({"scope": f"{pipe.get('name')} (global agent)", "agent_id": base_agent_id, **r})

    if not results:
        raise HTTPException(400, "No agents configured to sync. Set Agent ID in Settings or per pipeline.")
    summary = {
        "synced": sum(1 for r in results if r.get("status") == "synced"),
        "failed": sum(1 for r in results if r.get("status") == "failed"),
        "tools": tool_map,
    }
    return {"summary": summary, "results": results}


# ===== Pipeline Intelligence =====
# Candidate date fields the Intelligence filters may range over.
INTELLIGENCE_DATE_FIELDS = frozenset({
    "created_at", "updated_at", "appointment_at", "archived_at",
    "form_submitted_at", "moved_to_close_at", "moved_to_training_at", "training_start_at",
})


@api.get("/intelligence/dashboard")
async def intelligence_dashboard(
    pipeline_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    date_field: str = "created_at",
    exit_date_from: Optional[str] = None,
    exit_date_to: Optional[str] = None,
    user: dict = Depends(current_user),
):
    q: Dict[str, Any] = {"user_id": user["id"]}
    # Scope to the caller's pipelines like every other read. `user_id` alone is
    # the whole tenant, so a one-city recruiter used to see every city here.
    if pipeline_id:
        await assert_pipeline_access(user, pipeline_id)
        q["pipeline_id"] = pipeline_id
    else:
        q.update(pipeline_scope_filter(user))
    analyst_filter = analyst_intelligence_filter(user, pipeline_id)
    if analyst_filter:
        q.update(analyst_filter)
    # date_field becomes a Mongo key, so it must be one of the date fields — a
    # free-form name such as "user_id" or "pipeline_id" would replace the
    # tenant/office scope set above.
    if date_field not in INTELLIGENCE_DATE_FIELDS:
        raise HTTPException(400, "Invalid date_field")

    # Date range filter — applied to whichever field the caller specifies.
    # date_to is inclusive so we extend to end-of-day by bumping +1 day.
    if date_from or date_to:
        dq: Dict[str, Any] = {}
        if date_from:
            dq["$gte"] = date_from
        if date_to:
            end = (date.fromisoformat(date_to) + timedelta(days=1)).isoformat()
            dq["$lt"] = end
        q[date_field] = dq

    cands = await db.candidates.find(q, {"_id": 0}).to_list(5000)
    total = len(cands)
    by_stage = {s: 0 for s in ["SCREENING", "APPOINTMENT", "FORM", "CLOSE", "TRAINING"]}
    for c in cands:
        stage = c.get("stage", "SCREENING")
        by_stage[stage] = by_stage.get(stage, 0) + 1

    closed  = sum(1 for c in cands if c.get("stage") == "CLOSE")
    booked  = sum(1 for c in cands if c.get("appointment_at"))
    screened = sum(1 for c in cands if c.get("call_summary") or c.get("suitability_score") is not None)
    attended_interview = sum(1 for c in cands if c.get("attendance_status") in ("attended_form", "attended_no_form"))
    forms_sent = sum(1 for c in cands if (
        c.get("stage") in ("FORM", "TRAINING") or c.get("form_submitted_at") or c.get("form_responses")
    ))
    forms_submitted = sum(1 for c in cands if c.get("form_submitted_at"))
    booked_to_start  = sum(1 for c in cands if c.get("training_start_at"))
    attended_training = sum(1 for c in cands if c.get("training_attended"))

    # Trend chart — span the requested date range, fallback to last 21 days.
    today = datetime.now(timezone.utc).date()
    if date_from and date_to:
        trend_from = date.fromisoformat(date_from)
        trend_to = date.fromisoformat(date_to)
    else:
        trend_from = today - timedelta(days=20)
        trend_to = today
    trend = []
    d = trend_from
    while d <= trend_to:
        ds = d.isoformat()
        count = sum(1 for c in cands if (c.get("created_at") or "").startswith(ds))
        trend.append({"date": ds, "added": count})
        d += timedelta(days=1)

    # Screening channel breakdown
    convs_q: Dict[str, Any] = {"user_id": user["id"]}
    if not is_super_admin(user):
        # Calls carry no pipeline_id; scope them through their candidates.
        # (The owner's view is deliberately unchanged.)
        scope_q = {"user_id": user["id"], "pipeline_id": q["pipeline_id"]} if "pipeline_id" in q else {"user_id": user["id"]}
        scoped_ids = [c["id"] async for c in db.candidates.find(scope_q, {"_id": 0, "id": 1})]
        convs_q["candidate_id"] = {"$in": scoped_ids}
    convs = await db.conversations.find(convs_q, {"_id": 0}).to_list(2000)
    phone_done = sum(1 for c in convs if c.get("status") == "completed")
    phone_total = max(len(convs), 1)
    channels = [
        {"name": "Phone", "completion": int((phone_done / phone_total) * 100), "count": len(convs)},
        {"name": "Portal Voice", "completion": 0, "count": 0},
        {"name": "Portal Chat", "completion": 0, "count": 0},
    ]

    # Recruitment velocity — avg days between consecutive milestone timestamps.
    # Only candidates that reached BOTH endpoints of a transition count toward that avg.
    def _avg_days(from_field: str, to_field: str) -> tuple:
        deltas = []
        for c in cands:
            f_val = c.get(from_field)
            t_val = c.get(to_field)
            if not f_val or not t_val:
                continue
            try:
                f_dt = datetime.fromisoformat(f_val.replace("Z", "+00:00"))
                t_dt = datetime.fromisoformat(t_val.replace("Z", "+00:00"))
                delta = (t_dt - f_dt).total_seconds() / 86400
                if delta >= 0:
                    deltas.append(delta)
            except Exception:
                pass
        avg = round(sum(deltas) / len(deltas), 4) if deltas else None
        return avg, len(deltas)

    velocity_transitions = [
        ("Added",      "Appointment",  "created_at",        "appointment_at"),
        ("Appointment","Form Sent",    "appointment_at",     "moved_to_form_at"),
        ("Form Sent",  "Form Done",    "moved_to_form_at",   "form_submitted_at"),
        ("Form Done",  "Close",        "form_submitted_at",  "moved_to_close_at"),
        ("Close",      "Training",     "moved_to_close_at",  "moved_to_training_at"),
    ]
    velocity = []
    for from_label, to_label, from_field, to_field in velocity_transitions:
        avg, n = _avg_days(from_field, to_field)
        velocity.append({
            "from_stage": from_label,
            "to_stage": to_label,
            "avg_days": avg,
            "sample_size": n,
        })

    # ── Exit pipeline analytics ───────────────────────────────────────────────
    # Query only archived candidates for this user + optional pipeline scope.
    # Uses a separate date filter (exit_date_from/exit_date_to) applied to
    # archived_at, independent of the main created_at filter above.
    exit_q: Dict[str, Any] = {
        "user_id": user["id"],
        "archived_at": {"$ne": None, "$exists": True},
    }
    if "pipeline_id" in q:
        exit_q["pipeline_id"] = q["pipeline_id"]
    if exit_date_from or exit_date_to:
        adq: Dict[str, Any] = {"$ne": None, "$exists": True}
        if exit_date_from:
            adq["$gte"] = exit_date_from
        if exit_date_to:
            adq["$lt"] = (date.fromisoformat(exit_date_to) + timedelta(days=1)).isoformat()
        exit_q["archived_at"] = adq

    archived_docs = await db.candidates.find(
        exit_q, {"_id": 0, "archived_at": 1, "archived_reason": 1}
    ).to_list(5000)

    def _exit_category(reason: Optional[str]) -> str:
        if not reason:
            return "other"
        r = reason.lower()
        if "merged_into" in r or "duplicate" in r:
            return "duplicate"
        if "no_show" in r or "no-show" in r or "noshow" in r:
            return "no_show"
        if "uncontact" in r or "no_answer" in r or "no_contact" in r or "unreachable" in r:
            return "uncontactable"
        if "withdraw" in r or "withdrew" in r or "pulled out" in r:
            return "withdrawn"
        if r == "attended_no_form" or "attended_no_form" in r:
            return "attended_no_form"
        if "reject" in r or "disqualif" in r or "failed" in r:
            return "rejected"
        if r == "manual" or "not a fit" in r or "not fit" in r:
            return "manual"
        return "other"

    EXIT_CATEGORY_LABELS = {
        "no_show":          "No-Show",
        "uncontactable":    "Uncontactable",
        "withdrawn":        "Withdrawn",
        "attended_no_form": "Attended — Not Progressed",
        "rejected":         "Rejected",
        "duplicate":        "Duplicate",
        "manual":           "Manual / Not a Fit",
        "other":            "Other",
    }

    from collections import defaultdict

    # Per-category totals
    exit_counts: Dict[str, int] = defaultdict(int)
    for doc in archived_docs:
        exit_counts[_exit_category(doc.get("archived_reason"))] += 1

    total_exits = len(archived_docs)

    # 4-week trend per category (Mon–Sun windows, most recent last)
    today_d = datetime.now(timezone.utc).date()
    trend_weeks: list = []
    for i in range(3, -1, -1):
        w_start = today_d - timedelta(days=today_d.weekday()) - timedelta(weeks=i)
        trend_weeks.append(w_start.isoformat())

    cat_trend: Dict[str, list] = defaultdict(lambda: [0, 0, 0, 0])
    for doc in archived_docs:
        raw_at = (doc.get("archived_at") or "")[:10]
        if not raw_at:
            continue
        cat = _exit_category(doc.get("archived_reason"))
        for wi, ws in enumerate(trend_weeks):
            w_end = (date.fromisoformat(ws) + timedelta(days=7)).isoformat()
            if ws <= raw_at < w_end:
                cat_trend[cat][wi] += 1
                break

    exits_by_reason = []
    for key, label in EXIT_CATEGORY_LABELS.items():
        count = exit_counts.get(key, 0)
        exits_by_reason.append({
            "key": key,
            "label": label,
            "count": count,
            "pct": round(count / total_exits * 100, 1) if total_exits else 0,
            "trend": cat_trend.get(key, [0, 0, 0, 0]),
        })
    exits_by_reason.sort(key=lambda x: -x["count"])

    exits = {
        "total": total_exits,
        "by_reason": exits_by_reason,
        "trend_weeks": trend_weeks,
    }

    # ── Attendance patterns: show-rate by day-of-week × hour ─────────────────
    # All-time (not bounded by the dashboard date filter) — slot patterns are
    # structural and benefit from every data point. Only candidates whose
    # attendance was actually marked count; future/unmarked appointments are
    # excluded so the rates aren't diluted.
    from zoneinfo import ZoneInfo as _ZoneInfo
    _patterns_tz = app_zone()
    patt_q: Dict[str, Any] = {
        "user_id": user["id"],
        "appointment_at": {"$ne": None, "$exists": True},
        "attendance_status": {"$in": ["attended_form", "attended_no_form", "no_show"]},
    }
    if "pipeline_id" in q:
        patt_q["pipeline_id"] = q["pipeline_id"]
    patt_docs = await db.candidates.find(
        patt_q, {"_id": 0, "appointment_at": 1, "attendance_status": 1}
    ).to_list(10000)

    slot_cells: Dict[tuple, Dict[str, int]] = defaultdict(lambda: {"booked": 0, "attended": 0})
    for doc in patt_docs:
        try:
            appt_local = datetime.fromisoformat(
                doc["appointment_at"].replace("Z", "+00:00")
            ).astimezone(_patterns_tz)
        except Exception:
            continue
        cell = slot_cells[(appt_local.weekday(), appt_local.hour)]
        cell["booked"] += 1
        if doc["attendance_status"] in ("attended_form", "attended_no_form"):
            cell["attended"] += 1

    attendance_patterns = {
        "total_marked": len(patt_docs),
        "cells": [
            {
                "day": day, "hour": hour,
                "booked": v["booked"], "attended": v["attended"],
                "show_rate": round(v["attended"] / v["booked"] * 100, 1) if v["booked"] else 0,
            }
            for (day, hour), v in sorted(slot_cells.items())
        ],
    }

    return {
        "kpis": {
            "applicants_added": total,
            "applicants_screened": screened,
            "applicants_booked": booked,
            "attended_interview": attended_interview,
            "forms_sent": forms_sent,
            "forms_submitted": forms_submitted,
            "booked_to_start": booked_to_start,
            "attended_training": attended_training,
            # legacy fields kept so old clients don't break
            "closed": closed,
            "added_to_attended": booked,
            "time_saved_hours": round(len(convs) * 0.25, 1),
        },
        "by_stage": by_stage,
        "trend": trend,
        "channels": channels,
        "pipeline_steps": [
            {"name": "Added",     "count": total},
            {"name": "Screened",  "count": screened},
            {"name": "Booked",    "count": booked},
            {"name": "Attended",  "count": attended_interview},
            {"name": "Form Sent", "count": forms_sent},
            {"name": "Form Done", "count": forms_submitted},
            {"name": "Starting",  "count": booked_to_start},
            {"name": "Training",  "count": attended_training},
        ],
        "velocity": velocity,
        "exits": exits,
        "attendance_patterns": attendance_patterns,
    }


@api.get("/intelligence/cohort-week")
async def intelligence_cohort_week(
    pipeline_id: Optional[str] = None,
    week_start: Optional[str] = None,
    user: dict = Depends(current_user),
):
    """Cohort view: candidates ADDED in a Mon–Sun window and their eventual pipeline outcomes.
    Includes archived candidates so the full cohort is visible."""
    q: Dict[str, Any] = {"user_id": user["id"]}
    if pipeline_id:
        await assert_pipeline_access(user, pipeline_id)
        q["pipeline_id"] = pipeline_id
    else:
        q.update(pipeline_scope_filter(user))
    analyst_filter = analyst_intelligence_filter(user, pipeline_id)
    if analyst_filter:
        q.update(analyst_filter)
    if week_start:
        week_end = (date.fromisoformat(week_start) + timedelta(days=7)).isoformat()
        q["created_at"] = {"$gte": week_start, "$lt": week_end}

    cands = await db.candidates.find(q, {"_id": 0}).to_list(5000)
    total = len(cands)

    # Attempted: any dial made (includes voicemail, no-answer, hung-up)
    screened_attempted = sum(1 for c in cands if c.get("call_attempts", 0) >= 1)

    # Successful: we actually spoke with them — voicemail excluded.
    # Signals: definitively screened status, OR AI verdict set without voicemail,
    # OR last_call_status=completed without voicemail flag.
    def _successfully_screened(c: Dict[str, Any]) -> bool:
        if c.get("screening_status") in ("approved", "appointment_pending", "rejected"):
            return True
        if c.get("last_call_voicemail"):
            return False
        if c.get("verdict"):
            return True
        if c.get("last_call_status") == "completed":
            return True
        return False

    screened_successful = sum(1 for c in cands if _successfully_screened(c))
    booked = sum(1 for c in cands if c.get("appointment_at"))
    attended = sum(1 for c in cands if c.get("attendance_status") in ("attended_form", "attended_no_form"))
    invited_to_form = sum(1 for c in cands if c.get("moved_to_form_at") or c.get("stage") in ("FORM", "CLOSE", "TRAINING") or c.get("form_submitted_at"))
    forms_submitted = sum(1 for c in cands if c.get("form_submitted_at"))
    to_close = sum(1 for c in cands if c.get("form_submitted_at") or c.get("moved_to_close_at") or c.get("stage") in ("CLOSE", "TRAINING"))
    training = sum(1 for c in cands if c.get("moved_to_training_at") or c.get("hired") or c.get("stage") == "TRAINING")
    archived = sum(1 for c in cands if c.get("archived_at"))

    # Stage distribution counts only ACTIVE candidates — archived ones must not appear
    # in both their stage bucket and the Archived bucket (double-count would make
    # percentages exceed 100% and mislead anyone reviewing the data).
    stages: Dict[str, int] = {s: 0 for s in ["SCREENING", "APPOINTMENT", "FORM", "CLOSE", "TRAINING"]}
    for c in cands:
        if not c.get("archived_at"):
            s = c.get("stage", "SCREENING")
            stages[s] = stages.get(s, 0) + 1

    return {
        "week_start": week_start,
        "total": total,
        "archived": archived,
        "active": total - archived,
        "funnel": {
            "added": total,
            "screened_attempted": screened_attempted,
            "screened_successful": screened_successful,
            "booked": booked,
            "attended": attended,
            "invited_to_form": invited_to_form,
            "forms_submitted": forms_submitted,
            "to_close": to_close,
            "training": training,
        },
        "stages": stages,
    }


@api.get("/intelligence/activity-week")
async def intelligence_activity_week(
    pipeline_id: Optional[str] = None,
    week_start: Optional[str] = None,
    user: dict = Depends(current_user),
):
    """Activity view: pipeline movements that OCCURRED within a Mon–Sun window,
    regardless of when the candidate originally applied."""
    q_base: Dict[str, Any] = {"user_id": user["id"]}
    if pipeline_id:
        await assert_pipeline_access(user, pipeline_id)
        q_base["pipeline_id"] = pipeline_id
    else:
        q_base.update(pipeline_scope_filter(user))
    analyst_filter = analyst_intelligence_filter(user, pipeline_id)
    if analyst_filter:
        q_base.update(analyst_filter)

    if not week_start:
        today_d = datetime.now(timezone.utc).date()
        week_start = (today_d - timedelta(days=today_d.weekday())).isoformat()

    week_end = (date.fromisoformat(week_start) + timedelta(days=7)).isoformat()

    # Appointments DUE this Mon–Sun
    appointments_due = await db.candidates.count_documents({
        **q_base,
        "appointment_at": {"$gte": week_start, "$lt": week_end},
    })
    # Of those, how many attended
    attended = await db.candidates.count_documents({
        **q_base,
        "appointment_at": {"$gte": week_start, "$lt": week_end},
        "attendance_status": {"$in": ["attended_form", "attended_no_form"]},
    })
    # Forms sent this week (moved to FORM stage)
    forms_sent = await db.candidates.count_documents({
        **q_base,
        "moved_to_form_at": {"$gte": week_start, "$lt": week_end},
    })
    # Moved to CLOSE this week — form submission OR manual Kanban drag
    moved_to_close = await db.candidates.count_documents({
        **q_base,
        "$or": [
            {"moved_to_close_at": {"$gte": week_start, "$lt": week_end}},
            {"form_submitted_at": {"$gte": week_start, "$lt": week_end}},
        ],
    })
    # Booked for training this week — Kanban drag OR hire-button path
    training_booked = await db.candidates.count_documents({
        **q_base,
        "$or": [
            {"moved_to_training_at": {"$gte": week_start, "$lt": week_end}},
            {"hired_at": {"$gte": week_start, "$lt": week_end}},
        ],
    })

    sunday = (date.fromisoformat(week_start) + timedelta(days=6)).isoformat()
    return {
        "week_start": week_start,
        "week_end": sunday,
        "appointments_due": appointments_due,
        "attended": attended,
        "forms_sent": forms_sent,
        "moved_to_close": moved_to_close,
        "training_booked": training_booked,
    }


@api.get("/intelligence/funnel-report")
async def intelligence_funnel_report(
    pipeline_id: Optional[str] = None,
    week_start: Optional[str] = None,
    period_end: Optional[str] = None,
    user: dict = Depends(current_user),
):
    """Funnel report: CGRecruit recruitment funnel + CG1 post-training metrics.

    week_start is the period start (ISO date); defaults to current week's Monday.
    period_end overrides the default +7 days — use for month/quarter views.
    """
    import httpx as _httpx, os as _os, traceback as _tb

    # ── Date range ────────────────────────────────────────────────────────────
    try:
        today_d = datetime.now(timezone.utc).date()
        if not week_start:
            week_start = (today_d - timedelta(days=today_d.weekday())).isoformat()
        week_end = period_end if period_end else (date.fromisoformat(week_start) + timedelta(days=7)).isoformat()
        ws_d = date.fromisoformat(week_start)
        we_d = date.fromisoformat(week_end) - timedelta(days=1)  # inclusive end for label
        if ws_d.year == we_d.year:
            week_label = f"{ws_d.strftime('%b %-d')} – {we_d.strftime('%b %-d, %Y')}"
        else:
            week_label = f"{ws_d.strftime('%b %-d, %Y')} – {we_d.strftime('%b %-d, %Y')}"
    except Exception as _e:
        raise HTTPException(400, f"Invalid week_start: {_e}")

    # ── Pipeline / office scoping ─────────────────────────────────────────────
    q: Dict[str, Any] = {"user_id": user["id"]}
    if pipeline_id:
        await assert_pipeline_access(user, pipeline_id)
        q["pipeline_id"] = pipeline_id
    else:
        q.update(pipeline_scope_filter(user))
    analyst_filter = analyst_intelligence_filter(user, pipeline_id)
    if analyst_filter:
        q.update(analyst_filter)

    pipe: dict = {}
    try:
        if pipeline_id:
            pipe = await db.pipelines.find_one({"id": pipeline_id, "user_id": user["id"]}, {"_id": 0}) or {}
        elif not is_super_admin(user):
            pids = user.get("pipeline_ids") or []
            if pids:
                pipe = await db.pipelines.find_one({"id": pids[0], "user_id": user["id"]}, {"_id": 0}) or {}
    except Exception as _e:
        logger.error(f"funnel-report: pipeline lookup failed: {_tb.format_exc()}")
        raise HTTPException(500, f"Pipeline lookup error: {_e}")

    office_key = company_profile.office_key_for_pipeline(pipe)

    _PROJ = {
        "_id": 0, "first_name": 1, "last_name": 1, "email": 1, "call_attempts": 1,
        "screening_status": 1, "verdict": 1, "last_call_status": 1, "last_call_voicemail": 1,
        "appointment_at": 1, "attendance_status": 1, "moved_to_form_at": 1,
        "form_submitted_at": 1, "moved_to_close_at": 1, "moved_to_training_at": 1,
        "hired": 1, "hired_at": 1, "training_start_at": 1, "training_attended": 1,
        "stage": 1, "cg1_user_id": 1,
    }

    # ── Cohort 1: candidates ADDED this week → recruitment funnel (steps 1–4) ─
    # Tracks the inflow of new applicants: CV Upload → AI Screened → Booked →
    # Attended Presentation → Filled Form → Booked to Start.
    try:
        recruit_cands = await db.candidates.find(
            {**q, "created_at": {"$gte": week_start, "$lt": week_end}},
            _PROJ,
        ).to_list(5000)
    except Exception as _e:
        logger.error(f"funnel-report: recruit query failed: {_tb.format_exc()}")
        raise HTTPException(500, f"Recruit query error: {_e}")

    added = len(recruit_cands)

    def _screened(c):
        if c.get("screening_status") in ("approved", "appointment_pending", "rejected"):
            return True
        if c.get("last_call_voicemail"):
            return False
        if c.get("verdict"):
            return True
        if c.get("last_call_status") == "completed":
            return True
        return False

    screened       = sum(1 for c in recruit_cands if _screened(c))
    booked         = sum(1 for c in recruit_cands if c.get("appointment_at"))
    attended       = sum(1 for c in recruit_cands if c.get("attendance_status") in ("attended_form", "attended_no_form"))
    form_submitted = sum(1 for c in recruit_cands if c.get("form_submitted_at"))
    # How many of this week's recruits were eventually booked to start (any time)
    recruited_to_training = sum(1 for c in recruit_cands if c.get("moved_to_training_at") or c.get("hired") or c.get("stage") == "TRAINING")

    # ── Cohort 2: NEW STARTERS this week → training funnel (steps 5–8) ────────
    # Keyed on training_start_at (the date they physically started on territory),
    # not on created_at. A recruit from weeks ago who starts June 1 belongs to
    # the WE June 7 new-starters cohort, not the week they applied.
    starters = await db.candidates.find(
        {**q, "training_start_at": {"$gte": week_start, "$lt": week_end}},
        _PROJ,
    ).to_list(1000)

    new_starters_count   = len(starters)
    training_attended_count = sum(1 for c in starters if c.get("training_attended"))

    # Build identity lists for the CG1 lookup
    # Send both emails AND names — CG1 tries email first, falls back to name
    starter_cg1_ids = [c["cg1_user_id"] for c in starters if c.get("cg1_user_id")]
    unlinked = [c for c in starters if not c.get("cg1_user_id")]
    starter_emails = [c["email"].strip().lower() for c in unlinked if c.get("email")]
    starter_names  = [
        f"{c.get('first_name', '')} {c.get('last_name', '')}".strip()
        for c in unlinked
        if c.get("first_name") or c.get("last_name")
    ]

    # ── CG1 post-training metrics (keyed on new-starters cohort) ─────────────
    cg1_url    = _os.getenv("CG1_BACKEND_URL", "").rstrip("/")
    cg1_secret = _os.getenv("CG1_WEBHOOK_SECRET", "")
    cg1_data: dict = {"badged": None, "made_sale": None, "week1_complete": None, "names_resolved": 0}

    if cg1_url and office_key:
        try:
            async with _httpx.AsyncClient(timeout=15.0) as hc:
                r = await hc.post(
                    f"{cg1_url}/api/webhooks/cgrecruit/funnel-stats",
                    headers={"x-webhook-secret": cg1_secret, "Content-Type": "application/json"},
                    json={
                        "office_key":    office_key,
                        "week_start":    week_start,
                        "cg1_user_ids":  starter_cg1_ids,
                        "emails":        starter_emails,
                        "names":         starter_names,
                    },
                )
                if r.status_code == 200:
                    cg1_data.update(r.json())
        except Exception as ex:
            logger.warning(f"funnel-report: CG1 stats call failed: {ex}")

    # ── Benchmarks ────────────────────────────────────────────────────────────
    settings_doc = {}
    if pipeline_id:
        settings_doc = await db.settings.find_one(
            {"user_id": user["id"], "pipeline_id": pipeline_id}, {"_id": 0, "funnel_benchmarks": 1}
        ) or {}
    benchmarks = {
        "cv_to_screened":       65,
        "booked_to_attended":   40,
        "attended_to_form":     70,
        "form_to_training":     65,
        "training_to_attended": 70,
        "attended_to_badge":    70,
        "badge_to_sale":        70,
        "badge_to_week1":       70,
        **(settings_doc.get("funnel_benchmarks") or {}),
    }

    def pct(num, denom):
        if num is not None and denom and denom > 0:
            return round((num / denom) * 100)
        return None

    return {
        "week_start":  week_start,
        "week_label":  week_label,
        "office_key":  office_key,
        "pipeline_id": pipeline_id,
        "benchmarks":  benchmarks,
        # Raw counts split by cohort so the UI can label them clearly
        "actuals": {
            # Recruitment cohort (candidates added this WE)
            "added":               added,
            "screened":            screened,
            "booked":              booked,
            "attended":            attended,
            "form_submitted":      form_submitted,
            "recruited_to_training": recruited_to_training,
            # New-starters cohort (training_start_at this WE — may have been recruited weeks ago)
            "new_starters":        new_starters_count,
            "training_attended":   training_attended_count,
            "badged":              cg1_data.get("badged"),
            "made_sale":           cg1_data.get("made_sale"),
            "week1_complete":      cg1_data.get("week1_complete"),
        },
        "rates": {
            # Steps 1–4: recruitment cohort
            "cv_to_screened":       pct(screened, added),
            "booked_to_attended":   pct(attended, booked),
            "attended_to_form":     pct(form_submitted, attended),
            "form_to_training":     pct(recruited_to_training, form_submitted),
            # Steps 5–8: new-starters cohort
            "training_to_attended": pct(training_attended_count, new_starters_count),
            "attended_to_badge":    pct(cg1_data.get("badged"), training_attended_count),
            "badge_to_sale":        pct(cg1_data.get("made_sale"), cg1_data.get("badged")),
            "badge_to_week1":       pct(cg1_data.get("week1_complete"), cg1_data.get("badged")),
        },
        "cg1_available":       bool(cg1_url and office_key),
        "cg1_names_resolved":  cg1_data.get("names_resolved", 0),
        "starters_sent_to_cg1": len(starter_cg1_ids) + len(starter_names),
    }


@api.get("/intelligence/export-csv")
async def intelligence_export_csv(
    pipeline_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    date_field: str = "created_at",
    user: dict = Depends(current_user),
):
    import csv, io
    from fastapi.responses import StreamingResponse

    # The export is a list of named people with contact details. Analysts are
    # aggregates-only by design, so they don't get it at all.
    if is_analyst(user):
        raise HTTPException(403, "Analyst accounts see aggregates only")
    if date_field not in INTELLIGENCE_DATE_FIELDS:
        raise HTTPException(400, "Invalid date_field")
    q: Dict[str, Any] = {"user_id": user["id"]}
    if pipeline_id:
        await assert_pipeline_access(user, pipeline_id)
        q["pipeline_id"] = pipeline_id
    else:
        q.update(pipeline_scope_filter(user))
    # Viewers only ever see the candidates they added themselves.
    q.update(candidate_ownership_filter(user))
    if date_from or date_to:
        dq: Dict[str, Any] = {}
        if date_from:
            dq["$gte"] = date_from
        if date_to:
            end = (date.fromisoformat(date_to) + timedelta(days=1)).isoformat()
            dq["$lt"] = end
        q[date_field] = dq

    cands = await db.candidates.find(q, {"_id": 0}).to_list(5000)

    def _csv_screened(c: Dict[str, Any]) -> str:
        if c.get("screening_status") in ("approved", "appointment_pending", "rejected"):
            return "Yes"
        if c.get("last_call_voicemail"):
            return "No"
        if c.get("verdict") or c.get("call_summary") or c.get("suitability_score") is not None:
            return "Yes"
        if c.get("last_call_status") == "completed":
            return "Yes"
        return "No"

    fields = [
        ("First Name",         lambda c: c.get("first_name", "")),
        ("Last Name",          lambda c: c.get("last_name", "")),
        ("Email",              lambda c: c.get("email", "")),
        ("Phone",              lambda c: c.get("phone", "")),
        ("Pipeline",           lambda c: c.get("pipeline_id", "")),
        ("Stage",              lambda c: c.get("stage", "")),
        ("Archived",           lambda c: "Yes" if c.get("archived_at") else "No"),
        ("Archived Date",      lambda c: (c.get("archived_at") or "")[:10]),
        ("Archived Reason",    lambda c: c.get("archived_reason") or ""),
        ("Applied Date",       lambda c: (c.get("created_at") or "")[:10]),
        ("Screened",           _csv_screened),
        ("Suitability Score",  lambda c: str(c.get("suitability_score") or "")),
        ("Verdict",            lambda c: c.get("verdict", "")),
        ("Interview At",       lambda c: (c.get("appointment_at") or "")[:16]),
        ("Interview Status",   lambda c: c.get("attendance_status", "")),
        ("Form Submitted",     lambda c: "Yes" if c.get("form_submitted_at") else "No"),
        ("Form Submitted At",  lambda c: (c.get("form_submitted_at") or "")[:10]),
        ("Reached Close",      lambda c: "Yes" if (c.get("moved_to_close_at") or c.get("form_submitted_at")) else "No"),
        ("Reached Training",   lambda c: "Yes" if (c.get("moved_to_training_at") or c.get("hired")) else "No"),
        ("Training Start",     lambda c: (c.get("training_start_at") or "")[:10]),
        ("Attended Training",  lambda c: "Yes" if c.get("training_attended") else "No"),
        ("Call Summary",       lambda c: (c.get("call_summary") or "").replace("\n", " ")),
    ]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([f[0] for f in fields])
    for c in cands:
        writer.writerow([f[1](c) for f in fields])

    filename = f"cgrecruit-export-{date_from or 'all'}-to-{date_to or 'all'}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ===== Calendar =====
@api.get("/calendar/appointments")
async def list_appointments(
    pipeline_id: Optional[str] = None,
    limit: int = 2000,
    user: dict = Depends(current_user),
):
    """Every booked appointment, newest first.

    This used to be `.to_list(500)` with no sort at all. On a busy tenant that
    silently dropped booked interviews in whatever order Mongo felt like
    returning, so past sessions could simply be absent from the Calendar — part
    of why many interviews never got an outcome recorded. Sorted descending so the cap, if it is ever reached
    again, drops the oldest rather than an arbitrary slice.
    """
    query: dict = {"user_id": user["id"], "appointment_at": {"$ne": None}}
    if pipeline_id:
        await assert_pipeline_access(user, pipeline_id)
        query["pipeline_id"] = pipeline_id
    else:
        query.update(pipeline_scope_filter(user))
    query.update(candidate_ownership_filter(user))
    rows = await (
        db.candidates.find(query, {"_id": 0})
        .sort("appointment_at", -1)
        .to_list(max(1, min(int(limit or 2000), 5000)))
    )
    return rows


@api.get("/settings/screening-prompt-preview")
async def screening_prompt_preview(
    pipeline_id: Optional[str] = None,
    channel: str = "sms",
    user: dict = Depends(current_user),
):
    """The assembled text-screening prompt, exactly as the assistant receives it.

    There is no single screen showing this. The Screen Call Agent section shows
    the phone version, and the text screening is assembled from three different
    places — the shared question list, the per-stage SMS instruction, and the
    booking rules — so the only way to know what the assistant is actually being
    told has been to read the source. That is a poor way to check the thing that
    talks to every applicant.
    """
    # The assembled prompt can contain operator-authored context, so it is not
    # for any sub-account to read for any pipeline. Movers and admins only, and
    # only for a pipeline they are assigned to.
    require_mover(user)
    if pipeline_id:
        await assert_pipeline_access(user, pipeline_id)
    settings = await resolve_settings(user["id"], pipeline_id)
    pipeline = {}
    if pipeline_id:
        pipeline = await db.pipelines.find_one({"id": pipeline_id, "user_id": user["id"]}, {"_id": 0}) or {}

    from models import resolve_screening_mode
    mode = resolve_screening_mode(settings)
    sample = {
        "id": "preview", "user_id": user["id"], "pipeline_id": pipeline_id,
        "first_name": "Sample", "last_name": "Candidate", "stage": "SCREENING",
    }

    if channel == "sms":
        from training_sms import _llm_system_prompt
        enrichment = {"settings": settings, "slots": [
            {"label": "Tue 9:15 AM", "datetime": "2026-08-04T13:15:00+00:00"},
            {"label": "Wed 2:00 PM", "datetime": "2026-08-05T18:00:00+00:00"},
        ]}
        prompt = _llm_system_prompt(sample, pipeline, "unknown", enrichment)
    else:
        from routes.retry import _build_retry_chat_system_prompt
        prompt = _build_retry_chat_system_prompt(
            sca=settings.get("screen_call_agent") or {},
            pipeline=pipeline,
            cand=sample,
            job_title=(settings.get("recruiter_profile") or {}).get("job_role") or "the role",
            company=(settings.get("recruiter_profile") or {}).get("company_name") or company_profile.company_name(),
            slug=pipeline.get("public_slug") or "",
        )

    sca = settings.get("screen_call_agent") or {}
    questions = sca.get("screening_questions") or []
    return {
        "channel": channel,
        "screening_mode": mode,
        "active": mode in ("chat_first", "chat_only"),
        "questions": [
            {"n": i, "question": q.get("question"), "hard_gate": bool(q.get("auto_screen"))}
            for i, q in enumerate(questions, 1)
        ],
        "hard_gates": sum(1 for q in questions if q.get("auto_screen")),
        "prompt": prompt,
        "prompt_chars": len(prompt),
    }


@api.post("/screening/reengage-by-text")
async def reengage_by_text(payload: Dict[str, Any] = Body(...), user: dict = Depends(current_user)):
    """One-shot revival of the voice-era backlog after a pipeline flips to text.

    Targets candidates the phone never reached — no_answer / didnt_connect /
    incomplete_info with the call cadence behind them — and sends the honest
    channel-switch opener (text asking Q1 + email with the chat button). Their
    pending calls are cancelled and auto-dial turned off: they've had four
    dials; from here they're text-first. Stamped so it can never double-send.

    dry_run (default TRUE) returns the cohort without sending — the recruiter
    sees the number before anything fires. Deliberately manual, never a sweep:
    re-approaching a cold list is a decision, not a cron job.
    """
    require_mover(user)
    pipeline_id = payload.get("pipeline_id") or ""
    if not isinstance(pipeline_id, str) or not pipeline_id.strip():
        raise HTTPException(400, "pipeline_id is required")
    pipeline_id = pipeline_id.strip()
    # Texts (and unarchives) up to 200 of the office's candidates, and the dry
    # run lists them: the office must be in your account and assigned to you.
    await assert_pipeline_in_tenant(user, pipeline_id)
    dry_run = bool(payload.get("dry_run", True))
    limit = max(1, min(int(payload.get("limit") or 200), 200))
    # The visible screening column is the tip: the auto-archive sweeps file
    # call-exhausted candidates away (max_attempts_no_contact, no_contact_48h),
    # so the real backlog lives in the archive. include_archived reaches it,
    # bounded by application age — a 3-month-old applicant has moved on, a
    # 3-week-old one often hasn't. Archived candidates are unarchived as they
    # are sent, so their replies land in the normal screening flow.
    include_archived = bool(payload.get("include_archived", False))
    max_age_days = max(1, min(int(payload.get("max_age_days") or 30), 90))
    settings = await resolve_settings(user["id"], pipeline_id)
    from models import resolve_screening_mode
    if resolve_screening_mode(settings) not in ("chat_first", "chat_only"):
        raise HTTPException(400, "This pipeline isn't on a text screening mode")
    base = {
        "user_id": user["id"], "pipeline_id": pipeline_id,
        "appointment_at": None,
        "phone": {"$nin": [None, ""]},
        "sms_opted_out": {"$ne": True},
        "disqualification_reason": None,
        "reengaged_by_text_at": None,
    }
    active_q = dict(base,
                    stage={"$in": ["APPLICANT", "SCREENING"]},
                    screening_status={"$in": ["no_answer", "didnt_connect", "incomplete_info"]},
                    archived_at=None)
    cands = await db.candidates.find(active_q, {"_id": 0}).sort("created_at", 1).to_list(limit)
    n_active = len(cands)
    # The intake-unification postmortem cohort: candidates whose screening
    # start silently failed, so nobody ever called OR texted them. They're not
    # archived and not no-answer — they're invisible to both arms above. They
    # get the ARRIVAL opener (warmup_chat_first), not reengage_chat: "we tried
    # calling but couldn't catch you" would be a lie, this genuinely is our
    # first contact. No age cap — the staleness was our failure, not theirs.
    include_stranded = bool(payload.get("include_stranded", False))
    stranded_ids: set = set()
    if include_stranded and len(cands) < limit:
        stranded_q = dict(base,
                          stage={"$in": ["APPLICANT", "SCREENING"]},
                          screening_status="pending",
                          first_outreach_at=None,
                          archived_at=None)
        # Most stranded candidates are stranded BECAUSE intake failed to
        # capture a phone (most stranded records have none). The
        # arrival email carries the chat link and works without one, so any
        # contactable channel qualifies here — not specifically SMS.
        stranded_q.pop("phone", None)
        stranded_q["$or"] = [
            {"phone": {"$nin": [None, ""]}},
            {"email": {"$nin": [None, ""]}},
        ]
        stranded = await db.candidates.find(stranded_q, {"_id": 0}).sort("created_at", 1).to_list(limit - len(cands))
        stranded_ids = {c["id"] for c in stranded}
        cands += stranded
    if include_archived and len(cands) < limit:
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        cutoff = (_dt.now(_tz.utc) - _td(days=max_age_days)).isoformat()
        archived_q = dict(base,
                          archived_at={"$ne": None},
                          archived_reason={"$regex": "max_attempts|no_contact", "$options": "i"},
                          created_at={"$gte": cutoff})
        cands += await db.candidates.find(archived_q, {"_id": 0}).sort("created_at", -1).to_list(limit - len(cands))
    if dry_run:
        return {
            "dry_run": True, "count": len(cands),
            "active": n_active, "stranded": len(stranded_ids),
            "archived": len(cands) - n_active - len(stranded_ids),
            "max_age_days": max_age_days,
            "sample": [f"{c.get('first_name','')} {c.get('last_name','')}".strip() for c in cands[:12]],
        }
    from deps import send_stage_comms
    sent = 0
    for c in cands:
        try:
            await send_stage_comms(
                user["id"], c,
                "warmup_chat_first" if c["id"] in stranded_ids else "reengage_chat",
            )
            try:
                from auto_dialer import cancel_pending_retry_calls
                cancel_pending_retry_calls(c["id"])
            except Exception:
                pass
            revive = {"archived_at": None, "archived_reason": None, "stage": "SCREENING"} if c.get("archived_at") else {}
            if c["id"] in stranded_ids and not c.get("first_outreach_at"):
                # This genuinely was the first contact — the speed-to-contact
                # metric and the stranded query should both see it happened.
                revive["first_outreach_at"] = now_iso()
            await db.candidates.update_one({"id": c["id"]}, {"$set": {
                **revive,
                # Soft outreach ONLY: no call may ever follow from this. auto_dial
                # off stops the sweeps; next_call_at cleared stops the startup
                # recovery re-creating a stale job after the next deploy.
                "reengaged_by_text_at": now_iso(), "auto_dial": False,
                "next_call_at": None, "updated_at": now_iso(),
            }})
            sent += 1
            await asyncio.sleep(0.3)  # pace the Twilio/SendGrid calls
        except Exception as e:
            logger.warning(f"reengage-by-text failed for {c.get('id')}: {e}")
    logger.info(f"reengage-by-text: {sent}/{len(cands)} sent for pipeline {pipeline_id}")
    return {"dry_run": False, "count": sent}


@api.get("/candidates/{candidate_id}/transcript")
async def candidate_transcript(candidate_id: str, user: dict = Depends(current_user)):
    """Everything this candidate said, on every channel, in one timeline.

    Their words live in three unrelated stores — phone calls in `conversations`,
    the web chat in `chat_log`, SMS in `training_sms_messages` — and only the
    first was ever visible in the ATS. A recruiter could listen back to a call
    but could not read a chat at all, which stops being tolerable the moment
    most people are screened by text.
    """
    from transcript_service import merge, summarise

    cand = await db.candidates.find_one(
        {"id": candidate_id, "user_id": user["id"]},
        {"_id": 0, "chat_log": 1, "first_name": 1, "last_name": 1,
         "pipeline_id": 1, "added_by_user_id": 1},
    )
    # user_id alone is the whole tenant for a sub-account, so it is not access
    # control. This returns everything a candidate ever said across every
    # channel — the strongest scoping in the app should gate it, not the weakest.
    await assert_candidate_access(user, cand)

    sms_rows = await db.training_sms_messages.find(
        {"candidate_id": candidate_id},
        {"_id": 0, "direction": 1, "body": 1, "timestamp": 1, "was_llm_reply": 1},
    ).sort("timestamp", 1).to_list(500)

    convs = await db.conversations.find(
        {"candidate_id": candidate_id},
        {"_id": 0, "id": 1, "created_at": 1, "status": 1, "duration_seconds": 1,
         "suitability_score": 1, "transcript": 1},
    ).sort("created_at", 1).to_list(100)

    turns = merge(cand.get("chat_log"), sms_rows, convs)
    return {"turns": turns, **summarise(turns)}


@api.get("/metrics/speed-to-contact")
async def speed_to_contact(
    pipeline_id: Optional[str] = None,
    days: int = 30,
    user: dict = Depends(current_user),
):
    """How long candidates take to reply after we reach out.

    Candidates are created the instant they apply and messaged within seconds,
    so this is effectively apply-to-engagement — the number the whole chat-first
    change is being judged on. Median leads because the distribution has a long
    tail: a few people reply four days later and drag a mean somewhere
    unrecognisable.

    `no_reply` is deliberately reported alongside, because a fast median across
    a handful of repliers is not a good result.
    """
    from nudge_schedule import response_delay_seconds, summarise_delays

    since = (datetime.now(timezone.utc) - timedelta(days=max(1, min(int(days or 30), 365)))).isoformat()
    query: Dict[str, Any] = {
        "user_id": user["id"],
        "first_outreach_at": {"$ne": None, "$gte": since},
    }
    if pipeline_id:
        # Passing a pipeline_id must not be a way around the assignment check —
        # the scoping was previously only on the no-parameter branch.
        await assert_pipeline_access(user, pipeline_id)
        query["pipeline_id"] = pipeline_id
    elif not is_super_admin(user):
        query["pipeline_id"] = {"$in": user.get("pipeline_ids") or []}

    rows = await db.candidates.find(
        query,
        {"_id": 0, "pipeline_id": 1, "first_outreach_at": 1, "first_reply_at": 1,
         "first_reply_channel": 1},
    ).to_list(5000)

    pipes = {
        p["id"]: p.get("name", "")
        for p in await db.pipelines.find({"user_id": user["id"]}, {"_id": 0, "id": 1, "name": 1}).to_list(200)
    }

    def bucket(subset):
        delays = [response_delay_seconds(r.get("first_outreach_at"), r.get("first_reply_at"))
                  for r in subset if r.get("first_reply_at")]
        out = summarise_delays([d for d in delays if d is not None])
        out["contacted"] = len(subset)
        out["no_reply"] = len(subset) - out["replies"]
        out["reply_rate"] = round(out["replies"] / len(subset), 3) if subset else None
        return out

    by_channel: Dict[str, int] = {}
    for r in rows:
        if r.get("first_reply_at"):
            ch = r.get("first_reply_channel") or "unknown"
            by_channel[ch] = by_channel.get(ch, 0) + 1

    return {
        "since": since,
        "overall": bucket(rows),
        "by_reply_channel": by_channel,
        "by_pipeline": {
            pipes.get(pid, pid): bucket([r for r in rows if r.get("pipeline_id") == pid])
            for pid in {r.get("pipeline_id") for r in rows if r.get("pipeline_id")}
        },
    }


@api.get("/calendar/outcome-due")
async def list_outcome_due(
    grace_minutes: int = 120,
    user: dict = Depends(current_user),
):
    """Interview sessions that have finished with people still unmarked.

    Grouped by *session* — the (time, meeting room) pair — rather than by
    pipeline, and this is the important part. Two offices can deliberately run
    some slots in ONE shared video room as joint sessions. Attendance used to be
    an office-by-office job, so the host who was actually in the room never saw
    the other office's attendees, and that office's recruiter wasn't there —
    candidates in the shared room mostly went without a recorded outcome.

    So a session here is everyone booked at the same time into the same link,
    whichever pipeline they belong to, and it is visible to anyone who can see at
    least one of the pipelines represented in it — i.e. whoever is hosting.
    """
    visible: Optional[List[str]] = None
    if not is_super_admin(user):
        visible = list(user.get("pipeline_ids") or [])
        if not visible:
            return {"sessions": [], "total_unmarked": 0}

    rows = await db.candidates.find(
        {
            "user_id": user["id"],
            "appointment_at": {"$ne": None},
            "attendance_status": None,
        },
        {
            "_id": 0, "id": 1, "first_name": 1, "last_name": 1, "pipeline_id": 1,
            "appointment_at": 1, "appointment_link": 1, "stage": 1,
            "appointment_sms_confirmed": 1, "archived_at": 1,
        },
    ).sort("appointment_at", -1).to_list(5000)

    pipes = {
        p["id"]: p.get("name", "")
        for p in await db.pipelines.find({"user_id": user["id"]}, {"_id": 0, "id": 1, "name": 1}).to_list(200)
    }

    from attendance_queue import group_unmarked_into_sessions

    out = group_unmarked_into_sessions(
        rows,
        pipeline_names=pipes,
        visible_pipeline_ids=visible,
        grace_minutes=grace_minutes,
    )
    return {"sessions": out, "total_unmarked": sum(s["unmarked"] for s in out)}


@api.get("/elevenlabs/tools-config")
async def elevenlabs_tools_config(user: dict = Depends(current_super_admin)):
    """Returns ready-to-paste tool definitions for the user's ElevenLabs agents.
    Recruiter copy-pastes into ElevenLabs Agent → Tools tab (or we PATCH them automatically)."""
    base = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
    pipelines = await db.pipelines.find({"user_id": user["id"]}, {"_id": 0}).to_list(50)
    return {
        "base_url": base,
        "tools": [
            {
                "type": "webhook",
                "name": "get_available_slots",
                "description": "Get a list of available interview appointment slots for the candidate. Call this when the candidate is ready to book an interview, BEFORE proposing any specific times. Returns ISO datetimes the candidate can pick from. Use the `pipeline_slug` dynamic variable.",
                "method": "GET",
                "url": f"{base}/api/public/availability/{{pipeline_slug}}",
                "path_params": ["pipeline_slug"],
            },
            {
                "type": "webhook",
                "name": "book_slot",
                "description": "Book a confirmed appointment slot for the candidate. Call this AFTER the candidate has verbally agreed to a specific slot returned by get_available_slots. Pass the exact ISO datetime returned earlier.",
                "method": "POST",
                "url": f"{base}/api/public/book-by-phone",
                "body_schema": {
                    "phone": "{{phone}}",
                    "slot_iso": "<chosen ISO datetime>",
                    "pipeline_slug": "{{pipeline_slug}}",
                },
            },
        ],
        "pipelines": [{"name": p.get("name"), "slug": p.get("public_slug")} for p in pipelines],
    }


def _public_base_url(request: Optional[Request] = None) -> str:
    """Resolve our own public base URL. Used by callbacks (TwiML, SendGrid webhook)
    that need to round-trip back to us. Order:
      1. APP_PUBLIC_URL env var — explicit and unambiguous
      2. Request's X-Forwarded-Host / Host header — derived live so we work
         in any environment without env-var babysitting
      3. Empty string — caller decides what to do; usually fine for offline mode

    Always returns without trailing slash."""
    explicit = os.environ.get("APP_PUBLIC_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    if request is not None:
        # Behind K8s ingress + CloudFront, Host carries the public hostname and
        # X-Forwarded-Proto carries the scheme. Trust them — they're set by our
        # own infra layer.
        scheme = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        if host:
            return f"{scheme}://{host}".rstrip("/")
    return ""


@api.post("/twiml/voice/{conversation_id}")
@api.get("/twiml/voice/{conversation_id}")
async def twiml_voice(conversation_id: str, request: Request):
    """Twilio fetches this when our outbound call connects. We respond with TwiML
    that opens a Media Stream WebSocket back to our backend, which then bridges
    to ElevenLabs Conv AI.

    `track="inbound_track"` is the default but we make it explicit so future
    Twilio API changes don't switch it. With `<Connect>` (vs `<Start>`) the
    stream IS bidirectional — Twilio plays back audio it receives via the
    same WebSocket — so this works correctly for the AI screening use case.
    """
    # Only Twilio may fetch this: the TwiML hands out the (signed) stream URL
    # that opens a live voice-agent session for this conversation.
    if await twilio_signature_invalid(request):
        logger.warning("twiml/voice: invalid Twilio signature for %s", conversation_id)
        raise HTTPException(status_code=403, detail="Invalid signature")
    public_base = _public_base_url(request)
    # Twilio needs wss://, not https://
    ws_base = public_base.replace("https://", "wss://").replace("http://", "ws://")
    # Twilio Media Streams drop query strings, so the short-lived signature
    # rides in the path. The websocket refuses any connection without it.
    from webhook_auth import sign_stream_token
    stream_token = sign_stream_token(conversation_id)
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{ws_base}/api/twiml/stream/{conversation_id}/{stream_token}" track="inbound_track" />
  </Connect>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@api.post("/twiml/status/{conversation_id}")
async def twiml_status(conversation_id: str, request: Request):
    """Twilio call status callback — fires on initiated / ringing / answered / completed.
    We use the 'completed' event to mark the conversation done."""
    # Twilio signs its callbacks; that signature is the only authentication on
    # this route. Without it, anyone who can read a conversation id — a demo
    # account can — could POST CallStatus=completed and close out a real call.
    if await twilio_signature_invalid(request):
        logger.warning("twiml/status: invalid Twilio signature for %s", conversation_id)
        raise HTTPException(status_code=403, detail="Invalid signature")
    form = await request.form()
    call_status = form.get("CallStatus", "")
    duration = int(form.get("CallDuration") or 0)
    conv = await db.conversations.find_one({"id": conversation_id}, {"_id": 0})
    if not conv:
        return PlainTextResponse("ok")
    update: Dict[str, Any] = {}
    if call_status == "in-progress":
        update["status"] = "in_progress"
    elif call_status == "completed":
        update["status"] = "completed" if duration > 5 else "no_answer"
        update["duration_seconds"] = duration
    elif call_status in ("failed", "busy", "no-answer", "canceled"):
        update["status"] = "no_answer"
        update["duration_seconds"] = duration
    if update:
        await db.conversations.update_one({"id": conversation_id}, {"$set": update})
    # Also nudge the candidate's screening_status when the call completes.
    # We hold off setting a final status (approved/rejected/incomplete) until AFTER
    # Claude has summarised the transcript so the verdict is in hand.
    if call_status in ("completed", "failed", "busy", "no-answer", "canceled"):
        if duration <= 5:
            await db.candidates.update_one(
                {"id": conv["candidate_id"], "user_id": conv["user_id"]},
                {"$set": {"last_call_status": "no_answer", "screening_status": "no_answer", "updated_at": now_iso()}},
            )
        else:
            await db.candidates.update_one(
                {"id": conv["candidate_id"], "user_id": conv["user_id"]},
                {"$set": {"last_call_status": "completed", "updated_at": now_iso()}},
            )
        # Schedule a Claude summary on the captured transcript
        try:
            conv2 = await db.conversations.find_one({"id": conversation_id}, {"_id": 0}) or {}
            transcript = conv2.get("transcript") or []
            if transcript:
                cand = await db.candidates.find_one({"id": conv["candidate_id"]}, {"_id": 0}) or {}
                job = None
                if cand.get("job_id"):
                    job = await db.jobs.find_one({"id": cand["job_id"]}, {"_id": 0})
                summary = await summarize_call_transcript(transcript, cand.get("first_name", ""), (job or {}).get("title", ""), duration_seconds=duration)
                await db.conversations.update_one(
                    {"id": conversation_id},
                    {"$set": {"summary": summary.get("summary", ""), "suitability_score": summary.get("suitability_score")}},
                )
                verdict = summary.get("verdict")
                if verdict:
                    cand_set: Dict[str, Any] = {"verdict": verdict, "call_summary": summary.get("summary", "")}
                    # Re-fetch candidate so we see the appointment_at potentially set by `book_slot`
                    fresh = await db.candidates.find_one({"id": conv["candidate_id"]}, {"_id": 0}) or {}
                    has_booking = bool(fresh.get("appointment_at"))
                    disq_reason = summary.get("disqualification_reason")
                    # One ladder for every post-call processor — this path used
                    # to file "weak" as incomplete_info, texting "finish your
                    # screening" to someone who had finished it.
                    from call_classification import screening_status_after_call, DISQ_LABELS
                    _status = screening_status_after_call(
                        is_complete=True, verdict=verdict, disq_reason=disq_reason,
                        has_appointment=has_booking,
                    )
                    if _status != "approved" or not has_booking:
                        cand_set["screening_status"] = _status
                    if _status == "rejected":
                        cand_set["disqualification_reason"] = DISQ_LABELS.get(disq_reason, disq_reason)
                        cand_set["archived_at"] = now_iso()
                        # Reason was never stamped here, so voice rejections
                        # archived as `None` while every other path named itself.
                        cand_set["archived_reason"] = (
                            "withdrawn" if disq_reason == "withdrawn" else "rejected")
                    await db.candidates.update_one({"id": conv["candidate_id"]}, {"$set": cand_set})
                    # Tell the gate-failed candidate the outcome — this path
                    # thanked them on the phone and then went silent forever.
                    # Guard: only when we are the first to file the rejection.
                    if (
                        _status == "rejected" and disq_reason != "withdrawn"
                        and fresh.get("screening_status") != "rejected"
                    ):
                        try:
                            from deps import send_stage_comms as _ssc_rej
                            _rej_cand = await db.candidates.find_one({"id": conv["candidate_id"]}, {"_id": 0}) or fresh
                            await _ssc_rej(user_id=conv["user_id"], candidate=_rej_cand, template_key="rejection")
                        except Exception as e:
                            logger.warning(f"twiml rejection comms failed: {e}")
                    if cand_set.get("screening_status") == "appointment_pending":
                        try:
                            slot_cand = await db.candidates.find_one({"id": conv["candidate_id"]}, {"_id": 0}) or {}
                            from routes.attendance import _fire_slot_picker_outreach
                            await _fire_slot_picker_outreach(conv["user_id"], slot_cand)
                        except Exception as e:
                            logger.warning(f"auto slot-picker outreach failed: {e}")
                    # In-app notification — surface the call outcome on the
                    # bell icon. We deliberately fire this AFTER the verdict +
                    # booking are settled so the body text reflects reality
                    # (e.g. "appointment booked" vs "needs review").
                    try:
                        from notifications_service import create_notification
                        cand_name = f"{cand.get('first_name','')} {cand.get('last_name','')}".strip() or "Candidate"
                        if disq_reason:
                            await create_notification(
                                conv["user_id"], "call.completed_negative",
                                f"❌ {cand_name} — disqualified (hard gate)",
                                body=summary.get("summary","")[:200],
                                link=f"/?candidate={conv['candidate_id']}",
                                candidate_id=conv["candidate_id"],
                                pipeline_id=cand.get("pipeline_id"),
                            )
                        elif verdict in ("strong", "good") and has_booking:
                            await create_notification(
                                conv["user_id"], "call.completed_positive",
                                f"✅ {cand_name} — passed screening, slot booked",
                                body=f"Appointment: {fresh.get('appointment_at','')[:16].replace('T',' ')}",
                                link=f"/?candidate={conv['candidate_id']}",
                                candidate_id=conv["candidate_id"],
                                pipeline_id=cand.get("pipeline_id"),
                            )
                        elif verdict in ("strong", "good") and not has_booking:
                            await create_notification(
                                conv["user_id"], "call.booking_failed",
                                f"⚠️ {cand_name} — passed screening but booking didn't go through",
                                body="The AI confirmed a slot verbally but it didn't save. Please book them manually.",
                                link=f"/?candidate={conv['candidate_id']}",
                                candidate_id=conv["candidate_id"],
                                pipeline_id=cand.get("pipeline_id"),
                            )
                        else:
                            await create_notification(
                                conv["user_id"], "call.completed_review",
                                f"📋 {cand_name} — screening complete, needs review",
                                body=summary.get("summary","")[:200],
                                link=f"/?candidate={conv['candidate_id']}",
                                candidate_id=conv["candidate_id"],
                                pipeline_id=cand.get("pipeline_id"),
                            )
                    except Exception as e:
                        logger.warning(f"call-complete notification failed: {e}")
        except Exception as e:
            logger.warning(f"twiml-status summarize failed: {e}")
        # After we've finalised the candidate's screening_status (with verdict in mind),
        # auto-fire the retry email/SMS for fixable failures.
        try:
            from deps import maybe_fire_screening_retry
            retry_res = await maybe_fire_screening_retry(conv["user_id"], conv["candidate_id"])
            logger.info(f"twiml screening retry: {retry_res}")
        except Exception as e:
            logger.warning(f"twiml retry trigger failed: {e}")
        # Also queue the next automatic set per the per-attempt retry cadence
        # (settings.auto_dialer.retry_delays, falling back to retry_delay_hours).
        # The call self-aborts if the candidate has already finished screening online.
        try:
            cand_now = await db.candidates.find_one({"id": conv["candidate_id"]}, {"_id": 0}) or {}
            if cand_now.get("screening_status") in ("no_answer", "didnt_connect", "incomplete_info"):
                from auto_dialer import maybe_schedule_incomplete_retry
                call_res = await maybe_schedule_incomplete_retry(conv["user_id"], conv["candidate_id"])
                logger.info(f"twiml incomplete retry call: {call_res}")
        except Exception as e:
            logger.warning(f"twiml incomplete retry call schedule failed: {e}")
        await broadcast(conv["user_id"])
    return PlainTextResponse("ok")


def _verify_cg1_webhook(request: Request) -> None:
    """Inbound CG1 callbacks must carry CG1_WEBHOOK_SECRET. Fails closed: with
    no secret configured these routes refuse every request."""
    import hmac
    secret = os.getenv("CG1_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(503, "CG1 callbacks disabled — set CG1_WEBHOOK_SECRET")
    sent = request.headers.get("x-webhook-secret", "") or ""
    if not hmac.compare_digest(sent.encode("utf-8"), secret.encode("utf-8")):
        raise HTTPException(403, "Invalid webhook secret")


@api.post("/webhooks/cg1/attended")
async def cg1_attended_callback(request: Request):
    """CG1 fires this when a new starter is marked as attended (Day N confirmed).
    Sets training_attended=True on the CGRecruit candidate (if candidate_id given)
    and ticks the matching ATTENDED DAY N checkbox on the NEW HIRES Google Sheet.

    Accepts two forms:
      • { candidate_id, day? }                     — CGRecruit candidate path
      • { name, email, office_key, day? }           — legacy/manual starter path
    """
    import os
    _verify_cg1_webhook(request)
    body = await request.json()
    candidate_id = (body.get("candidate_id") or "").strip()
    day = int(body.get("day") or body.get("day_number") or 1)
    # CG1 sends its own user_id so future lookups use the permanent ID (immune to name changes)
    cg1_user_id = (body.get("cg1_user_id") or "").strip() or None

    # ── Path A: CGRecruit candidate ──────────────────────────────────────────
    if candidate_id:
        update_fields: dict = {"training_attended": True, "training_attended_at": now_iso(), "updated_at": now_iso()}
        if cg1_user_id:
            update_fields["cg1_user_id"] = cg1_user_id
        res = await db.candidates.update_one(
            {"id": candidate_id},
            {"$set": update_fields},
        )
        if res.matched_count == 0:
            raise HTTPException(404, "Candidate not found")
        logger.info(f"CG1 attended callback: candidate {candidate_id} marked training_attended=True (day {day})")
        try:
            cand = await db.candidates.find_one({"id": candidate_id}, {"_id": 0, "first_name": 1, "last_name": 1, "email": 1, "pipeline_id": 1})
            if cand:
                pipe = await db.pipelines.find_one({"id": cand.get("pipeline_id")}, {"_id": 0, "cg1_office_key": 1, "name": 1, "public_slug": 1}) or {}
                office_key = company_profile.office_key_for_pipeline(pipe)
                if office_key:
                    full_name = f"{cand.get('first_name', '')} {cand.get('last_name', '')}".strip()
                    await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: __import__("sheets_service").mark_attended(
                            office_key=office_key,
                            email=cand.get("email") or "",
                            name=full_name,
                            day=day,
                        )
                    )
        except Exception as _se:
            logger.warning(f"sheets_service mark_attended (candidate path) failed (non-fatal): {_se}")
        return {"ok": True}

    # ── Path B: Manual/legacy starter — tick sheet directly ─────────────────
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip().lower()
    office_key = (body.get("office_key") or "").strip().lower()
    # Either identifier is enough — CG1 hires without a login have no email,
    # and mark_attended's NAME-column fallback handles the empty one.
    if not office_key or not (name or email):
        raise HTTPException(400, "Provide either candidate_id, or office_key plus name and/or email")
    logger.info(f"CG1 attended callback (direct): ticking sheet for {name} / {email} / {office_key} day {day}")
    try:
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: __import__("sheets_service").mark_attended(
                office_key=office_key,
                email=email,
                name=name,
                day=day,
            )
        )
    except Exception as _se:
        logger.warning(f"sheets_service mark_attended (direct path) failed (non-fatal): {_se}")
    return {"ok": True}


@api.post("/webhooks/cg1/email-updated")
async def cg1_email_updated_callback(request: Request):
    """CG1 fires this when a trainee swaps their Indeed relay address
    (…@indeedemail.com) for their real email via the in-app prompt.

    Updates the candidate card's email to match, (re)stamps the permanent
    cg1_user_id link, and overwrites the email cell on their NEW HIRES
    Google Sheet row so all three systems agree on the new address.

    Payload: { cg1_user_id, old_email, new_email, candidate_id?, name? }
    """
    import os
    _verify_cg1_webhook(request)
    body = await request.json()
    candidate_id = (body.get("candidate_id") or "").strip()
    cg1_user_id = (body.get("cg1_user_id") or "").strip()
    old_email = (body.get("old_email") or "").strip().lower()
    new_email = (body.get("new_email") or "").strip().lower()
    if not new_email or "@" not in new_email:
        raise HTTPException(400, "new_email is required")

    # Resolve the candidate: explicit id → permanent CG1 link → old email
    # (latest record wins, matching how repeat applicants are handled).
    cand = None
    if candidate_id:
        cand = await db.candidates.find_one({"id": candidate_id}, {"_id": 0})
    if not cand and cg1_user_id:
        cand = await db.candidates.find_one(
            {"cg1_user_id": cg1_user_id}, {"_id": 0}, sort=[("created_at", -1)]
        )
    if not cand and old_email:
        cand = await db.candidates.find_one(
            {"email": old_email}, {"_id": 0}, sort=[("created_at", -1)]
        )
    if not cand:
        logger.warning(f"CG1 email-updated: no candidate for id={candidate_id!r} cg1={cg1_user_id!r} email={old_email!r}")
        raise HTTPException(404, "Candidate not found")

    update_fields = {"email": new_email, "updated_at": now_iso()}
    if old_email and old_email != new_email:
        update_fields["previous_email"] = old_email
    if cg1_user_id:
        update_fields["cg1_user_id"] = cg1_user_id
    await db.candidates.update_one({"id": cand["id"]}, {"$set": update_fields})
    logger.info(f"CG1 email-updated: candidate {cand['id']} email {old_email} -> {new_email}")

    # Overwrite the email on their NEW HIRES sheet row (non-fatal).
    try:
        pipe = await db.pipelines.find_one({"id": cand.get("pipeline_id")}, {"_id": 0, "cg1_office_key": 1, "name": 1, "public_slug": 1}) or {}
        office_key = company_profile.office_key_for_pipeline(pipe)
        if office_key:
            full_name = f"{cand.get('first_name', '')} {cand.get('last_name', '')}".strip()
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: __import__("sheets_service").update_hire_email(
                    office_key=office_key,
                    old_email=old_email or (cand.get("email") or ""),
                    name=full_name,
                    new_email=new_email,
                )
            )
    except Exception as _se:
        logger.warning(f"sheets_service update_hire_email failed (non-fatal): {_se}")
    return {"ok": True, "candidate_id": cand["id"]}


@app.websocket("/api/twiml/stream/{conversation_id}/{stream_token}")
async def twiml_stream_ws(websocket: WebSocket, conversation_id: str, stream_token: str):
    """Twilio Media Streams WebSocket. We bridge audio to ElevenLabs Conv AI.

    The path carries a signed, expiring token minted by /twiml/voice (which
    only answers Twilio-signed requests). Without it, anyone who learned a
    conversation id could open a voice-agent session, write its transcript
    and flip the call's status."""
    from webhook_auth import stream_token_valid
    if not stream_token_valid(conversation_id, stream_token):
        logger.warning(f"[bridge] refused stream for {conversation_id}: bad or expired token")
        await websocket.close(code=4403)
        return
    await websocket.accept()
    logger.info(f"[bridge] Twilio WS connected conversation_id={conversation_id}")

    conv = await db.conversations.find_one({"id": conversation_id}, {"_id": 0})
    if not conv:
        await websocket.close(code=4404)
        logger.warning(f"[bridge] no conversation {conversation_id}")
        return
    agent_id = conv.get("agent_id") or ""
    dyn = conv.get("dynamic_variables") or {}
    if not agent_id:
        await websocket.close(code=4400)
        logger.warning(f"[bridge] conversation {conversation_id} missing agent_id")
        return

    from twiml_bridge import open_elevenlabs_ws, bridge_streams

    # Append a transcript row to the DB as turns come in
    async def on_turn(role: str, text: str):
        await db.conversations.update_one(
            {"id": conversation_id},
            {"$push": {"transcript": {"role": role, "text": text}}},
        )

    # Persist the ElevenLabs conversation_id as soon as the WS init metadata arrives so
    # the audio playback endpoint + post-call sync can reference it later.
    async def on_metadata(eleven_conv_id: str):
        await db.conversations.update_one(
            {"id": conversation_id},
            {"$set": {"elevenlabs_conversation_id": eleven_conv_id}},
        )

    eleven_ws = None
    try:
        eleven_ws = await open_elevenlabs_ws(agent_id=agent_id, dynamic_variables=dyn)
        await db.conversations.update_one(
            {"id": conversation_id},
            {"$set": {"status": "in_progress"}},
        )
        await bridge_streams(websocket, eleven_ws, on_transcript_turn=on_turn, on_metadata=on_metadata)
    except WebSocketDisconnect:
        logger.info(f"[bridge] Twilio disconnected conversation_id={conversation_id}")
    except Exception as e:
        logger.exception(f"[bridge] error: {e}")
    finally:
        if eleven_ws:
            try: await eleven_ws.close()
            except Exception: pass
        try: await websocket.close()
        except Exception: pass
        # The bridge is the last writer for a call ElevenLabs never identified:
        # with no elevenlabs_conversation_id the post-call webhook can't match the
        # row and the auto-sync sweep filters it out, so it would sit in an active
        # status forever — an unresolvable ghost holding the candidate at
        # in_progress. Twilio's status callback still owns the normal ending; this
        # only takes effect when the callback never arrives, hence the guard.
        try:
            from dialer.live_count import ACTIVE_CONV_STATUSES
            _fresh = await db.conversations.find_one(
                {"id": conversation_id}, {"_id": 0, "transcript": 1},
            ) or {}
            await db.conversations.update_one(
                {
                    "id": conversation_id,
                    "status": {"$in": ACTIVE_CONV_STATUSES},
                    "elevenlabs_conversation_id": None,
                },
                # A transcript means the AI did speak to them, so the honest
                # terminal state is a call with no verdict, not a missed one.
                {"$set": {"status": "completed" if (_fresh.get("transcript") or []) else "no_answer"}},
            )
        except Exception as _te:
            logger.warning(f"[bridge] terminal status write failed for {conversation_id}: {_te}")
        logger.info(f"[bridge] closed conversation_id={conversation_id}")


# ===== Mount =====
# Modular routers (see /app/backend/routes/ — extracted from server.py for clarity).
# Each sub-router defines paths relative to /api (because they are mounted on `api`
# which has prefix='/api').
from routes.public import router as public_router
from routes.seed import router as seed_router
from routes.email_intake import router as email_intake_router
from routes.webhooks import router as webhooks_router
from routes.admin_users import router as admin_users_router
from routes.retry import router as retry_router
from routes.revival import router as revival_router
from routes.attendance import router as attendance_router
from routes.notifications import router as notifications_router
from routes.needs_attention import router as needs_attention_router
from routes.widget import router as widget_router
from routes.internal import router as internal_router
from routes.integrations import router as integrations_router
from routes.partner_feed import router as partner_feed_router
from routes.analyst_users import router as analyst_users_router
from routes.ai_insights import router as ai_insights_router
from routes.sms_inbox import router as sms_inbox_router
from routes.inbound import router as inbound_router

api.include_router(public_router)
api.include_router(seed_router)
api.include_router(email_intake_router)
api.include_router(webhooks_router)
api.include_router(admin_users_router)
api.include_router(retry_router)
api.include_router(revival_router)
api.include_router(attendance_router)
api.include_router(notifications_router)
api.include_router(needs_attention_router)
api.include_router(widget_router)
api.include_router(internal_router)
api.include_router(integrations_router)
api.include_router(partner_feed_router)
api.include_router(analyst_users_router)
api.include_router(ai_insights_router)
api.include_router(training_sms_router)
api.include_router(email_replies_router)
api.include_router(sms_inbox_router)
api.include_router(inbound_router)


@api.post("/admin/reschedule-stuck-retries")
async def reschedule_stuck_retries(
    min_hours_out: float = Query(8.0),
    dry_run: bool = Query(False),
    user: dict = Depends(current_super_admin),
):
    """One-time cleanup after the per-attempt retry-cadence fix.

    Candidates whose next set was scheduled under the OLD flat retry_delay_hours
    (often pushed ~20h out / into the next call window) are re-queued using the
    corrected per-attempt `retry_delays` cadence. The idempotency guard in
    `maybe_schedule_incomplete_retry` skips future-dated candidates, so we must
    clear `next_call_at` + cancel the stale APScheduler job first, then reschedule.

    Only touches no_answer/incomplete_info candidates in SCREENING whose current
    next_call_at is more than `min_hours_out` hours away — anything already inside
    the correct cadence is left alone, so this is safe to re-run. Pass dry_run=true
    to preview which candidates would be moved without changing anything.
    """
    import pytz
    from dialer.retry import cancel_pending_call_jobs, maybe_schedule_incomplete_retry

    def _to_utc(s: str):
        """Parse next_call_at whether it's ET-naive ("...T09:00:00") or an
        offset-aware UTC ISO string (the DND path stores the latter)."""
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
        if dt.tzinfo is None:
            dt = pytz.timezone(default_tz_name()).localize(dt)
        return dt.astimezone(timezone.utc)

    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=min_hours_out)
    candidates = await db.candidates.find(
        {
            "screening_status": {"$in": ["no_answer", "incomplete_info"]},
            "stage": "SCREENING",
            "next_call_at": {"$ne": None},
            "archived_at": None,
            "appointment_at": None,
        },
        {"_id": 0, "id": 1, "user_id": 1, "next_call_at": 1, "call_attempts": 1},
    ).to_list(2000)

    moved, previewed, skipped = [], [], 0
    for c in candidates:
        target = _to_utc(c.get("next_call_at") or "")
        if target is None or target <= cutoff:
            skipped += 1
            continue
        entry = {
            "candidate_id": c["id"],
            "was_scheduled_at": c.get("next_call_at"),
            "attempts": c.get("call_attempts"),
        }
        if dry_run:
            previewed.append(entry)
            continue
        cancel_pending_call_jobs(c["id"])
        await db.candidates.update_one(
            {"id": c["id"], "user_id": c["user_id"]},
            {"$set": {"next_call_at": None, "updated_at": now_iso()}},
        )
        res = await maybe_schedule_incomplete_retry(c["user_id"], c["id"])
        entry["result"] = res.get("status")
        entry["now_scheduled_at"] = res.get("scheduled_at")
        moved.append(entry)

    if dry_run:
        return {"status": "dry_run", "would_move": len(previewed), "left_alone": skipped, "candidates": previewed}
    return {"status": "done", "rescheduled": len(moved), "left_alone": skipped, "candidates": moved}


app.include_router(api)

# Claude connector: a read-only MCP server at /mcp with its own OAuth sign-in
# (/oauth/*, /.well-known/oauth-*). Its tokens are opaque, not app JWTs, so they
# can't reach any /api route. Set CLAUDE_CONNECTOR=off in Railway to switch it
# off. See claude_connector/core.py.
if os.environ.get("CLAUDE_CONNECTOR", "on").strip().lower() not in ("off", "0", "false", "no"):
    try:
        from claude_connector.core import Connector  # noqa: E402
        from claude_connector.cgrecruit import CGRecruitAdapter  # noqa: E402
        _connector_url = company_profile.public_app_url()
        if not _connector_url:
            # OAuth issuer/redirect URLs must be absolute, so the connector
            # needs to know where it is served.
            logger.info("Claude connector off: set APP_PUBLIC_URL to enable it")
        else:
            Connector(
                db=db,
                public_url=_connector_url,
                adapter=CGRecruitAdapter(db),
            ).install(app)
    except Exception:  # an add-on must never stop the ATS from booting
        logger.exception("Claude connector failed to install; continuing without it")

# CORS — `allow_credentials=True` requires explicit origins (browsers reject
# the wildcard combo when cookies are involved). The list is derived from
# `APP_PUBLIC_URL` (auto-set per environment) plus optional `CORS_ORIGINS`
# extras, so the same code works in preview AND production without changes.
#
# Critical: do NOT hardcode the preview URL here — the production deploy will
# have a different host (e.g. recruit.example.com), and a stale hardcoded
# preview origin would silently break the cookie-auth flow with cryptic 401s.
_app_public_url = os.environ.get("APP_PUBLIC_URL", "").strip().rstrip("/")
# The local dev server's origin is only trusted when this isn't a public
# https deployment (or when CORS_ALLOW_LOCALHOST=true says so explicitly).
_default_origins = []
if (not _app_public_url.startswith("https://")
        or os.environ.get("CORS_ALLOW_LOCALHOST", "").strip().lower() in ("1", "true", "yes")):
    _default_origins.append("http://localhost:3000")
if _app_public_url:
    _default_origins.append(_app_public_url)
_extra_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",")
                  if o.strip() and o.strip() != "*"]
# De-dupe while preserving insertion order (sets reorder).
_seen: set = set()
_cors_origins = [o for o in (_default_origins + _extra_origins) if not (o in _seen or _seen.add(o))]
logger.info(f"CORS allowed origins: {_cors_origins}")

# Demo/trial accounts: refuse every mutation from an is_demo user in one
# blanket rule (see demo_guard.py). Registered BEFORE CORSMiddleware —
# Starlette wraps in reverse order, so CORS (added last → outermost) still
# stamps its headers on the guard's 403 for cross-origin dev clients.
from demo_guard import DemoWriteGuard  # noqa: E402
app.add_middleware(DemoWriteGuard)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Note: webhook routes set their own CORS/response headers inline.
# BaseHTTPMiddleware was removed because it wraps HTMLResponse bodies in a
# streaming iterator that can be lost before reaching the browser, causing blank pages.


@app.on_event("startup")
async def startup_event():
    # Refuse to start with a missing/short JWT_SECRET or a non-HMAC
    # JWT_ALGORITHM: every session cookie and the call-bridge stream URLs are
    # signed with it. Raising here aborts uvicorn's startup.
    from auth_service import _jwt_settings as _check_jwt_settings
    _check_jwt_settings()
    start_scheduler(db)
    logger.info("CGRecruit started — auto-dialer scheduler online")
    # Training confirmation SMS scheduler — fires every 5 min
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        _sms_scheduler = AsyncIOScheduler(timezone=default_tz_name())
        async def _sms_tick():
            try:
                await training_sms_tick(db)
            except Exception as ex:
                logger.error("training_sms tick failed: %s", ex)
        _sms_scheduler.add_job(_sms_tick, "interval", minutes=5, id="training_sms_tick", replace_existing=True)
        async def _screening_retry_sweep():
            """Find candidates stuck in no_answer/didnt_connect/incomplete_info who never
            received a screening retry email — fires when the twiml webhook missed it."""
            try:
                from deps import maybe_fire_screening_retry
                cur = db.candidates.find({
                    "screening_status": {"$in": ["no_answer", "didnt_connect", "incomplete_info"]},
                    # `$exists: False` was the bug: Candidate declares
                    # screening_retry_sent_at with a default of None, and
                    # model_dump() writes it as an explicit null on every insert,
                    # so the field always exists and this sweep matched nothing:
                    # the safety net could never fire. `$in: [None]` matches both an explicit null and
                    # a genuinely absent field, so old and new docs both qualify.
                    "screening_retry_sent_at": {"$in": [None]},
                    "screening_attempts": {"$not": {"$gte": 3}},
                    # Email OR phone. This used to require an email, which quietly
                    # excluded every phone-only candidate from the one safety net
                    # that exists for them — and the SMS is the half that actually
                    # gets read. maybe_fire_screening_retry skips each channel on
                    # its own if that channel has nothing to send to.
                    "$or": [
                        {"email": {"$nin": [None, ""]}},
                        {"phone": {"$nin": [None, ""]}},
                    ],
                })
                async for cand in cur:
                    try:
                        await maybe_fire_screening_retry(cand["user_id"], cand["id"])
                    except Exception as _e:
                        logger.warning("screening retry sweep failed for %s: %s", cand.get("id"), _e)
            except Exception as ex:
                logger.error("screening_retry_sweep failed: %s", ex)

        _sms_scheduler.add_job(_screening_retry_sweep, "interval", minutes=30, id="screening_retry_sweep", replace_existing=True)

        async def _stranded_pending_sweep():
            """Re-start candidates who never made it into the dialler at all.

            `screening_status="pending"` is the value every candidate is created
            with, and it used to be a dead end: if scheduling threw, the six intake
            paths all swallowed it into a log line, and no other sweep queries
            `pending` — startup recovery looks at queued/no_answer/incomplete_info,
            and the archive sweeps require call_attempts >= 1. So the candidate was
            never called, never texted again, and never archived.

            Deliberately narrow. Seven days, because a month-old application that
            was never contacted should be a human's decision, not a surprise call
            from a robot. Twenty-five per run, so a backlog drains visibly rather
            than becoming a dial storm.
            """
            try:
                from screening_start import start_screening
                from deps import resolve_settings
                cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
                stranded = await db.candidates.find({
                    "screening_status": "pending",
                    "archived_at": None,
                    "next_call_at": None,
                    "auto_dial": True,
                    "phone": {"$nin": [None, ""]},
                    "created_at": {"$gte": cutoff},
                }, {"_id": 0}).to_list(25)
                for cand in stranded:
                    try:
                        st = await resolve_settings(cand["user_id"], cand.get("pipeline_id"))
                        # Comms already went out at intake; only the call is missing.
                        res = await start_screening(cand["user_id"], cand, st, send_comms=False)
                        logger.info(
                            "stranded-pending sweep: restarted %s (%s)",
                            cand.get("id"), res.get("no_dial_reason") or "queued",
                        )
                    except Exception as _e:
                        logger.warning("stranded-pending restart failed for %s: %s", cand.get("id"), _e)
                if stranded:
                    logger.info("stranded-pending sweep: handled %s candidate(s)", len(stranded))
            except Exception as ex:
                logger.error("stranded_pending_sweep failed: %s", ex)

        _sms_scheduler.add_job(_stranded_pending_sweep, "interval", minutes=30, id="stranded_pending_sweep", replace_existing=True)

        async def _outcome_due_nudge():
            """Ask, while the answer is still fresh, whether people turned up.

            There was already a nudge for this, but it fired at 48 hours, from
            inside the loop that archives the candidate, and only *after* that
            loop had set archived_at — and the board it links to hides archived
            candidates by default. So the one prompt in the system pointed at a
            screen that filtered out the thing it was asking about.

            This is the early one: two hours after the slot, before anything has
            been archived, once per candidate. Grouped into a single notification
            per session so a room of twelve is one prompt, not twelve.
            """
            try:
                from notifications_service import create_notification
                cutoff_recent = (datetime.now(timezone.utc) - timedelta(hours=36)).isoformat()
                rows = await db.candidates.find({
                    "appointment_at": {"$ne": None, "$gte": cutoff_recent},
                    "attendance_status": None,
                    "archived_at": None,
                    "attendance_nudge_sent_at": {"$in": [None]},
                }, {"_id": 0, "id": 1, "user_id": 1, "pipeline_id": 1,
                    "appointment_at": 1, "appointment_link": 1}).to_list(500)
                lapsed = [r for r in rows
                          if appointment_has_lapsed(r.get("appointment_at"), grace_minutes=120)]
                if not lapsed:
                    return
                groups: Dict[tuple, list] = {}
                for r in lapsed:
                    groups.setdefault(
                        (r["user_id"], str(r.get("appointment_at")), str(r.get("appointment_link") or "")),
                        [],
                    ).append(r)
                for (user_id, appt_at, _link), members in groups.items():
                    # In the timezone the session was actually held in — see
                    # models.local_slot_label. The pipeline's own timezone wins
                    # over the account default, the way the calendar reads it.
                    pipe_id = members[0].get("pipeline_id")
                    try:
                        _s = await resolve_settings(user_id, pipe_id) or {}
                        _p = await db.pipelines.find_one({"id": pipe_id}, {"_id": 0, "timezone": 1}) or {}
                        tz_name = (_p.get("timezone")
                                   or (_s.get("region_language") or {}).get("timezone")
                                   or default_tz_name())
                    except Exception:
                        tz_name = default_tz_name()
                    label = local_slot_label(appt_at, tz_name) or str(appt_at)
                    try:
                        await create_notification(
                            user_id, "appointment.outcome_due",
                            f"❓ {len(members)} unmarked from the {label} interview",
                            body="The session has finished and nobody has said who turned up. "
                                 "Mark them off while it's fresh.",
                            link="/calendar?outcome_due=1",
                            pipeline_id=members[0].get("pipeline_id"),
                        )
                    except Exception as e:
                        logger.warning("outcome-due notification failed for %s: %s", appt_at, e)
                    await db.candidates.update_many(
                        {"id": {"$in": [m["id"] for m in members]}},
                        {"$set": {"attendance_nudge_sent_at": now_iso()}},
                    )
                logger.info("outcome-due nudge: %s session(s), %s candidate(s)",
                            len(groups), len(lapsed))
            except Exception as ex:
                logger.error("outcome_due_nudge failed: %s", ex)

        _sms_scheduler.add_job(_outcome_due_nudge, "interval", minutes=60, id="outcome_due_nudge", replace_existing=True)

        _sms_scheduler.start()
        app.state.training_sms_scheduler = _sms_scheduler
        logger.info("Training SMS confirmation scheduler started (every 5min, lead 3h)")
    except Exception as ex:
        logger.error("Failed to start training SMS scheduler: %s", ex)
    try:
        await db.candidates.create_index([("public_token", 1)], unique=True, sparse=True)
        await db.candidates.create_index([("pipeline_id", 1), ("stage", 1)])
        await db.candidates.create_index([("pipeline_id", 1), ("email", 1)])
        await db.candidates.create_index([("pipeline_id", 1), ("appointment_at", 1)])
        await db.candidates.create_index([("user_id", 1), ("stage", 1)])
        await db.candidates.create_index([("phone", 1)])
        await db.pipelines.create_index([("public_slug", 1)], unique=True, sparse=True)
        await db.pipelines.create_index([("user_id", 1)])
        await db.conversations.create_index([("elevenlabs_conversation_id", 1)], sparse=True)
        await db.conversations.create_index([("candidate_id", 1)])
        # dialer.live_count.count_tenant_live runs on EVERY dial and on every
        # Call Queue poll, filtering conversations by (user_id, status, created_at).
        # Without this it collection-scans the whole conversations history — which
        # only grows — and a batch of dials is enough to take the process down.
        await db.conversations.create_index([("user_id", 1), ("status", 1), ("created_at", 1)])
        await db.users.create_index([("email", 1)], unique=True, sparse=True)
        # SMS screening reads these on every inbound message (thread history,
        # inbound count, last-outbound check) and on every transcript open. They
        # had no indexes, so each was a full collection scan — the queries most
        # in the Twilio 15s webhook window, and the ones that grow fastest.
        await db.training_sms_messages.create_index([("candidate_id", 1), ("timestamp", 1)])
        await db.training_sms_messages.create_index([("message_sid", 1)], sparse=True)
        await db.communications.create_index([("candidate_id", 1), ("type", 1), ("status", 1)])
        # The new sweeps and metrics lead on these fields, none of which the
        # existing pipeline_id-prefixed indexes can serve.
        await db.candidates.create_index([("screening_status", 1), ("updated_at", 1)])
        await db.candidates.create_index([("retry_chat_last_at", 1)], sparse=True)
        await db.candidates.create_index([("attendance_status", 1), ("appointment_at", 1)])
        await db.candidates.create_index([("first_outreach_at", 1)], sparse=True)
        # dialer.live_count.count_pipeline_live's $or arm — the pipeline-scoped
        # half of the concurrency cap, also on the per-dial hot path.
        await db.candidates.create_index([("revival_last_attempt_at", 1)], sparse=True)
        logger.info("MongoDB indexes ensured")
    except Exception as e:
        logger.warning(f"Index creation failed (non-fatal): {e}")
    # Re-arm reminder jobs for any existing future appointments — APScheduler
    # uses an in-memory store, so jobs are lost across restarts. Idempotent
    # because schedule_appointment_reminders uses replace_existing=True.
    try:
        from auto_dialer import schedule_appointment_reminders, schedule_rebook_watchlist_sweep
        from datetime import datetime, timezone
        cutoff = datetime.now(timezone.utc).isoformat()
        upcoming = await db.candidates.find(
            {
                "appointment_at": {"$gt": cutoff},
                # Only re-arm reminders for candidates ACTUALLY in the
                # appointment funnel — if they were moved back to SCREENING
                # (or to CLOSE), the appointment_at left behind is stale and
                # we should not be pinging them about a meeting they're no
                # longer attending.
                "stage": {"$in": ["APPOINTMENT", "FORM", "TRAINING"]},
            },
            {"_id": 0, "id": 1, "user_id": 1, "appointment_at": 1, "attendance_status": 1},
        ).to_list(2000)
        rearmed = 0
        for c in upcoming:
            if c.get("attendance_status") in ("attended_form", "attended_no_form", "no_show"):
                continue
            try:
                await schedule_appointment_reminders(c["user_id"], c["id"], c["appointment_at"])
                rearmed += 1
            except Exception as e:
                logger.warning(f"re-arm reminder for {c.get('id')} failed: {e}")
        logger.info(f"re-armed {rearmed} appointment reminder set(s) on startup")
        # Form-completion reminders are in-memory too, and had NO re-arm: any
        # deploy inside the 2-hour window silently ate them (a deploy-heavy
        # week ate a whole cohort's). form_reminder_at is stamped at schedule
        # time and cleared on send/cancel, so it is exactly the set of
        # reminders a restart orphaned. Past-due ones fire shortly after
        # boot, staggered; future ones keep their original due time.
        try:
            from auto_dialer import schedule_form_reminder
            from models import parse_appointment_at as _parse_ts
            _form_orphans = await db.candidates.find(
                {"stage": "FORM", "form_submitted_at": None,
                 "form_reminder_at": {"$ne": None}},
                {"_id": 0, "id": 1, "user_id": 1, "form_reminder_at": 1},
            ).to_list(500)
            _now_dt = datetime.now(timezone.utc)
            _fr = 0
            for _i, c in enumerate(_form_orphans):
                due = _parse_ts(c.get("form_reminder_at"))
                mins = max(2 + _i, int((due - _now_dt).total_seconds() / 60)) if due else 2 + _i
                await schedule_form_reminder(c["user_id"], c["id"], delay_minutes=mins)
                _fr += 1
            if _fr:
                logger.info(f"re-armed {_fr} form reminder(s) on startup")
        except Exception as e:
            logger.warning(f"form-reminder re-arm failed: {e}")
        # Hourly rebook-watchlist sweep — re-emails candidates who clicked
        # "None of these work" on the reschedule portal once 3 days have passed.
        schedule_rebook_watchlist_sweep()
        # Hourly booking-retry sweep — texts the booking page to candidates who
        # were parked because the booking window held nothing bookable, once it
        # has rolled far enough forward to contain a slot.
        from auto_dialer import schedule_booking_retry_sweep
        schedule_booking_retry_sweep()
        # Hourly unbooked-screened sweep — sends the slot-picker link to
        # candidates who completed screening but never locked in a time
        # (first run 2 min after startup so a deploy drains the backlog).
        from auto_dialer import schedule_unbooked_screened_sweep
        schedule_unbooked_screened_sweep()
        # Hourly training-lapsed sweep — archives TRAINING candidates 4+ days
        # past their training date: they either attended (now in CG1, outside
        # recruitment) or never rebooked. Getting back in touch un-archives
        # them at their old stage with a fresh 4-day hold.
        from auto_dialer import schedule_training_lapsed_sweep
        schedule_training_lapsed_sweep()
        # Hourly abandoned-retry-chat nudge sweep — nudges candidates who
        # started the /retry/{token} text screening but went quiet mid-way
        # (first run 2 min after startup so a deploy drains the backlog).
        from auto_dialer import schedule_retry_chat_nudge_sweep
        schedule_retry_chat_nudge_sweep()
        # Hourly gate-check sweep — texts the four hard gates to anyone booked
        # with zero screening material, so the bell flag has a mechanism behind
        # it and a hiring manager is never the first to ask the gates.
        from auto_dialer import schedule_gate_check_sweep
        schedule_gate_check_sweep()
        # 20-min session-unconfirmed alarm — a session 30-120 min out where
        # nobody replied Y to the confirm chaser rings the bell, so an empty
        # room is a decision someone made, never a surprise the interviewer
        # discovers alone.
        from auto_dialer import schedule_session_confirm_sweep
        schedule_session_confirm_sweep()
        # (The 15-min unconfirmed-starter alarm was retired 2026-08-18: CG1
        # admins only want genuine trainee questions/correspondence, never
        # status flags — see cg1_alerts.py.)
        # 15-min late transcript recovery — the auto-sync sweep finds work by
        # status, so a call that reached a terminal status with an empty
        # transcript was invisible to it forever. This one is blind to status:
        # no screening stays lost just because something concluded it early.
        from auto_dialer import schedule_late_transcript_recovery
        schedule_late_transcript_recovery()
    except Exception as e:
        logger.warning(f"startup reminder re-arming failed: {e}")
    # Backfill permanent milestone timestamps for candidates created before this
    # field existed. Uses the best available approximation of the true date.
    try:
        r1 = await db.candidates.update_many(
            {"stage": "TRAINING", "moved_to_training_at": None},
            [{"$set": {"moved_to_training_at": {"$ifNull": ["$hired_at", "$updated_at"]}}}],
        )
        r2 = await db.candidates.update_many(
            {
                "$or": [{"stage": "CLOSE"}, {"form_submitted_at": {"$ne": None}}],
                "moved_to_close_at": None,
            },
            [{"$set": {"moved_to_close_at": {"$ifNull": ["$form_submitted_at", "$updated_at"]}}}],
        )
        r3 = await db.candidates.update_many(
            {
                "$or": [
                    {"stage": {"$in": ["FORM", "CLOSE", "TRAINING"]}},
                    {"form_submitted_at": {"$ne": None}},
                ],
                "moved_to_form_at": None,
            },
            [{"$set": {"moved_to_form_at": {"$ifNull": ["$form_submitted_at", "$updated_at"]}}}],
        )
        if r1.modified_count or r2.modified_count or r3.modified_count:
            logger.info(
                f"Backfilled milestone timestamps: {r1.modified_count} training, "
                f"{r2.modified_count} close, {r3.modified_count} form"
            )
    except Exception as e:
        logger.warning(f"Milestone timestamp backfill failed (non-fatal): {e}")
    # Migrate old wide-window availability rules to individual single-slot rules.
    # Idempotent — already-migrated rules are left unchanged.
    try:
        from availability_service import migrate_rules_to_single_slots
        migrated = 0
        all_pipes = await db.pipelines.find({}, {"_id": 0}).to_list(1000)
        for pipe in all_pipes:
            new_rules = migrate_rules_to_single_slots(pipe)
            if new_rules is not pipe.get("availability_rules"):
                await db.pipelines.update_one(
                    {"id": pipe["id"]},
                    {"$set": {"availability_rules": new_rules}},
                )
                migrated += 1
        if migrated:
            logger.info(f"Migrated {migrated} pipeline(s) from wide-window to single-slot rules")
    except Exception as e:
        logger.warning(f"Rule migration failed (non-fatal): {e}")

    # One-time (2026-10): move voice agents still on the old default models to
    # the faster ones (see voice_service.DEFAULT_VOICE_LLM / DEFAULT_TTS_MODEL).
    # Only rewrites values that are still the OLD defaults (or the deprecated
    # gemini-2.5-flash), so a recruiter's deliberate choice is left alone, and
    # a marker makes it run once — picking the old model again later sticks.
    # Runs before the startup sync below, which then pushes it to ElevenLabs.
    try:
        from voice_service import DEFAULT_VOICE_LLM, DEFAULT_TTS_MODEL
        _mid = "voice_models_2026_10"
        if not await db.schema_migrations.find_one({"_id": _mid}):
            r_llm = await db.settings.update_many(
                {"screen_call_agent.llm_model": {"$in": ["gemini-3-flash-preview", "gemini-2.5-flash", None, ""]}},
                {"$set": {"screen_call_agent.llm_model": DEFAULT_VOICE_LLM}},
            )
            r_tts = await db.settings.update_many(
                {"screen_call_agent.voice_model": {"$in": ["eleven_turbo_v2", None, ""]}},
                {"$set": {"screen_call_agent.voice_model": DEFAULT_TTS_MODEL}},
            )
            await db.schema_migrations.insert_one({"_id": _mid, "at": datetime.now(timezone.utc).isoformat(),
                                                   "llm_updated": r_llm.modified_count, "tts_updated": r_tts.modified_count})
            logger.info(f"voice model migration: {r_llm.modified_count} LLM, {r_tts.modified_count} TTS setting(s) upgraded")
    except Exception as e:
        logger.warning(f"voice model migration failed (non-fatal): {e}")

    # Auto-sync all ElevenLabs agents on startup so prompt code changes
    # (voice_service.py) take effect immediately without a manual Save.
    try:
        from voice_service import upsert_elevenlabs_tools
        _startup_public_base = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
        try:
            _tool_map = upsert_elevenlabs_tools(_startup_public_base)
            startup_tool_ids = [tid for tid in _tool_map.values() if tid]
        except Exception as _te:
            logger.warning(f"startup tool upsert failed (agents will sync without tools): {_te}")
            startup_tool_ids = []
        users = await db.users.find({}, {"_id": 0, "id": 1}).to_list(200)
        synced = 0
        for u in users:
            uid = u.get("id")
            if not uid:
                continue
            try:
                pipelines = await db.pipelines.find({"user_id": uid}, {"_id": 0}).to_list(50)
                for pipe in pipelines:
                    try:
                        s = await resolve_settings(uid, pipe["id"])
                        sca = dict(s.get("screen_call_agent", {}) or {})
                        agent_id = pipe.get("elevenlabs_agent_id_override") or sca.get("elevenlabs_agent_id")
                        if not agent_id:
                            continue
                        bp = s.get("booking_preferences", {}) or {}
                        # Apply pipeline-level field overrides (same logic as _autosync_pipeline_agent)
                        if pipe.get("agent_name_override"):
                            sca["agent_name"] = pipe["agent_name_override"]
                        if pipe.get("first_message_override"):
                            sca["opening_message"] = pipe["first_message_override"]
                        if pipe.get("additional_context_override"):
                            existing = sca.get("additional_context", "")
                            sca["additional_context"] = (
                                f"{existing}\n\n# Location-Specific Context ({pipe.get('name', '')})\n{pipe['additional_context_override']}"
                            ).strip()
                        voice_id = pipe.get("voice_id_override") or sca.get("voice_id") or None
                        sync_agent_to_elevenlabs(agent_id, sca, voice_id=voice_id, tool_ids=startup_tool_ids, booking_prefs=bp)
                        synced += 1
                    except Exception as pe:
                        logger.warning(f"startup agent sync failed for pipeline {pipe.get('id')}: {pe}")
            except Exception as ue:
                logger.warning(f"startup agent sync failed for user {uid}: {ue}")
        logger.info(f"startup ElevenLabs agent sync complete — {synced} agent(s) updated")
    except Exception as e:
        logger.warning(f"startup ElevenLabs agent sync failed: {e}")


@app.on_event("shutdown")
async def shutdown_db_client():
    stop_scheduler()
    sms_sched = getattr(app.state, "training_sms_scheduler", None)
    if sms_sched:
        sms_sched.shutdown(wait=False)
    client.close()


# ── Crawlers ─────────────────────────────────────────────────────────────────
# CGRecruit is internal tooling plus candidate links, not a website. It served
# no robots.txt: the SPA catch-all answered /robots.txt with index.html, and a
# robots.txt that comes back as HTML is read as "no robots.txt, crawl freely".
# Googlebot rendered the shell, followed its /api/auth/me call, got a 401, and
# Search Console filed it under "Blocked due to unauthorized request (401)".
#
# Indexing matters more here than the 401 did: /apply/:slug, /refer/:slug and
# the /applicant, /retry, /reschedule token links are handed to candidates
# directly and carry a token in the URL. None of them belong in a search index.
# robots.txt stops the crawl; X-Robots-Tag stops indexing of anything already
# discovered. Declared above the catch-all so it wins — FastAPI matches in
# registration order and /{full_path:path} takes every path after it.
@app.middleware("http")
async def no_index(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


# Serve the React frontend — must be mounted after all API routes so /api/...
# requests are handled by FastAPI first and not swallowed by the static handler.
import pathlib as _pl
from fastapi.responses import FileResponse as _FileResponse
_frontend_build = _pl.Path(__file__).parent / "static"
logger.info(f"Frontend build path: {_frontend_build} (exists={_frontend_build.is_dir()})")
if _frontend_build.is_dir():
    # The hashed-assets dir is mounted only if it exists. It once didn't — a
    # commit shipped index.html without its bundles, git kept no empty dir,
    # and StaticFiles raised at startup: the whole service (API, webhooks,
    # SMS screening) crash-looped over missing CSS. A broken frontend must
    # degrade to a blank page, never take the backend down with it.
    _assets_dir = _frontend_build / "static"
    if _assets_dir.is_dir():
        from fastapi.staticfiles import StaticFiles as _StaticFiles
        app.mount("/static", _StaticFiles(directory=str(_assets_dir)), name="assets")
    else:
        logger.error(f"Frontend asset dir missing ({_assets_dir}) — API up, UI degraded")

    # no-cache on the shell (index.html, sw.js, manifest): these carried NO
    # Cache-Control at all, leaving stale-bundle behavior to each browser's
    # heuristics. The hashed bundles under /static stay cacheable — a fresh
    # index.html always points at the right hashes.
    _NO_CACHE = {"Cache-Control": "no-cache"}

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _serve_spa(full_path: str):
        # Resolve before trusting: today the ASGI layer collapses ".." before
        # the path reaches here, so "/..%2f.env" already falls through to the
        # shell. That is the only thing standing between this handler and
        # backend/.env — MONGO_URL, JWT_SECRET, the Twilio keys — so don't
        # leave it resting on a server-level detail that could change.
        try:
            file = (_frontend_build / full_path).resolve()
            inside = file.is_relative_to(_frontend_build.resolve())
        except (OSError, ValueError):
            inside = False
        if inside and file.is_file():
            return _FileResponse(str(file), headers=_NO_CACHE)
        return _FileResponse(str(_frontend_build / "index.html"), headers=_NO_CACHE)

    logger.info("Frontend static files mounted at /")
else:
    logger.warning(f"Frontend build not found at {_frontend_build} — serving API only")
