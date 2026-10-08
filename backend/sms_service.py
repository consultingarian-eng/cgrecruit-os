"""SMS service — unified sender for all outbound texts.

Centralizes:
1. **Sender resolution**: always uses the recruiter's *single warmed* SMS sender
   (Settings → Screen Call Agent → SMS Sender Number). Per-pipeline Twilio numbers
   are voice-only — Twilio penalizes "cold" numbers texting, so we keep all SMS
   coming from one warmed number.
2. **First-message STOP/HELP disclosure** (TCPA / 10DLC compliance).
   The very first SMS sent to any phone number gets the opt-out clause appended.
3. **Opt-out enforcement**: if the candidate has previously replied STOP, we skip
   the message entirely.
"""
import os
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Default disclosure text appended to a candidate's first-ever SMS.
DEFAULT_OPTOUT_DISCLOSURE = "Reply STOP to opt out."


# SMS deliverability suffers when you spread texts across cold/new numbers, so
# each office keeps one stable, warmed sender. Set it per office as
# "sms_number" in backend/company_profile.json; offices without one share the
# TWILIO_PHONE_NUMBER env var.


def resolve_sms_sender(settings: Dict[str, Any], pipeline: Optional[Dict[str, Any]] = None) -> str:
    """Pick the outbound SMS number based on the candidate's pipeline office.

    Order: the explicit super-admin override
    (`screen_call_agent.sms_sender_number`), then the pipeline's office
    `sms_number` from the company profile, then TWILIO_PHONE_NUMBER.
    """
    import company_profile
    sca = settings.get("screen_call_agent", {}) or {}
    explicit_override = (sca.get("sms_sender_number") or "").strip()
    if explicit_override:
        return explicit_override
    if pipeline:
        number = company_profile.office_sms_number(company_profile.office_key_for_pipeline(pipeline))
        if number:
            return number
    return company_profile.default_twilio_number()


async def has_received_sms_before(db, phone_number: str) -> bool:
    """Check whether this phone number has been sent at least one SMS by us before
    (across all candidates / pipelines / users)."""
    if not phone_number:
        return False
    n = await db.communications.count_documents({
        "type": "sms",
        "to_address": phone_number,
        "status": "sent",
    })
    return n > 0


