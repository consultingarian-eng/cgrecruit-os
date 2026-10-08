"""SendGrid Inbound Parse: receive resume emails → auto-create candidates.

Configure: SendGrid → Settings → Inbound Parse → Host = inbox.<your-domain>,
Destination = <APP_PUBLIC_URL>/api/webhooks/sendgrid/inbound.
"""
import asyncio
import os
import company_profile
import re
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, Any
from fastapi import APIRouter, Request, Body, Depends, HTTPException

from deps import (
    db, logger, current_user, current_super_admin, get_or_create_settings, send_stage_email,
    require_mover, assert_pipeline_access, assert_pipeline_in_tenant,
)
from models import Candidate, new_id, now_iso
from ai_service import extract_resume_text, parse_resume, smart_score
from auto_dialer import schedule_call_with_window

router = APIRouter()

# Serialize inbound resume AI processing so bulk email drops (100 resumes at once)
# don't concurrently exhaust Claude API / crash the server. Each resume waits for the
# previous one to finish parsing + a 25-second cooldown before the next starts.
_inbound_parse_lock = asyncio.Semaphore(1)

# Bodies are truncated before they go in email_intake_log. 8000 chars was enough for
# plain resume-forward emails, but a templated job-board notification (Indeed and the
# like) spends most of its first 8000 chars on layout markup — the links we need to
# read sit well past that. Mongo's ceiling is 16MB/doc, so this is still tiny.
_BODY_LOG_LIMIT = 120_000


@router.get("/email-intake/setup")
async def email_intake_setup(user: dict = Depends(current_super_admin)):
    """Returns the per-user inbound address + setup instructions for SendGrid Inbound Parse."""
    settings = await get_or_create_settings(user["id"])
    intake = settings.get("email_intake") or {}
    base_domain = intake.get("inbound_domain") or company_profile.inbound_parse_domain()
    return {
        "enabled": intake.get("enabled", True),
        "inbound_domain": base_domain,
        "addresses": {
            "auto_detect": f"apply@{base_domain}",
            "per_pipeline": f"<pipeline-slug>@{base_domain}",
        },
        # The real secret is never echoed back; the owner pastes it in place
        # of the placeholder (see docs/SERVICES.md).
        "webhook_url": f"{os.environ.get('APP_PUBLIC_URL', '').rstrip('/')}/api/webhooks/sendgrid/inbound?key=<SENDGRID_INBOUND_SECRET>",
        "secret_configured": bool((os.environ.get("SENDGRID_INBOUND_SECRET") or "").strip()),
        "instructions": [
            f"1. Add an MX record for your subdomain (e.g. {base_domain}) pointing to mx.sendgrid.net (priority 10).",
            "2. SendGrid → Settings → Inbound Parse → Add Host & URL.",
            f"3. Subdomain: {base_domain}",
            "4. Destination URL: paste the webhook_url above, replacing <SENDGRID_INBOUND_SECRET> with the value of that variable.",
            "5. Tick 'POST the raw, full MIME message' = OFF (we expect parsed).",
            f"6. Save. Then forward resumes to apply@{base_domain} — they'll auto-create candidates.",
        ],
    }


@router.get("/email-intake/pipeline/{pipeline_id}")
async def email_intake_for_pipeline(pipeline_id: str, user: dict = Depends(current_user)):
    """Returns the inbound email address tied to a specific pipeline so recruiters
    can copy it from Add Applicant → Email-in tab. Available to recruiters scoped
    to that pipeline (not just super-admin)."""
    # current_user already resolves a sub-account to its owner's id, so the
    # account filter is enough. There used to be a fallback lookup by id alone,
    # which returned another account's pipeline and intake settings.
    await assert_pipeline_access(user, pipeline_id)
    pipe = await db.pipelines.find_one({"id": pipeline_id, "user_id": user["id"]}, {"_id": 0})
    if not pipe:
        raise HTTPException(404, "Pipeline not found")
    settings = await db.settings.find_one({"user_id": pipe["user_id"], "pipeline_id": None}, {"_id": 0}) or {}
    intake = settings.get("email_intake") or {}
    base_domain = intake.get("inbound_domain") or company_profile.inbound_parse_domain()
    slug = (pipe.get("public_slug") or "").strip()
    inbound_address = f"{slug}@{base_domain}" if slug else f"apply@{base_domain}"
    return {
        "pipeline_name": pipe.get("name", ""),
        "pipeline_slug": slug,
        "inbound_address": inbound_address,
        "fallback_address": f"apply@{base_domain}",
        "enabled": intake.get("enabled", True),
        "auto_dial": intake.get("auto_dial", True),
    }


