"""AI Pipeline Insights — per-pipeline deep analysis using Claude.

POST /intelligence/ai-insights   → kicks off background job, returns {job_id}
GET  /intelligence/ai-insights/{job_id} → polls for {status, result, error}
"""
import uuid
from datetime import datetime, timezone, timedelta
from typing import Dict, Any

from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends, Body

from deps import db, logger, current_user, resolve_settings, assert_pipeline_access
from ai_service import analyze_pipeline_insights

router = APIRouter()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.post("/intelligence/ai-insights")
async def start_ai_insights(
    background_tasks: BackgroundTasks,
    payload: Dict[str, Any] = Body(default={}),
    user: dict = Depends(current_user),
):
    pipeline_id = payload.get("pipeline_id")
    if not pipeline_id:
        raise HTTPException(400, "pipeline_id required")

    await assert_pipeline_access(user, pipeline_id)
    pipeline = await db.pipelines.find_one({"id": pipeline_id, "user_id": user["id"]}, {"_id": 0})
    if not pipeline:
        raise HTTPException(404, "Pipeline not found")

    job_id = uuid.uuid4().hex
    await db.ai_insight_jobs.insert_one({
        "id": job_id,
        "user_id": user["id"],
        "pipeline_id": pipeline_id,
        "status": "running",
        "result": None,
        "error": None,
        "created_at": _now(),
        "completed_at": None,
    })

    background_tasks.add_task(_run_insights_job, job_id, user["id"], pipeline_id, pipeline)
    return {"job_id": job_id}


@router.get("/intelligence/ai-insights/latest")
async def get_latest_ai_insights(
    pipeline_id: str,
    user: dict = Depends(current_user),
):
    """Return the most recent job for this pipeline (any status) so the
    frontend can resume polling a running job after a page refresh."""
    await assert_pipeline_access(user, pipeline_id)
    job = await db.ai_insight_jobs.find_one(
        {"user_id": user["id"], "pipeline_id": pipeline_id},
        {"_id": 0},
        sort=[("created_at", -1)],
    )
    return job or {}


@router.get("/intelligence/ai-insights/{job_id}")
async def get_ai_insights(job_id: str, user: dict = Depends(current_user)):
    job = await db.ai_insight_jobs.find_one({"id": job_id, "user_id": user["id"]}, {"_id": 0})
    if not job:
        raise HTTPException(404, "Job not found")
    await assert_pipeline_access(user, job.get("pipeline_id") or "")
    return job


async def _run_insights_job(job_id: str, user_id: str, pipeline_id: str, pipeline: Dict[str, Any]):
    try:
        data = await _gather_pipeline_data(user_id, pipeline_id, pipeline)
        result = await analyze_pipeline_insights(data)
        await db.ai_insight_jobs.update_one(
            {"id": job_id},
            {"$set": {"status": "complete", "result": result, "completed_at": _now()}},
        )
    except Exception as e:
        logger.exception(f"AI insights job {job_id} failed: {e}")
        await db.ai_insight_jobs.update_one(
            {"id": job_id},
            {"$set": {"status": "error", "error": str(e), "completed_at": _now()}},
        )


