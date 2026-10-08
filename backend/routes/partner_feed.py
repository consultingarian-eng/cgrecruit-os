"""Optional partner reporting feed — stage-event ledger.

Off unless PARTNER_FEED_API_KEY is set. Country, campaign prefix and per-office pins come
from "partner_feed" and offices.<key>.partner_pin in backend/company_profile.json.

GET /partner/stage-events?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD

Returns a JSON array of stage-event rows mapped to a fixed stage taxonomy
(S01–S08, S99). One row per stage transition per candidate. Candidates are
selected whose updated_at or created_at falls within [date_from, date_to].
stage_event_id is stable so the partner can deduplicate on incremental syncs.

Auth: X-Partner-Api-Key request header must equal the PARTNER_FEED_API_KEY env var.

Stage mapping (CGRecruit → feed):
  S01 APPLICATION             created_at           SYSTEM   source: APPLICANT_CREATED
  S02 SCREENING               created_at (proxy)   AI       source: SCREENING (inferred — no timestamp)
  S03 BOOKED_IN               appointment_at       HUMAN    source: APPOINTMENT
  S04 PRESENTATION            appointment_at       HUMAN    source: APPOINTMENT_ATTENDED
  S05 FORM_SENT               moved_to_form_at     HUMAN    source: FORM
  S06 OFFER_CLOSE             moved_to_close_at    HUMAN    source: CLOSE
  S07 PRODUCT_TRAINING_BOOKED moved_to_training_at HUMAN    source: TRAINING
  S08 PRODUCT_TRAINING        training_attended_at HUMAN    source: TRAINING_ATTENDED
  S99 EXIT_STATUS             archived_at          HUMAN    source: ARCHIVED

campaign_ref format:  {COUNTRY}_{CAMPAIGN_PREFIX}_{PIPELINE_SLUG}_{YYYYMM}
source_channel:       DIRECT — CGRecruit does not capture a recruitment source channel
S02 note:             No moved_to_screening_at timestamp exists; created_at is used as proxy.
                      S01 and S02 share the same stage_datetime but have distinct stage_event_ids.
"""
import os
import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.requests import Request

from deps import db

router = APIRouter()

PLATFORM_NAME = "CGRECRUIT"
SOURCE_CHANNEL = "DIRECT"  # not captured in CGRecruit


def _feed_cfg() -> Dict[str, str]:
    import company_profile
    return company_profile.PROFILE.get("partner_feed") or {}


def _tenant_country() -> str:
    return (_feed_cfg().get("country") or "").upper()


def _mc_pin_map() -> Dict[str, str]:
    import company_profile
    return {k: str(o.get("partner_pin")) for k, o in company_profile.offices().items() if o.get("partner_pin")}


STAGE_NAMES: Dict[str, str] = {
    "S01": "APPLICATION",
    "S02": "SCREENING",
    "S03": "BOOKED_IN",
    "S04": "PRESENTATION",
    "S05": "FORM_SENT",
    "S06": "OFFER_CLOSE",
    "S07": "PRODUCT_TRAINING_BOOKED",
    "S08": "PRODUCT_TRAINING",
    "S99": "EXIT_STATUS",
}

SOURCE_STAGE_NAMES: Dict[str, str] = {
    "S01": "APPLICANT_CREATED",
    "S02": "SCREENING",
    "S03": "APPOINTMENT",
    "S04": "APPOINTMENT_ATTENDED",
    "S05": "FORM",
    "S06": "CLOSE",
    "S07": "TRAINING",
    "S08": "TRAINING_ATTENDED",
    "S99": "ARCHIVED",
}

EXIT_REASON_MAP: Dict[str, str] = {
    "uncontactable": "UNCONTACTABLE",
    "no_show": "NO_SHOW",
    "no_show_no_response": "NO_SHOW",
    "withdrawn": "WITHDRAWN",
    "rejected": "REJECTED",
    "duplicate": "DUPLICATE",
    "invalid": "INVALID",
    "attended_no_form": "WITHDRAWN",
}

SCREENING_STATUS_MAP: Dict[str, str] = {
    "pending": "PENDING",
    "queued": "QUEUED",
    "in_progress": "IN_PROGRESS",
    "approved": "PASSED",
    "strong": "PASSED",
    "no_answer": "NO_ANSWER",
    "didnt_connect": "INCOMPLETE",
    "incomplete_info": "INCOMPLETE",
    "rejected": "FAILED",
}