@router.put("/email-intake/settings")
async def update_email_intake(payload: Dict[str, Any] = Body(...), user: dict = Depends(current_super_admin)):
    await get_or_create_settings(user["id"])
    update = {"email_intake": {
        "enabled": bool(payload.get("enabled", True)),
        "inbound_domain": (payload.get("inbound_domain") or "").strip(),
        "default_pipeline_id": (payload.get("default_pipeline_id") or "").strip(),
        "auto_dial": bool(payload.get("auto_dial", True)),
    }, "updated_at": now_iso()}
    await db.settings.update_one({"user_id": user["id"], "pipeline_id": None}, {"$set": update})
    return {"ok": True, "email_intake": update["email_intake"]}


@router.get("/email-intake/aliases")
async def list_aliases(pipeline_id: str, user: dict = Depends(current_user)):
    """List per-employee email-in aliases for a pipeline."""
    await assert_pipeline_access(user, pipeline_id)
    pipe = await db.pipelines.find_one({"id": pipeline_id, "user_id": user["id"]}, {"_id": 0})
    if not pipe:
        raise HTTPException(404, "Pipeline not found")
    settings = await db.settings.find_one({"user_id": user["id"], "pipeline_id": None}, {"_id": 0}) or {}
    intake = settings.get("email_intake") or {}
    base_domain = intake.get("inbound_domain") or company_profile.inbound_parse_domain()
    rows = await db.employee_email_aliases.find(
        {"user_id": user["id"], "pipeline_id": pipeline_id}, {"_id": 0}
    ).sort("created_at", 1).to_list(200)
    for r in rows:
        r["email"] = f"{r['slug']}@{base_domain}"
    return rows


@router.post("/email-intake/aliases")
async def create_alias(payload: Dict[str, Any] = Body(...), user: dict = Depends(current_user)):
    """Create a unique email-in alias for an employee on a pipeline."""
    # An alias files every email sent to it as a candidate in that office:
    # writers only, and only in an office they are assigned to.
    require_mover(user)
    fields = {k: payload.get(k) for k in ("pipeline_id", "employee_name", "slug")}
    if any(v is not None and not isinstance(v, str) for v in fields.values()):
        raise HTTPException(400, "pipeline_id, employee_name, and slug must be strings")
    pipeline_id = (fields["pipeline_id"] or "").strip()
    employee_name = (fields["employee_name"] or "").strip()
    slug = (fields["slug"] or "").strip().lower()
    if not (pipeline_id and employee_name and slug):
        raise HTTPException(400, "pipeline_id, employee_name, and slug are required")
    await assert_pipeline_in_tenant(user, pipeline_id)
    existing = await db.employee_email_aliases.find_one({"slug": slug}, {"_id": 0, "id": 1})
    if existing:
        raise HTTPException(409, "That slug is already in use — choose a different one")
    doc = {
        "id": new_id(),
        "user_id": user["id"],
        "pipeline_id": pipeline_id,
        "employee_name": employee_name,
        "slug": slug,
        "created_at": now_iso(),
    }
    await db.employee_email_aliases.insert_one(doc)
    settings = await db.settings.find_one({"user_id": user["id"], "pipeline_id": None}, {"_id": 0}) or {}
    intake = settings.get("email_intake") or {}
    base_domain = intake.get("inbound_domain") or company_profile.inbound_parse_domain()
    doc.pop("_id", None)
    doc["email"] = f"{slug}@{base_domain}"
    return doc