def optout_key(phone_number: str) -> str:
    """The last ten digits — the only form both sides of this agree on.

    An opt-out arrives from Twilio as "+16175551234". The number we check before
    sending is whatever was typed into the candidate record: "(617) 555-1234",
    "617-555-1234", "6175551234". Matching those as exact strings never
    succeeds, so an opt-out recorded on one shape would be invisible to every
    send that used another — the list would look populated and still stop nothing.
    """
    digits = "".join(ch for ch in (phone_number or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


async def is_opted_out(db, phone_number: str) -> bool:
    if not phone_number:
        return True
    key = optout_key(phone_number)
    if not key:
        return True
    doc = await db.sms_optouts.find_one(
        # phone_number is still matched for any row written before this keyed on
        # digits; new rows always carry phone_key.
        {"$or": [{"phone_key": key}, {"phone_number": phone_number}]},
        {"_id": 0},
    )
    return bool(doc)


async def mark_opted_out(db, phone_number: str, source: str = "twilio_inbound") -> None:
    from datetime import datetime, timezone
    key = optout_key(phone_number)
    await db.sms_optouts.update_one(
        {"phone_key": key},
        {"$set": {
            "phone_key": key,
            "phone_number": phone_number,
            "source": source,
            "at": datetime.now(timezone.utc).isoformat(),
        }},
        upsert=True,
    )


async def mark_opted_in(db, phone_number: str) -> None:
    """Candidate replied START / UNSTOP — clear the opt-out flag."""
    await db.sms_optouts.delete_one({"phone_number": phone_number})


async def send_direct_sms(
    db,
    settings: Dict[str, Any],
    phone: str,
    body: str,
    pipeline: Optional[Dict[str, Any]] = None,
    template_key: str = "direct",
    user_id: str = "",
    candidate_id: str = "",
) -> Dict[str, Any]:
    """Send an SMS to a bare phone number — no candidate record required.

    `send_candidate_sms` below needs a candidate doc for the sender lookup and
    the Communication row, so it cannot serve the inbound front-desk agent: the
    caller who most needs the office address texted to them is the one who has
    never applied and has no record. Same opt-out and first-message-disclosure
    rules apply — a stranger is exactly who must get the STOP clause.

    Returns {status: 'sent'|'skipped'|'failed', ...}.
    """
    if not phone:
        return {"status": "skipped", "reason": "no phone"}
    if await is_opted_out(db, phone):
        logger.info(f"direct sms skipped — {phone} previously opted out")
        return {"status": "skipped", "reason": "recipient opted out"}

    from voice_service import send_sms
    from models import Communication

    from_number = resolve_sms_sender(settings, pipeline=pipeline)
    if not from_number:
        return {"status": "failed", "error": "no SMS sender configured"}

    needs_disclosure = not await has_received_sms_before(db, phone)
    if needs_disclosure and "STOP" not in body.upper():
        body = f"{body}\n\n{DEFAULT_OPTOUT_DISCLOSURE}"

    res = send_sms(phone, body, from_number=from_number)

    # Logged like any other outbound text so it shows in the comms history and
    # counts towards `has_received_sms_before` for the next send to this number.
    try:
        comm = Communication(
            candidate_id=candidate_id or "",
            user_id=user_id or "",
            type="sms",
            template_key=template_key,
            subject=None,
            body=body,
            to_address=phone,
            status=res.get("status", "failed"),
            error=res.get("error"),
        ).model_dump()
        await db.communications.insert_one(comm)
    except Exception as e:
        logger.warning(f"direct sms comm log failed: {e}")

    return {**res, "body": body, "from_number": from_number, "disclosure_appended": needs_disclosure}


async def send_candidate_sms(
    db,
    settings: Dict[str, Any],
    candidate: Dict[str, Any],
    body: str,
    template_key: str = "manual",
    force_disclosure: Optional[bool] = None,
) -> Dict[str, Any]:
    """Send an SMS to a candidate with proper sender + STOP-clause + opt-out handling.

    Returns {status: 'sent'|'skipped'|'failed', sid?, error?, body?, ...}.
    The `body` returned reflects what was actually sent (with disclosure appended if any).
    """
    from voice_service import send_sms
    from models import Communication

    phone = (candidate or {}).get("phone") or ""
    if not phone:
        return {"status": "skipped", "reason": "no phone"}
    if await is_opted_out(db, phone):
        logger.info(f"sms skipped — {phone} previously opted out")
        return {"status": "skipped", "reason": "recipient opted out"}

    from_number = resolve_sms_sender(
        settings,
        pipeline=await db.pipelines.find_one({"id": (candidate or {}).get("pipeline_id")}, {"_id": 0}) if (candidate or {}).get("pipeline_id") else None,
    )
    if not from_number:
        return {"status": "failed", "error": "no SMS sender configured"}

    # Append the STOP/HELP disclosure on the first SMS to this number, OR when forced.
    needs_disclosure = bool(force_disclosure) or not await has_received_sms_before(db, phone)
    if needs_disclosure:
        disclosure = DEFAULT_OPTOUT_DISCLOSURE
        # Avoid double-appending if the body already mentions STOP
        if "STOP" not in body.upper():
            body = f"{body}\n\n{disclosure}"

    res = send_sms(phone, body, from_number=from_number)

    comm = Communication(
        candidate_id=candidate["id"],
        user_id=candidate["user_id"],
        type="sms",
        template_key=template_key,
        subject=None,
        body=body,
        to_address=phone,
        status=res.get("status", "failed"),
        error=res.get("error"),
    ).model_dump()
    await db.communications.insert_one(comm)
    return {**res, "body": body, "from_number": from_number, "disclosure_appended": needs_disclosure}