def _check_auth(request: Request) -> None:
    expected = os.environ.get("PARTNER_FEED_API_KEY", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Partner feed disabled until PARTNER_FEED_API_KEY is set")
    given = request.headers.get("X-Partner-Api-Key", "")
    import hmac
    if not hmac.compare_digest(str(given or ""), expected):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Partner-Api-Key")


def _compact_ts(iso: str) -> str:
    return re.sub(r"[^0-9]", "", iso[:19])


def _campaign_ref(pipeline_name: str, pipeline_created_at: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", pipeline_name.strip()).upper()[:30].strip("_") or "UNKNOWN"
    month = re.sub(r"[^0-9]", "", (pipeline_created_at or "202001")[:7])
    prefix = "_".join(p for p in (_tenant_country(), (_feed_cfg().get("campaign_prefix") or "").upper()) if p)
    return f"{prefix}_{slug}_{month}" if prefix else f"{slug}_{month}"


def _mc_pin(cg1_office_key: str) -> str:
    return _mc_pin_map().get((cg1_office_key or "").lower().replace(" ", ""), _feed_cfg().get("default_pin") or "")


def _exit_reason(archived_reason: Optional[str]) -> str:
    if not archived_reason:
        return "UNKNOWN"
    if archived_reason.startswith("merged_into:"):
        return "DUPLICATE"
    return EXIT_REASON_MAP.get(archived_reason.lower(), "WITHDRAWN")


def _snapshot_flags(cand: Dict[str, Any]) -> Dict[str, Any]:
    """Applicant-level boolean snapshot fields included on every row."""
    screening_status = cand.get("screening_status") or ""
    screening_done = screening_status in (
        "approved", "strong", "no_answer", "didnt_connect", "incomplete_info", "rejected"
    )
    screening_passed = screening_status in ("approved", "strong")
    booked = bool(cand.get("appointment_at"))
    attended = cand.get("attendance_status") in ("attended_form", "attended_no_form")
    training_booked = bool(cand.get("moved_to_training_at"))
    training_attended = bool(cand.get("training_attended"))
    start_date_str = (cand.get("training_start_at") or "")[:10] or None
    return {
        "screening_completed_flag": screening_done,
        "screening_passed_flag": screening_passed,
        "booked_in_flag": booked,
        "presentation_attended_flag": attended,
        "form_sent_flag": bool(cand.get("moved_to_form_at")),
        "offer_made_flag": bool(cand.get("moved_to_close_at")),
        "start_booked_flag": training_booked,
        "start_date": start_date_str,
        "product_training_booked_flag": training_booked,
        "product_training_datetime": cand.get("training_start_at") or None,
        "product_training_attended_flag": training_attended,
        # Absent = booked + start date set + explicitly not attended.
        # Only flagged when training_start_at is known; unknown absence stays False.
        "product_training_absent_flag": (
            training_booked and start_date_str is not None and not training_attended
        ),
    }


def _row(
    cand: Dict[str, Any],
    stage_id: str,
    stage_datetime: str,
    stage_actor_type: str,
    stage_status: str,
    campaign_ref: str,
    mc_pin: str,
    flags: Dict[str, Any],
    exit_reason: Optional[str] = None,
) -> Dict[str, Any]:
    cid = cand["id"]
    r: Dict[str, Any] = {
        # Identifiers
        "applicant_uid": f"CGRECRUIT_{cid}",
        "platform_applicant_id": cid,
        "platform_name": PLATFORM_NAME,
        "stage_event_id": f"CGRECRUIT_{cid}_{stage_id}_{_compact_ts(stage_datetime)}",
        # Campaign / tenant
        "tenant_country_code": _tenant_country(),
        "campaign_ref": campaign_ref,
        "mc_pin": mc_pin,
        # Applicant
        "applicant_first_name": cand.get("first_name") or "",
        "applicant_surname": cand.get("last_name") or "",
        "applicant_email": cand.get("email") or "",
        "applicant_phone": cand.get("phone") or "",
        "source_channel": SOURCE_CHANNEL,
        # Record timestamps
        "applied_datetime": cand.get("created_at") or "",
        "created_datetime": cand.get("created_at") or "",
        "updated_datetime": cand.get("updated_at") or "",
        # Stage event
        "stage_id": stage_id,
        "stage_name": STAGE_NAMES[stage_id],
        "stage_status": stage_status,
        "stage_datetime": stage_datetime,
        "stage_actor_type": stage_actor_type,
        "source_stage_name": SOURCE_STAGE_NAMES[stage_id],
        # Pipeline context (extra — aids supplier reconciliation)
        "pipeline_id": cand.get("pipeline_id") or "",
        "pipeline_name": cand.get("_pipeline_name") or "",
    }
    r.update(flags)
    if exit_reason is not None:
        r["exit_reason"] = exit_reason
    return r


def _build_events(cand: Dict[str, Any], campaign_ref: str, mc_pin: str) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    flags = _snapshot_flags(cand)
    screening_status_feed = SCREENING_STATUS_MAP.get(
        cand.get("screening_status") or "", "IN_PROGRESS"
    )

    if cand.get("created_at"):
        ts = cand["created_at"]
        events.append(_row(cand, "S01", ts, "SYSTEM", "RECEIVED", campaign_ref, mc_pin, flags))
        # S02 uses same timestamp — distinct stage_id keeps stage_event_id unique
        events.append(_row(cand, "S02", ts, "AI", screening_status_feed, campaign_ref, mc_pin, flags))

    if cand.get("appointment_at"):
        events.append(_row(cand, "S03", cand["appointment_at"], "HUMAN", "BOOKED",
                           campaign_ref, mc_pin, flags))

    if cand.get("attendance_status") and cand.get("appointment_at"):
        att = cand["attendance_status"]
        status = "NO_SHOW" if att == "no_show" else "ATTENDED"
        events.append(_row(cand, "S04", cand["appointment_at"], "HUMAN", status,
                           campaign_ref, mc_pin, flags))

    if cand.get("moved_to_form_at"):
        events.append(_row(cand, "S05", cand["moved_to_form_at"], "HUMAN", "SENT",
                           campaign_ref, mc_pin, flags))

    if cand.get("moved_to_close_at"):
        events.append(_row(cand, "S06", cand["moved_to_close_at"], "HUMAN", "CLOSED",
                           campaign_ref, mc_pin, flags))

    if cand.get("moved_to_training_at"):
        events.append(_row(cand, "S07", cand["moved_to_training_at"], "HUMAN", "BOOKED",
                           campaign_ref, mc_pin, flags))

    if cand.get("training_attended") and cand.get("training_attended_at"):
        events.append(_row(cand, "S08", cand["training_attended_at"], "HUMAN", "ATTENDED",
                           campaign_ref, mc_pin, flags))

    if cand.get("archived_at"):
        reason = _exit_reason(cand.get("archived_reason"))
        events.append(_row(cand, "S99", cand["archived_at"], "HUMAN", reason,
                           campaign_ref, mc_pin, flags, exit_reason=reason))

    return events


@router.get("/partner/stage-events")
async def partner_stage_events(
    request: Request,
    date_from: str = Query(..., description="Start date inclusive — YYYY-MM-DD"),
    date_to: str = Query(..., description="End date inclusive — YYYY-MM-DD"),
) -> List[Dict[str, Any]]:
    """Partner stage-event ledger.

    Returns all stage transitions for candidates active (created or updated)
    within the requested date window. One row per stage event per candidate.
    stage_event_id is stable — safe for incremental daily syncs with deduplication.

    Filtering logic: candidates where created_at OR updated_at falls within
    [date_from, date_to] inclusive. All stage events for those candidates are
    returned, not only the events that occurred within the window.
    Maximum extraction window: no hard limit (use wide date ranges for backfill).
    """
    _check_auth(request)

    try:
        dt_from = date.fromisoformat(date_from)
        dt_to = date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(status_code=400, detail="date_from and date_to must be YYYY-MM-DD")
    if dt_to < dt_from:
        raise HTTPException(status_code=400, detail="date_to must be >= date_from")

    dt_to_exclusive = (dt_to + timedelta(days=1)).isoformat()

    cands = await db.candidates.find(
        {
            "$or": [
                {"created_at": {"$gte": date_from, "$lt": dt_to_exclusive}},
                {"updated_at": {"$gte": date_from, "$lt": dt_to_exclusive}},
            ]
        },
        {
            "_id": 0,
            "id": 1, "user_id": 1, "pipeline_id": 1,
            "first_name": 1, "last_name": 1, "email": 1, "phone": 1,
            "screening_status": 1,
            "attendance_status": 1,
            "created_at": 1, "updated_at": 1,
            "appointment_at": 1,
            "moved_to_form_at": 1,
            "moved_to_close_at": 1,
            "moved_to_training_at": 1,
            "training_start_at": 1,
            "training_attended": 1,
            "training_attended_at": 1,
            "archived_at": 1, "archived_reason": 1,
        },
    ).to_list(10000)

    if not cands:
        return []

    pipeline_ids = list({c["pipeline_id"] for c in cands if c.get("pipeline_id")})
    pipe_map: Dict[str, Dict[str, Any]] = {}
    async for p in db.pipelines.find(
        {"id": {"$in": pipeline_ids}},
        {"_id": 0, "id": 1, "name": 1, "created_at": 1, "cg1_office_key": 1},
    ):
        pipe_map[p["id"]] = p

    rows: List[Dict[str, Any]] = []
    for cand in cands:
        if not cand.get("id"):
            continue
        pipe = pipe_map.get(cand.get("pipeline_id") or "") or {}
        cand["_pipeline_name"] = pipe.get("name") or cand.get("pipeline_id") or "UNKNOWN"
        camp_ref = _campaign_ref(
            pipe.get("name") or "UNKNOWN",
            pipe.get("created_at") or "2020-01-01",
        )
        pin = _mc_pin(pipe.get("cg1_office_key") or "")
        rows.extend(_build_events(cand, camp_ref, pin))

    rows.sort(key=lambda r: r["stage_datetime"])
    return rows