@router.delete("/email-intake/aliases/{alias_id}")
async def delete_alias(alias_id: str, user: dict = Depends(current_user)):
    require_mover(user)
    alias = await db.employee_email_aliases.find_one(
        {"id": alias_id, "user_id": user["id"]}, {"_id": 0, "pipeline_id": 1})
    if not alias:
        raise HTTPException(404, "Alias not found")
    await assert_pipeline_access(user, alias.get("pipeline_id") or "")
    await db.employee_email_aliases.delete_one({"id": alias_id, "user_id": user["id"]})
    return {"ok": True}


@router.get("/email-intake/log")
async def email_intake_log(user: dict = Depends(current_super_admin)):
    rows = await db.email_intake_log.find(
        {"user_id": user["id"]}, {"_id": 0}
    ).sort("created_at", -1).to_list(100)
    return rows


@router.get("/email-intake/log/{log_id}")
async def email_intake_log_entry(log_id: str, user: dict = Depends(current_super_admin)):
    row = await db.email_intake_log.find_one({"id": log_id, "user_id": user["id"]}, {"_id": 0})
    if not row:
        raise HTTPException(404, "Log entry not found")
    return row


@router.get("/email-intake/failed-resumes")
async def list_failed_resumes(user: dict = Depends(current_super_admin)):
    """Returns inbound emails where the resume couldn't be parsed (empty fields,
    extraction failure, etc.). Recruiter can re-process or dismiss from here."""
    rows = await db.email_intake_log.find(
        {"user_id": user["id"], "status": {"$in": ["parse_failed", "rejected"]}},
        {"_id": 0},
    ).sort("created_at", -1).to_list(200)
    return rows


@router.post("/email-intake/failed-resumes/{log_id}/dismiss")
async def dismiss_failed_resume(log_id: str, user: dict = Depends(current_super_admin)):
    res = await db.email_intake_log.update_one(
        {"id": log_id, "user_id": user["id"]},
        {"$set": {"status": "dismissed", "dismissed_at": now_iso()}},
    )
    if not res.modified_count:
        raise HTTPException(404, "log entry not found")
    return {"ok": True}


async def record_link_only_application(*, user_id: str, pipeline_doc: dict, parsed: dict) -> dict:
    """Remember an application that arrived without a resume attached.

    Idempotent on (pipeline, applicant name, job title) within a day so a
    re-delivered webhook doesn't list the same person twice. The resume link
    itself is never stored.
    """

    name = (parsed.get("applicant_name") or "").strip()
    job_title = (parsed.get("job_title") or "").strip()
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    existing = await db.link_only_applications.find_one(
        {"user_id": user_id, "pipeline_id": pipeline_doc["id"], "applicant_name": name,
         "job_title": job_title, "created_at": {"$gte": since}},
        {"_id": 0, "id": 1},
    )
    if existing:
        return existing
    item = {
        "id": new_id(),
        "user_id": user_id,
        "pipeline_id": pipeline_doc["id"],
        "pipeline_name": pipeline_doc.get("name"),
        "status": "open",
        "applicant_name": name,
        "job_title": job_title,
        "relevant_experience": parsed.get("relevant_experience") or "",
        "qualifications": parsed.get("qualifications") or [],
        "created_at": now_iso(),
    }
    await db.link_only_applications.insert_one(dict(item))
    return item