async def _gather_pipeline_data(user_id: str, pipeline_id: str, pipeline: Dict[str, Any]) -> Dict[str, Any]:
    ninety_days_ago = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()

    # ── Candidates ────────────────────────────────────────────────────────────
    all_cands = await db.candidates.find(
        {"user_id": user_id, "pipeline_id": pipeline_id},
        {"_id": 0},
    ).to_list(3000)

    recent = [c for c in all_cands if (c.get("created_at") or "") >= ninety_days_ago]
    archived = [c for c in recent if c.get("archived_at")]
    active = [c for c in recent if not c.get("archived_at")]
    total = len(recent)

    by_stage: Dict[str, int] = {}
    for c in active:
        s = c.get("stage", "SCREENING")
        by_stage[s] = by_stage.get(s, 0) + 1

    archived_reasons: Dict[str, int] = {}
    for c in archived:
        r = c.get("archived_reason") or "unspecified"
        archived_reasons[r] = archived_reasons.get(r, 0) + 1

    verdicts: Dict[str, int] = {}
    disq_reasons: Dict[str, int] = {}
    for c in recent:
        v = c.get("verdict")
        if v:
            verdicts[v] = verdicts.get(v, 0) + 1
        d = c.get("disqualification_reason")
        if d:
            disq_reasons[d] = disq_reasons.get(d, 0) + 1

    screening_statuses: Dict[str, int] = {}
    for c in recent:
        s = c.get("screening_status", "pending")
        screening_statuses[s] = screening_statuses.get(s, 0) + 1

    screened = sum(1 for c in recent if c.get("verdict") in ("strong", "good", "borderline", "weak"))
    booked = sum(1 for c in recent if c.get("appointment_at"))
    attended = sum(1 for c in recent if c.get("attendance_status") in ("attended_form", "attended_no_form"))
    no_show = sum(1 for c in recent if c.get("attendance_status") == "no_show")
    form_submitted = sum(1 for c in recent if c.get("form_submitted_at"))
    hired = sum(1 for c in recent if c.get("hired"))
    training = sum(1 for c in recent if c.get("training_start_at"))

    def pct(num, denom):
        return f"{round(num / denom * 100)}%" if denom else "N/A (no data)"

    # ── Conversations ─────────────────────────────────────────────────────────
    # Use only the 90-day recent candidate IDs to keep the $in query small
    recent_cand_ids = {c["id"] for c in recent}
    convs = await db.conversations.find(
        {"user_id": user_id, "candidate_id": {"$in": list(recent_cand_ids)}},
        {"_id": 0, "candidate_id": 1, "status": 1, "duration_seconds": 1,
         "transcript": 1, "summary": 1, "suitability_score": 1, "created_at": 1},
    ).sort("created_at", -1).to_list(100)

    completed_convs = [cv for cv in convs if cv.get("status") == "completed"]
    durations = [cv["duration_seconds"] for cv in completed_convs if cv.get("duration_seconds")]
    avg_duration = round(sum(durations) / len(durations)) if durations else None
    scores = [cv["suitability_score"] for cv in completed_convs if cv.get("suitability_score") is not None]
    avg_score = round(sum(scores) / len(scores), 1) if scores else None

    # Build verdict → candidate map for sampling
    cand_verdict_map = {c["id"]: c.get("verdict") for c in recent}

    # Sample call summaries by verdict bucket (up to 5 each)
    conv_by_verdict: Dict[str, list] = {"strong": [], "good": [], "borderline": [], "weak": [], "incomplete": []}
    for cv in convs:
        if not cv.get("summary"):
            continue
        v = cand_verdict_map.get(cv.get("candidate_id")) or "incomplete"
        bucket = conv_by_verdict.get(v)
        if bucket is not None and len(bucket) < 5:
            bucket.append({
                "summary": cv["summary"],
                "suitability_score": cv.get("suitability_score"),
                "duration_seconds": cv.get("duration_seconds"),
            })

    # Raw transcript excerpts for weak/borderline calls (Claude looks for AI agent issues)
    raw_transcript_samples = []
    for cv in convs:
        v = cand_verdict_map.get(cv.get("candidate_id"))
        if v in ("weak", "borderline") and cv.get("transcript") and len(raw_transcript_samples) < 4:
            lines = "\n".join(
                f"{t.get('role', '?')}: {t.get('text', '')}"
                for t in (cv.get("transcript") or [])
            )[:3000]
            raw_transcript_samples.append({
                "verdict": v,
                "transcript_excerpt": lines,
                "duration_seconds": cv.get("duration_seconds"),
            })

    # ── Communications ────────────────────────────────────────────────────────
    comms = await db.communications.find(
        {"user_id": user_id, "candidate_id": {"$in": list(recent_cand_ids)}},
        {"_id": 0, "type": 1, "template_key": 1, "status": 1},
    ).to_list(3000)

    comms_by_template: Dict[str, int] = {}
    failed_comms = 0
    for comm in comms:
        key = comm.get("template_key") or "unknown"
        comms_by_template[key] = comms_by_template.get(key, 0) + 1
        if comm.get("status") == "failed":
            failed_comms += 1

    # ── Opt-outs ──────────────────────────────────────────────────────────────
    phones = list({c.get("phone", "") for c in recent if c.get("phone")})
    opt_out_count = await db.sms_optouts.count_documents({"phone_number": {"$in": phones}}) if phones else 0

    # ── Settings (screening questions) ────────────────────────────────────────
    settings = await resolve_settings(user_id, pipeline_id)
    sca = (settings or {}).get("screen_call_agent") or {}
    screening_questions = (sca.get("screening_questions") or [])[:15]

    # ── Jobs ──────────────────────────────────────────────────────────────────
    jobs = await db.jobs.find(
        {"pipeline_id": pipeline_id, "user_id": user_id},
        {"_id": 0, "title": 1},
    ).to_list(10)

    return {
        "pipeline_name": pipeline.get("name", "Unknown"),
        "job_titles": [j.get("title", "") for j in jobs],
        "lookback_days": 90,
        "funnel": {
            "total_candidates": total,
            "active": len(active),
            "archived": len(archived),
            "by_stage": by_stage,
            "archived_reasons": archived_reasons,
            "opt_out_count": opt_out_count,
            "opt_out_rate": pct(opt_out_count, total),
        },
        "screening": {
            "verdicts": verdicts,
            "disqualification_reasons": disq_reasons,
            "statuses": screening_statuses,
            "avg_suitability_score": avg_score,
            "avg_call_duration_seconds": avg_duration,
            "total_calls_attempted": len(convs),
            "completed_calls": len(completed_convs),
            "call_completion_rate": pct(len(completed_convs), len(convs)),
        },
        "conversions": {
            "apply_to_screened": pct(screened, total),
            "screened_to_booked": pct(booked, screened),
            "booked_to_attended": pct(attended, booked),
            "no_show_rate": pct(no_show, booked),
            "attended_to_form_submitted": pct(form_submitted, attended),
            "form_to_hired": pct(hired, form_submitted),
            "hired_to_training": pct(training, hired),
            "overall_apply_to_training": pct(training, total),
            "raw": {
                "total": total, "screened": screened, "booked": booked,
                "attended": attended, "no_show": no_show,
                "form_submitted": form_submitted, "hired": hired, "training": training,
            },
        },
        "call_samples": conv_by_verdict,
        "raw_transcript_samples": raw_transcript_samples,
        "communications": {
            "by_template": comms_by_template,
            "failed_count": failed_comms,
            "total": len(comms),
        },
        "screening_questions": screening_questions,
    }