@router.post("/webhooks/sendgrid/inbound")
async def sendgrid_inbound_webhook(request: Request):
    """SendGrid Inbound Parse → creates a candidate from the resume attachment.

    Inbound Parse cannot sign requests, so the destination URL carries a
    secret: .../api/webhooks/sendgrid/inbound?key=<SENDGRID_INBOUND_SECRET>
    (or the basic-auth password). Fails closed: without it anyone could file
    candidates who are then texted and called."""
    from webhook_auth import require_sendgrid_inbound_secret
    require_sendgrid_inbound_secret(request)
    from email_intake_service import (
        parse_email_address, split_full_name, detect_pipeline,
        select_all_resume_attachments, extract_phone_from_text, normalize_phone,
    )
    form = await request.form()
    attachments: Dict[str, Any] = {}
    fields: Dict[str, str] = {}
    for key, value in form.multi_items():
        if hasattr(value, "filename"):
            attachments[key] = value
        else:
            fields[key] = value

    from_addr = fields.get("from", "")
    to_addr = fields.get("to", "")
    subject = fields.get("subject", "")
    text = fields.get("text", "") or ""
    html = fields.get("html", "") or ""
    try:
        attachment_info = json.loads(fields.get("attachment-info") or "{}") if fields.get("attachment-info") else {}
    except Exception:
        attachment_info = {}

    # Extract Message-ID from raw headers for idempotency — SendGrid may deliver
    # the same email twice (retry on timeout, or forwarded + direct delivery).
    raw_headers = fields.get("headers", "")
    mid_match = re.search(r"Message-ID:\s*<([^>]+)>", raw_headers, re.IGNORECASE)
    message_id = mid_match.group(1).strip() if mid_match else None
    if message_id:
        existing = await db.email_intake_log.find_one(
            {"message_id": message_id, "status": {"$nin": ["rejected", "parse_failed", "dismissed"]}},
            {"_id": 0, "candidate_id": 1, "status": 1},
        )
        if existing:
            logger.info(f"Duplicate inbound webhook (message_id={message_id}), skipping")
            return {"ok": True, "duplicate": True, "candidate_id": existing.get("candidate_id")}

    sender_name, sender_email = parse_email_address(from_addr)
    _, recipient_email = parse_email_address(to_addr)

    # The address the message was actually delivered to, which is not always the
    # one in the To header. An Exchange mail flow rule that Bcc's applications here
    # leaves To as the recruiter's own mailbox, so routing on the header alone would
    # miss the pipeline slug and fall back to guessing the city from the subject.
    # SendGrid reports the true envelope recipient; prefer it when it names a slug.
    envelope_recipient = ""
    try:
        env = json.loads(fields.get("envelope") or "{}")
        env_to = env.get("to")
        if isinstance(env_to, list) and env_to:
            _, envelope_recipient = parse_email_address(str(env_to[0]))
        elif isinstance(env_to, str):
            _, envelope_recipient = parse_email_address(env_to)
    except Exception:
        envelope_recipient = ""
    body_text = (text or "").strip()
    body_html = (html or "").strip()
    log_doc: Dict[str, Any] = {
        "id": new_id(),
        "from": sender_email,
        "to": recipient_email,
        "subject": subject,
        "received_at": now_iso(),
        "created_at": now_iso(),
        "status": "received",
        "message_id": message_id,
        "body_text": body_text[:_BODY_LOG_LIMIT] if body_text else "",
        "body_html": body_html[:_BODY_LOG_LIMIT] if body_html else "",
    }

    users = await db.users.find({}, {"_id": 0, "id": 1}).to_list(20)
    if not users:
        log_doc.update({"status": "rejected", "error": "no users in system"})
        return {"ok": False, "error": "no users registered"}

    def _local_part(addr: str) -> str:
        part = (addr.split("@", 1)[0] if "@" in addr else "").lower()
        return part.split("+", 1)[1] if "+" in part else part

    # Envelope first — it is where the mail was really delivered. Falls back to the
    # To header, which is the same address for a plain forward.
    local = _local_part(envelope_recipient) or _local_part(recipient_email)

    # 0. Employee alias match — check before pipeline slug so personalized
    #    addresses (e.g. sam-downtown@inbox…) are caught first.
    email_referred_by: str | None = None
    pipeline_doc = None
    if local and local != "apply":
        alias_doc = await db.employee_email_aliases.find_one({"slug": local}, {"_id": 0})
        if alias_doc:
            pipeline_doc = await db.pipelines.find_one({"id": alias_doc["pipeline_id"]}, {"_id": 0})
            if pipeline_doc:
                email_referred_by = alias_doc.get("employee_name")

    # 1. Exact slug match — searches across all tenants (slugs are globally unique).
    if not pipeline_doc and local and local != "apply":
        pipeline_doc = await db.pipelines.find_one({"public_slug": local}, {"_id": 0})

    # 2. If slug didn't resolve, run detect_pipeline across ALL tenants' pipelines so
    #    we route to the correct owner even in multi-user deployments where users[0]
    #    may not be the intended recipient.
    if not pipeline_doc:
        all_pipelines = await db.pipelines.find({}, {"_id": 0}).to_list(200)
        pipeline_doc = detect_pipeline(
            all_pipelines, envelope_recipient or recipient_email, subject, f"{text}\n{html}"
        )

    target_user_id = pipeline_doc["user_id"] if pipeline_doc else users[0]["id"]
    log_doc["user_id"] = target_user_id

    # Email intake config (enabled/disabled + auto_dial) is tenant-wide, so read
    # the GLOBAL settings doc here. Per-pipeline overrides are applied below
    # once we know which pipeline the resume belongs to.
    settings = await db.settings.find_one({"user_id": target_user_id, "pipeline_id": None}, {"_id": 0}) or {}
    intake_settings = settings.get("email_intake") or {}
    if not intake_settings.get("enabled", True):
        log_doc.update({"status": "skipped", "error": "email_intake disabled"})
        await db.email_intake_log.insert_one(log_doc)
        return {"ok": False, "error": "intake disabled"}

    if not pipeline_doc:
        log_doc.update({"status": "rejected", "error": "no pipeline matched"})
        await db.email_intake_log.insert_one(log_doc)
        return {"ok": False, "error": "no pipeline matched"}
    log_doc["pipeline_id"] = pipeline_doc["id"]
    log_doc["pipeline_name"] = pipeline_doc.get("name")

    # Re-resolve settings against the matched pipeline so per-pipeline overrides
    # (warmup template, agent name, etc.) apply to inbound-email candidates too.
    from deps import resolve_settings
    pipeline_settings = await resolve_settings(target_user_id, pipeline_doc["id"])
    if pipeline_settings:
        settings = pipeline_settings

    all_attachments = select_all_resume_attachments(attachments, attachment_info)
    if not all_attachments:
        # Indeed's "new application" notification usually links to the resume
        # instead of attaching it, and carries no phone number. This open-source
        # build does not download resumes from Indeed, so the applicant is
        # recorded as a link-only application and shown in the Needs Attention
        # panel for a recruiter to add by hand (or to forward the resume to the
        # intake address as an attachment). To get resumes attached
        # automatically, have your job board email the resume itself, or post
        # applications to the intake webhook (Settings → Integrations). Any
        # other attachment-less email falls through to the rejection below.
        try:
            from indeed_intake import parse_indeed_application
            indeed = parse_indeed_application(subject, body_html, body_text)
        except Exception as e:
            indeed = None
            logger.warning(f"indeed parse errored, falling back to normal handling: {e}")
        if indeed:
            item = await record_link_only_application(
                user_id=target_user_id, pipeline_doc=pipeline_doc, parsed=indeed,
            )
            log_doc.update({
                "status": "needs_manual_add",
                "link_only_id": item.get("id"),
                "candidate_name": indeed.get("applicant_name") or "",
                # The body links to the applicant's resume; don't keep it.
                "body_html": "",
            })
            await db.email_intake_log.insert_one(log_doc)
            logger.info(f"email-intake: link-only Indeed application recorded for manual add ({item.get('id')})")
            return {"ok": True, "needs_manual_add": True, "link_only_id": item.get("id")}

        log_doc.update({"status": "rejected", "error": "no resume attachment"})
        await db.email_intake_log.insert_one(log_doc)
        return {"ok": False, "error": "no resume attachment found"}

    logger.info(f"email-intake: {len(all_attachments)} resume attachment(s) from {sender_email}")

    job_doc = await db.jobs.find_one(
        {"pipeline_id": pipeline_doc["id"], "user_id": target_user_id, "is_active": True},
        {"_id": 0},
    )

    # Read all file content NOW while the request is still open, then return 200
    # to SendGrid immediately. If we don't respond within ~3 minutes, SendGrid
    # retries the webhook and we end up processing the same 95 resumes twice.
    attachment_data = []
    for filename, upload in all_attachments:
        content = await upload.read()
        attachment_data.append((filename, content))

    asyncio.create_task(_process_inbound_batch(
        attachment_data, log_doc, target_user_id, pipeline_doc,
        job_doc, settings, intake_settings, sender_email, sender_name, subject, text,
        email_referred_by,
    ))
    logger.info(f"email-intake: accepted {len(attachment_data)} attachment(s) for background processing")
    return {"ok": True, "accepted": len(attachment_data), "processing": "background"}


async def _process_inbound_batch(
    attachment_data: list,
    log_doc: Dict[str, Any],
    target_user_id: str,
    pipeline_doc: Dict[str, Any],
    job_doc,
    settings: Dict[str, Any],
    intake_settings: Dict[str, Any],
    sender_email: str,
    sender_name: str,
    subject: str,
    text: str,
    email_referred_by: str | None = None,
) -> None:
    """Background worker: parse + ingest each resume attachment sequentially."""
    from email_intake_service import split_full_name, extract_phone_from_text, normalize_phone
    from deps import find_duplicate_candidate
    results = []

    for att_index, (filename, content) in enumerate(attachment_data):
        # Acquire global parse lock — one resume processed at a time across all
        # concurrent requests, with a 25-second cooldown between each parse so
        # bulk drops (100 attachments in one email) don't spike the AI API.
        async with _inbound_parse_lock:
            att_log: Dict[str, Any] = {
                **log_doc,
                "id": new_id(),
                "attachment_filename": filename,
                "attachment_size": len(content),
                "attachment_index": att_index,
                "total_attachments": len(attachment_data),
            }

            resume_text = extract_resume_text(filename, content)
            candidate_id = new_id()
            parsed = await parse_resume(resume_text, candidate_id)
            parse_failed = (
                not (parsed.get("first_name") or "").strip()
                and not (parsed.get("last_name") or "").strip()
                and not (parsed.get("email") or "").strip()
                and not (parsed.get("phone") or "").strip()
                and not (parsed.get("skills") or [])
            )
            if parse_failed:
                att_log.update({
                    "status": "parse_failed",
                    "error": "Claude could not extract structured fields from the resume.",
                    "resume_text_excerpt": (resume_text or "")[:1000],
                    "raw_filename": filename,
                    "sender_email": sender_email,
                    "sender_name": sender_name,
                })
                await db.email_intake_log.insert_one(att_log)
                logger.warning(f"resume parse failed for {filename} from {sender_email}")
                results.append({"ok": False, "error": "parse_failed", "filename": filename, "log_id": att_log["id"]})
                await asyncio.sleep(25)
                continue

            first_from_resume = parsed.get("first_name") or ""
            last_from_resume = parsed.get("last_name") or ""
            if not (first_from_resume and last_from_resume) and sender_name:
                sf, sl = split_full_name(sender_name)
                first_from_resume = first_from_resume or sf
                last_from_resume = last_from_resume or sl
            # Do NOT fall back to sender_email — if the resume has no email address,
            # leave it blank. Using the forwarding address contaminates the candidate
            # record and sends comms to the wrong person.
            # Lowercased — CVs shout emails in caps, and a case-different copy
            # of the same address sailed straight past every exact-match dedupe
            # (the same applicant twice, six minutes apart, one already booked).
            email_field = (parsed.get("email") or "").strip().lower()
            # Resume parses sometimes drop the "@" (an address missing its @) — the
            # address then survives every truthiness check and detonates inside
            # the email sender on every send for that candidate, forever.
            # Repair the unambiguous big-provider shape; otherwise treat as no
            # email (the phone leg still works and needs-attention flags it).
            if email_field and "@" not in email_field:
                _m = re.match(r"^(.+?)(gmail|yahoo|hotmail|outlook|icloud|aol)(\.[a-z.]+)$", email_field)
                email_field = f"{_m.group(1)}@{_m.group(2)}{_m.group(3)}" if _m else ""
            # normalize_phone only strips separators — a US number off a resume
            # stayed 10 raw digits, so half of today's intake stored raw phones.
            # E.164 here keeps storage consistent with the apply form and makes
            # the phone-based dedupe fingerprint reliable across batches. (The
            # dial-site backstop already normalizes at call time either way.)
            from deps import normalize_phone_e164
            phone_field = normalize_phone_e164(
                normalize_phone(parsed.get("phone") or "") or extract_phone_from_text(resume_text) or extract_phone_from_text(text)
            )

            cand = Candidate(
                id=candidate_id,
                user_id=target_user_id,
                pipeline_id=pipeline_doc["id"],
                job_id=(job_doc or {}).get("id"),
                first_name=first_from_resume or "Unknown",
                last_name=last_from_resume or "",
                email=email_field,
                phone=phone_field,
                resume_text=resume_text[:20000],
                parsed_resume=parsed,
                stage="SCREENING",
                referred_by=email_referred_by,
            )
            if job_doc:
                try:
                    score = await smart_score(parsed, job_doc, candidate_id)
                    cand.smart_score = score.get("score")
                    cand.smart_score_rationale = score.get("rationale", "")
                except Exception as e:
                    logger.warning(f"smart_score failed for {filename}: {e}")

            cand.chat_log = [{
                "role": "agent",
                "text": (
                    f"Resume received via email from {sender_email}. "
                    f"Subject: \"{subject}\". Pipeline auto-detected: {pipeline_doc.get('name')}."
                ),
                "at": now_iso(),
            }]
            doc = cand.model_dump()
            dup = await find_duplicate_candidate(pipeline_doc["id"], email_field or "", phone_field or "")
            if dup:
                logger.info(f"email_intake: duplicate {email_field} in pipeline {pipeline_doc['id']}")
                att_log.update({"status": "duplicate", "existing_id": dup["id"]})
                await db.email_intake_log.insert_one(att_log)
                results.append({"ok": True, "status": "duplicate", "filename": filename, "existing_id": dup["id"]})
                await asyncio.sleep(25)
                continue

            await db.candidates.insert_one(doc)
            att_log.update({
                "status": "ingested",
                "candidate_id": cand.id,
                "candidate_name": f"{cand.first_name} {cand.last_name}".strip(),
            })
            await db.email_intake_log.insert_one(att_log)

            # Intake is unattended, so a partial result would otherwise be silent —
            # the candidate looks healthy in the pipeline while the dialer never calls.
            try:
                from needs_attention_service import candidate_issues, notification_text
                from notifications_service import create_notification
                issues = candidate_issues(doc)
                if issues:
                    msg = notification_text(
                        f"{cand.first_name} {cand.last_name}".strip(),
                        issues, pipeline_doc.get("name", ""),
                    )
                    await create_notification(
                        target_user_id, "candidate_incomplete", msg["title"], msg["body"],
                        link=f"/?candidate={cand.id}", candidate_id=cand.id,
                        pipeline_id=pipeline_doc["id"],
                    )
            except Exception as e:
                logger.warning(f"incomplete-candidate notification failed for {cand.id}: {e}")

            # No email address means no arrival email to send — intake deliberately
            # refuses to fall back to the sender's address — but the candidate may
            # still have a phone worth texting and calling.
            from screening_start import start_screening

            await start_screening(
                target_user_id, doc, settings,
                # email OR phone — send_stage_comms degrades per channel, and
                # Indeed resumes routinely arrive with the email redacted but
                # the phone intact. bool(cand.email) alone silently skipped ALL
                # arrival comms for exactly those candidates.
                send_comms=bool(cand.email or cand.phone),
                allow_dial=intake_settings.get("auto_dial", True),
            )

            results.append({
                "ok": True,
                "candidate_id": cand.id,
                "filename": filename,
                "pipeline": pipeline_doc.get("name"),
                "name": f"{cand.first_name} {cand.last_name}".strip(),
                "smart_score": cand.smart_score,
            })

            # 25-second cooldown before releasing the lock so next attachment
            # doesn't start immediately — keeps AI load steady under bulk drops.
            if att_index < len(attachment_data) - 1:
                await asyncio.sleep(25)

    ingested = [r for r in results if r.get("ok") and r.get("status") != "duplicate" and r.get("candidate_id")]
    logger.info(f"email-intake batch complete: {len(ingested)}/{len(attachment_data)} ingested")
