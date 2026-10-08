"""CGRecruit side of the Claude connector: who may sign in, and the read-only tools.

Scoping mirrors deps.py, but applied consistently (some app screens are looser):
  * super_admin - every pipeline in their tenant
  * recruiter   - only their assigned pipeline_ids
  * viewer      - their pipeline_ids, and only candidates they added themselves
  * analyst     - their pipeline_ids, aggregate tools only (Intelligence)
The shared demo login is refused at sign-in.

Candidate-level tools return what the CGRecruit screens show (name, phone,
email, CV text, transcripts) - an owner decision, 2026-10-08. Credentials never
leave: `public_token` unlocks the unauthenticated candidate portal, so it and
anything named like a token/secret/password is stripped from every record.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import statistics
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .core import LoginRefused, Tool, ToolContext, ToolError
from app_tz import app_zone, default_tz_name  # APP_TIMEZONE (noqa: F401)
from link_redaction import redact_magic_links

ET = app_zone()
STAGES = ["SCREENING", "APPOINTMENT", "FORM", "CLOSE", "TRAINING"]
ATTENDED = ("attended_form", "attended_no_form")
MAX_RANGE_DAYS = 400
# A bcrypt hash of a random string, so an unknown email costs the same time as
# a wrong password and the sign-in page can't be used to test which emails exist.
_DUMMY_HASH = __import__("bcrypt").hashpw(
    __import__("secrets").token_bytes(16), __import__("bcrypt").gensalt(rounds=12)
).decode("utf-8")
_SECRET_KEY = re.compile(r"token|secret|password", re.I)


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

def _scope(user: Dict[str, Any]) -> Dict[str, Any]:
    """Same normalisation as deps.current_user, minus its write-on-read."""
    role = user.get("role")
    if not user.get("parent_user_id") and role not in ("super_admin", "viewer"):
        role = "super_admin"
    return {
        "role": role,
        "tenant": user.get("parent_user_id") or user["id"],
        "auth_id": user["id"],
        "pipeline_ids": None if role == "super_admin" else list(user.get("pipeline_ids") or []),
    }


async def _pipelines(ctx: ToolContext) -> Dict[str, Dict[str, Any]]:
    s = _scope(ctx.user)
    q: Dict[str, Any] = {"user_id": s["tenant"]}
    if s["pipeline_ids"] is not None:
        q["id"] = {"$in": s["pipeline_ids"]}
    rows = await ctx.db.pipelines.find(q, {"_id": 0, "id": 1, "name": 1, "twilio_phone_number": 1,
                                            "appointment_duration_minutes": 1, "created_at": 1}).to_list(200)
    return {p["id"]: p for p in rows}


async def _resolve_pipelines(ctx: ToolContext, pipeline: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """The pipelines a question covers. Accepts an id or part of the name ("downtown")."""
    allowed = await _pipelines(ctx)
    if not allowed:
        raise ToolError("Your account isn't assigned to any pipeline yet.")
    if not pipeline:
        return allowed
    needle = pipeline.strip().lower()
    hit = {pid: p for pid, p in allowed.items() if pid == pipeline or needle in (p.get("name") or "").lower()}
    if not hit:
        names = ", ".join(sorted(p.get("name") or pid for pid, p in allowed.items()))
        raise ToolError(f"No pipeline you can see matches '{pipeline}'. Yours: {names}.")
    return hit


def _candidate_filter(ctx: ToolContext, pipeline_ids: List[str], *, individual: bool) -> Dict[str, Any]:
    s = _scope(ctx.user)
    if individual and s["role"] == "analyst":
        raise ToolError("Your account has Intelligence (aggregate) access only, so it can't open individual candidates.")
    q: Dict[str, Any] = {"user_id": s["tenant"], "pipeline_id": {"$in": pipeline_ids}}
    if individual and s["role"] == "viewer":
        q["added_by_user_id"] = s["auth_id"]
    return q


# ---------------------------------------------------------------------------
# Time helpers. CGRecruit stores timestamps as ISO strings (UTC offset).
# ---------------------------------------------------------------------------

def _parse(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _et_day_start(d: date) -> datetime:
    return datetime.combine(d, dtime(0, 0), tzinfo=ET).astimezone(timezone.utc)


def _range(args: Dict[str, Any], from_key: str, to_key: str, default_days: int) -> Tuple[date, date, str, str]:
    """Inclusive ET calendar dates -> UTC ISO bounds [lo, hi)."""
    today = datetime.now(ET).date()
    d_to = date.fromisoformat(args[to_key]) if args.get(to_key) else today
    d_from = date.fromisoformat(args[from_key]) if args.get(from_key) else d_to - timedelta(days=default_days - 1)
    if d_from > d_to:
        raise ToolError(f"'{from_key}' is after '{to_key}'.")
    if (d_to - d_from).days > MAX_RANGE_DAYS:
        raise ToolError(f"Date range is limited to {MAX_RANGE_DAYS} days.")
    lo = _et_day_start(d_from).isoformat()
    hi = _et_day_start(d_to + timedelta(days=1)).isoformat()
    return d_from, d_to, lo, hi


def _et(value: Any) -> Optional[str]:
    d = _parse(value)
    return d.astimezone(ET).strftime("%Y-%m-%d %H:%M ET") if d else None


def _week_of(d: datetime) -> str:
    local = d.astimezone(ET).date()
    return (local - timedelta(days=local.weekday())).isoformat()


def _pct(n: int, d: int) -> Optional[float]:
    return round(100.0 * n / d, 1) if d else None


def _fmt_minutes(m: Optional[float]) -> Optional[str]:
    if m is None:
        return None
    if m < 1:
        return f"{m * 60:.0f}s"
    if m < 120:
        return f"{m:.0f}m"
    if m < 48 * 60:
        return f"{m / 60:.1f}h"
    return f"{m / 1440:.1f}d"


def _delay_stats(minutes: List[float], population: int) -> Dict[str, Any]:
    if not minutes:
        return {"count": 0, "of": population}
    v = sorted(minutes)

    def q(p: float) -> float:
        return v[min(len(v) - 1, int(p * len(v)))]

    return {
        "count": len(v),
        "of": population,
        "median": _fmt_minutes(statistics.median(v)),
        "p75": _fmt_minutes(q(0.75)),
        "p90": _fmt_minutes(q(0.90)),
        "within_5_min_pct": _pct(sum(1 for m in v if m <= 5), len(v)),
        "within_1_hour_pct": _pct(sum(1 for m in v if m <= 60), len(v)),
        "median_minutes": round(statistics.median(v), 2),
    }


def _name(c: Dict[str, Any]) -> str:
    return " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x) or "(no name)"


def _clean(doc: Any) -> Any:
    """Drop Mongo ids and anything credential-shaped, recursively. That covers
    the candidate's portal token inside text too: sent emails and texts carry
    `/applicant/<token>`, `/reschedule/<token>` and `/retry/<token>` links."""
    if isinstance(doc, dict):
        return {k: _clean(v) for k, v in doc.items() if k != "_id" and not _SECRET_KEY.search(k)}
    if isinstance(doc, list):
        return [_clean(v) for v in doc]
    if isinstance(doc, str):
        return redact_magic_links(doc)
    return doc


def _successfully_screened(c: Dict[str, Any]) -> bool:
    # Same rule as /intelligence/cohort-week: we actually spoke with them.
    if c.get("screening_status") in ("approved", "appointment_pending", "rejected"):
        return True
    if c.get("last_call_voicemail"):
        return False
    return bool(c.get("verdict")) or c.get("last_call_status") == "completed"


def _exit_category(reason: Optional[str]) -> str:
    # Same buckets as the Intelligence "Exit pipeline" card (server.py).
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
    if "attended_no_form" in r:
        return "attended_no_form"
    if "reject" in r or "disqualif" in r or "failed" in r:
        return "rejected"
    if r == "manual" or "not a fit" in r or "not fit" in r:
        return "manual"
    return "other"


_PIPELINE_ARG = {"type": "string", "description": "Pipeline id or part of its name, e.g. 'Downtown'. Omit for every pipeline you can see."}


def _date_arg(desc: str) -> Dict[str, Any]:
    return {"type": "string", "format": "date", "description": desc + " (YYYY-MM-DD, Eastern time)."}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

async def list_pipelines(ctx: ToolContext, args: Dict[str, Any]) -> Any:
    pipes = await _pipelines(ctx)
    counts: Dict[str, Dict[str, int]] = {pid: {s: 0 for s in STAGES} for pid in pipes}
    if pipes:
        q = _candidate_filter(ctx, list(pipes), individual=False)
        q["archived_at"] = None
        async for row in ctx.db.candidates.aggregate([
            {"$match": q},
            {"$group": {"_id": {"p": "$pipeline_id", "s": "$stage"}, "n": {"$sum": 1}}},
        ]):
            pid, stage = row["_id"].get("p"), row["_id"].get("s") or "SCREENING"
            if pid in counts:
                counts[pid][stage] = counts[pid].get(stage, 0) + row["n"]
    return {
        "your_role": _scope(ctx.user)["role"],
        "pipelines": [{
            "id": pid,
            "name": p.get("name"),
            "calls_from": p.get("twilio_phone_number"),
            "interview_minutes": p.get("appointment_duration_minutes"),
            "active_candidates_by_stage": counts[pid],
        } for pid, p in pipes.items()],
    }


async def funnel(ctx: ToolContext, args: Dict[str, Any]) -> Any:
    pipes = await _resolve_pipelines(ctx, args.get("pipeline"))
    d_from, d_to, lo, hi = _range(args, "applied_from", "applied_to", 30)
    q = _candidate_filter(ctx, list(pipes), individual=False)
    q["created_at"] = {"$gte": lo, "$lt": hi}
    fields = ["pipeline_id", "created_at", "first_reply_at", "screening_status", "last_call_voicemail", "verdict",
              "last_call_status", "appointment_at", "attendance_status", "moved_to_form_at", "form_submitted_at",
              "moved_to_close_at", "moved_to_training_at", "training_attended", "stage", "archived_at", "hired"]
    rows = await ctx.db.candidates.find(q, {"_id": 0, **{f: 1 for f in fields}}).to_list(20000)
    now = datetime.now(timezone.utc)
    group_by = args.get("group_by")

    def key(c: Dict[str, Any]) -> str:
        if group_by == "week":
            d = _parse(c.get("created_at"))
            return _week_of(d) if d else "unknown"
        if group_by == "pipeline":
            return (pipes.get(c.get("pipeline_id")) or {}).get("name") or "unknown"
        return "all"

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for c in rows:
        groups.setdefault(key(c), []).append(c)

    def summarise(cs: List[Dict[str, Any]]) -> Dict[str, Any]:
        applied = len(cs)
        reached = sum(1 for c in cs if c.get("first_reply_at") or _successfully_screened(c))
        booked = [c for c in cs if c.get("appointment_at")]
        booked_past = [c for c in booked if (_parse(c["appointment_at"]) or now) < now]
        attended = sum(1 for c in cs if c.get("attendance_status") in ATTENDED)
        no_show = sum(1 for c in booked_past if c.get("attendance_status") == "no_show")
        to_form = sum(1 for c in cs if c.get("moved_to_form_at") or c.get("stage") in ("FORM", "CLOSE", "TRAINING") or c.get("form_submitted_at"))
        forms = sum(1 for c in cs if c.get("form_submitted_at"))
        to_close = sum(1 for c in cs if c.get("form_submitted_at") or c.get("moved_to_close_at") or c.get("stage") in ("CLOSE", "TRAINING"))
        training_booked = sum(1 for c in cs if c.get("moved_to_training_at") or c.get("stage") == "TRAINING" or c.get("hired"))
        training_attended = sum(1 for c in cs if c.get("training_attended"))
        return {
            "applied": applied,
            "reached": reached,
            "booked_interview": len(booked),
            "interview_already_happened": len(booked_past),
            "attended_interview": attended,
            "no_show_interview": no_show,
            "invited_to_form": to_form,
            "form_submitted": forms,
            "reached_close": to_close,
            "booked_training": training_booked,
            "attended_training": training_attended,
            "archived": sum(1 for c in cs if c.get("archived_at")),
            "rates_pct": {
                "reached_of_applied": _pct(reached, applied),
                "booked_of_reached": _pct(len(booked), reached),
                "attended_of_interviews_that_happened": _pct(attended, len(booked_past)),
                "training_attended_of_interview_attended": _pct(training_attended, attended),
                "training_attended_of_applied": _pct(training_attended, applied),
            },
        }

    return {
        "applied_between": [d_from.isoformat(), d_to.isoformat()],
        "pipelines": [p.get("name") for p in pipes.values()],
        "groups": {k: summarise(v) for k, v in sorted(groups.items())},
        "definitions": {
            "cohort": "Candidates who entered CGRecruit in the date range, followed to wherever they are now.",
            "reached": "Replied by text/chat/email, or we actually spoke on a call (voicemails don't count).",
            "attended_interview": "attendance_status attended_form or attended_no_form on their latest booking.",
            "attended_training": "training_attended - the real end state. The 'hired' flag is unused.",
            "recent_cohorts": "Recent applicants are still moving, so their later-stage numbers will rise.",
        },
    }


async def speed_to_contact(ctx: ToolContext, args: Dict[str, Any]) -> Any:
    pipes = await _resolve_pipelines(ctx, args.get("pipeline"))
    d_from, d_to, lo, hi = _range(args, "applied_from", "applied_to", 30)
    if (d_to - d_from).days > 120:
        raise ToolError("Speed to contact is limited to 120 days at a time.")
    q = _candidate_filter(ctx, list(pipes), individual=False)
    q["created_at"] = {"$gte": lo, "$lt": hi}
    cands = await ctx.db.candidates.find(q, {"_id": 0, "id": 1, "pipeline_id": 1, "created_at": 1, "first_reply_at": 1}).to_list(10000)
    ids = [c["id"] for c in cands]
    first: Dict[str, Dict[str, datetime]] = {i: {} for i in ids}

    def note(cid: str, kind: str, at: Optional[datetime]) -> None:
        if at is None or cid not in first:
            return
        f = first[cid]
        if kind not in f or at < f[kind]:
            f[kind] = at

    for i in range(0, len(ids), 1000):
        chunk = ids[i:i + 1000]
        async for m in ctx.db.communications.find({"candidate_id": {"$in": chunk}}, {"_id": 0, "candidate_id": 1, "type": 1, "status": 1, "created_at": 1}):
            if m.get("status") in ("failed", "error", "skipped"):
                continue
            at = _parse(m.get("created_at"))
            note(m["candidate_id"], m.get("type") or "other", at)
            note(m["candidate_id"], "any", at)
        async for cv in ctx.db.conversations.find({"candidate_id": {"$in": chunk}}, {"_id": 0, "candidate_id": 1, "created_at": 1, "status": 1, "duration_seconds": 1, "call_type": 1}):
            # Interview no-show revival calls are not first contact; counting
            # them once made the first-call tail look like eight days.
            if cv.get("call_type") not in (None, "screening"):
                continue
            at = _parse(cv.get("created_at"))
            note(cv["candidate_id"], "call", at)
            note(cv["candidate_id"], "any", at)
            if cv.get("status") == "completed" and (cv.get("duration_seconds") or 0) >= 30:
                note(cv["candidate_id"], "answered_call", at)

    def bucket(c: Dict[str, Any]) -> str:
        t = _parse(c.get("created_at"))
        if not t:
            return "unknown"
        local = t.astimezone(ET)
        if local.weekday() == 6:
            return "applied Sunday"
        return "applied Mon-Sat 9am-7pm" if 9 <= local.hour < 19 else "applied Mon-Sat evening/overnight"

    def block(cs: List[Dict[str, Any]]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for kind, label in (("any", "first_touch_any_channel"), ("sms", "first_text"), ("email", "first_email"),
                            ("call", "first_ai_screening_call"), ("answered_call", "first_real_phone_conversation")):
            mins = []
            for c in cs:
                at, created = first[c["id"]].get(kind), _parse(c.get("created_at"))
                if at and created:
                    mins.append(max(0.0, (at - created).total_seconds() / 60))
            out[label] = _delay_stats(mins, len(cs))
        replies = []
        for c in cs:
            r, created = _parse(c.get("first_reply_at")), _parse(c.get("created_at"))
            if r and created:
                replies.append(max(0.0, (r - created).total_seconds() / 60))
        out["first_reply_from_candidate"] = _delay_stats(replies, len(cs))
        out["never_contacted"] = sum(1 for c in cs if "any" not in first[c["id"]])
        return out

    def _pick(b: Dict[str, Any], n: int) -> Dict[str, Any]:
        return {"applicants": n, "first_touch_any_channel": b["first_touch_any_channel"], "first_ai_screening_call": b["first_ai_screening_call"]}

    by_pipeline: Dict[str, List[Dict[str, Any]]] = {}
    by_time: Dict[str, List[Dict[str, Any]]] = {}
    for c in cands:
        by_pipeline.setdefault((pipes.get(c.get("pipeline_id")) or {}).get("name") or "unknown", []).append(c)
        by_time.setdefault(bucket(c), []).append(c)
    return {
        "applied_between": [d_from.isoformat(), d_to.isoformat()],
        "applicants": len(cands),
        "overall": block(cands),
        "by_pipeline": {k: block(v) for k, v in sorted(by_pipeline.items())} if len(by_pipeline) > 1 else None,
        "by_time_applied": {k: _pick(block(v), len(v)) for k, v in sorted(by_time.items())},
        "notes": [
            "Clock starts when the applicant landed in CGRecruit, not when they clicked apply on Indeed.",
            "Chat-first pipelines text first and only call ~2 hours later if there's no reply, so many people are never called.",
            "No AI calls go out on Sunday; evening applicants are called the next morning.",
            "Only screening calls count; interview no-show revival calls are excluded.",
        ],
    }


async def interview_attendance(ctx: ToolContext, args: Dict[str, Any]) -> Any:
    pipes = await _resolve_pipelines(ctx, args.get("pipeline"))
    d_from, d_to, lo, hi = _range(args, "from", "to", 28)
    q = _candidate_filter(ctx, list(pipes), individual=False)
    q["$or"] = [{"appointment_at": {"$gte": lo, "$lt": hi}},
                {"attendance_history.appointment_at": {"$gte": lo, "$lt": hi}}]
    rows = await ctx.db.candidates.find(q, {"_id": 0, "id": 1, "pipeline_id": 1, "appointment_at": 1,
                                            "attendance_status": 1, "attendance_history": 1}).to_list(20000)
    lo_dt, hi_dt, now = _parse(lo), _parse(hi), datetime.now(timezone.utc)
    sessions: List[Tuple[datetime, str, str]] = []  # (when, pipeline_id, outcome)
    for c in rows:
        current = _parse(c.get("appointment_at"))
        seen = set()
        if current and lo_dt <= current < hi_dt:
            st = c.get("attendance_status")
            outcome = ("attended" if st in ATTENDED else "no_show" if st == "no_show"
                       else "upcoming" if current >= now else "unrecorded")
            sessions.append((current, c.get("pipeline_id"), outcome))
            seen.add(current)
        # Earlier bookings that were marked and then rebooked live in history.
        for h in c.get("attendance_history") or []:
            at = _parse(h.get("appointment_at"))
            if not at or at in seen or not (lo_dt <= at < hi_dt):
                continue
            seen.add(at)
            st = h.get("status")
            sessions.append((at, c.get("pipeline_id"), "attended" if st in ATTENDED else "no_show" if st == "no_show" else "unrecorded"))

    group_by = args.get("group_by")

    def key(s: Tuple[datetime, str, str]) -> str:
        local = s[0].astimezone(ET)
        name = (pipes.get(s[1]) or {}).get("name") or "unknown"
        if group_by == "day":
            return local.strftime("%Y-%m-%d %a")
        if group_by == "week":
            return "week of " + _week_of(s[0])
        if group_by == "pipeline":
            return name
        if group_by == "weekday_time":
            return f"{local.strftime('%a %H:%M')} | {name}"
        return f"{local.strftime('%Y-%m-%d %a %H:%M')} | {name}"

    groups: Dict[str, Dict[str, int]] = {}
    for s in sorted(sessions):
        g = groups.setdefault(key(s), {"booked": 0, "attended": 0, "no_show": 0, "unrecorded": 0, "upcoming": 0})
        g["booked"] += 1
        g[s[2]] += 1
    for g in groups.values():
        g["show_rate_pct"] = _pct(g["attended"], g["attended"] + g["no_show"])
    total = {k: sum(g[k] for g in groups.values()) for k in ("booked", "attended", "no_show", "unrecorded", "upcoming")}
    total["show_rate_pct"] = _pct(total["attended"], total["attended"] + total["no_show"])
    return {
        "interviews_between": [d_from.isoformat(), d_to.isoformat()],
        "total": total,
        "groups": groups,
        "notes": [
            "show_rate_pct = attended / (attended + no-show); unrecorded sessions are left out of it, so a high unrecorded count means the rate is uncertain.",
            "Times are Eastern. Rebooked candidates count once per session they were booked into.",
        ],
    }


async def exit_reasons(ctx: ToolContext, args: Dict[str, Any]) -> Any:
    pipes = await _resolve_pipelines(ctx, args.get("pipeline"))
    d_from, d_to, lo, hi = _range(args, "from", "to", 30)
    basis = args.get("date_basis")
    q = _candidate_filter(ctx, list(pipes), individual=False)
    field = "created_at" if basis == "applied" else "archived_at"
    q[field] = {"$gte": lo, "$lt": hi}
    rows = await ctx.db.candidates.find(q, {"_id": 0, "archived_at": 1, "archived_reason": 1, "pipeline_id": 1}).to_list(20000)
    archived = [r for r in rows if r.get("archived_at")]
    cats: Dict[str, int] = {}
    raw: Dict[str, int] = {}
    for r in archived:
        reason = r.get("archived_reason") or "(none)"
        cats[_exit_category(r.get("archived_reason"))] = cats.get(_exit_category(r.get("archived_reason")), 0) + 1
        raw[reason] = raw.get(reason, 0) + 1
    out = {
        "date_basis": "application date" if basis == "applied" else "date archived",
        "between": [d_from.isoformat(), d_to.isoformat()],
        "archived": len(archived),
        "by_category": dict(sorted(cats.items(), key=lambda kv: -kv[1])),
        "by_reason": dict(sorted(raw.items(), key=lambda kv: -kv[1])),
        "notes": [
            "Uncontactable is almost all max_attempts_no_contact: ~6 calls, ~3 texts, ~2-3 emails with no reply.",
            "About 1 in 5 'uncontactable' did talk to us once then went quiet - dropped mid-screening, not unreachable.",
        ],
    }
    if basis == "applied":
        out["applicants_in_cohort"] = len(rows)
        out["archived_pct_of_cohort"] = _pct(len(archived), len(rows))
    else:
        out["notes"].append("By date archived, a re-engage-by-text run re-stamps archived_at on people who don't reply, which can spike a period. Use date_basis='applied' for a clean read.")
    return out


async def call_activity(ctx: ToolContext, args: Dict[str, Any]) -> Any:
    pipes = await _resolve_pipelines(ctx, args.get("pipeline"))
    d_from, d_to, lo, hi = _range(args, "from", "to", 14)
    if (d_to - d_from).days > 120:
        raise ToolError("Call activity is limited to 120 days at a time.")
    s = _scope(ctx.user)
    calls = await ctx.db.conversations.find(
        {"user_id": s["tenant"], "created_at": {"$gte": lo, "$lt": hi}},
        {"_id": 0, "candidate_id": 1, "created_at": 1, "status": 1, "duration_seconds": 1, "call_type": 1},
    ).to_list(50000)
    cand_ids = list({c.get("candidate_id") for c in calls if c.get("candidate_id")})
    owner: Dict[str, str] = {}
    base = _candidate_filter(ctx, list(pipes), individual=False)
    for i in range(0, len(cand_ids), 2000):
        async for c in ctx.db.candidates.find({**base, "id": {"$in": cand_ids[i:i + 2000]}}, {"_id": 0, "id": 1, "pipeline_id": 1}):
            owner[c["id"]] = c["pipeline_id"]
    group_by = args.get("group_by")
    groups: Dict[str, Dict[str, Any]] = {}
    for c in calls:
        pid = owner.get(c.get("candidate_id"))
        at = _parse(c.get("created_at"))
        if not pid or not at:
            continue
        local = at.astimezone(ET)
        k = local.strftime("%Y-%m-%d %a") if group_by != "week" else "week of " + _week_of(at)
        if len(pipes) > 1:
            k += " | " + ((pipes.get(pid) or {}).get("name") or "unknown")
        g = groups.setdefault(k, {"dials": 0, "screening": 0, "no_show_revival": 0, "answered_30s_plus": 0, "no_answer_or_voicemail": 0, "failed": 0})
        g["dials"] += 1
        g["screening" if c.get("call_type") in (None, "screening") else "no_show_revival"] += 1
        st = c.get("status")
        if st == "completed" and (c.get("duration_seconds") or 0) >= 30:
            g["answered_30s_plus"] += 1
        elif st == "failed":
            g["failed"] += 1
        else:
            g["no_answer_or_voicemail"] += 1
    for g in groups.values():
        g["answer_rate_pct"] = _pct(g["answered_30s_plus"], g["dials"])
    return {
        "between": [d_from.isoformat(), d_to.isoformat()],
        "groups": dict(sorted(groups.items())),
        "notes": ["Each pipeline dials from its own number; a falling answer rate on one number with steady texting usually means carrier spam-labelling."],
    }


async def search_candidates(ctx: ToolContext, args: Dict[str, Any]) -> Any:
    pipes = await _resolve_pipelines(ctx, args.get("pipeline"))
    q = _candidate_filter(ctx, list(pipes), individual=True)
    if not args.get("include_archived"):
        q["archived_at"] = None
    if args.get("stage"):
        q["stage"] = args["stage"]
    att = args.get("attendance")
    if att == "attended":
        q["attendance_status"] = {"$in": list(ATTENDED)}
    elif att == "no_show":
        q["attendance_status"] = "no_show"
    elif att == "not_recorded":
        q["attendance_status"] = None
    if args.get("applied_from") or args.get("applied_to"):
        _, _, lo, hi = _range(args, "applied_from", "applied_to", 30)
        q["created_at"] = {"$gte": lo, "$lt": hi}
    text = (args.get("query") or "").strip()
    if text:
        digits = re.sub(r"\D", "", text)
        ors: List[Dict[str, Any]] = []
        if len(digits) >= 7 and len(digits) >= len(text.replace(" ", "")) - 4:
            ors.append({"phone": {"$regex": re.escape(digits[-10:])}})
        if "@" in text:
            ors.append({"email": {"$regex": re.escape(text), "$options": "i"}})
        words = [w for w in re.split(r"\s+", text) if w and not w.isdigit()]
        if words and "@" not in text:
            ors.append({"$and": [{"$or": [{"first_name": {"$regex": re.escape(w), "$options": "i"}},
                                          {"last_name": {"$regex": re.escape(w), "$options": "i"}}]} for w in words]})
        if not ors:
            raise ToolError("Search by name, phone number or email.")
        q["$or"] = ors
    limit = args["limit"]
    proj = {"_id": 0, "id": 1, "first_name": 1, "last_name": 1, "phone": 1, "email": 1, "pipeline_id": 1, "stage": 1,
            "screening_status": 1, "created_at": 1, "appointment_at": 1, "attendance_status": 1, "training_attended": 1,
            "training_start_at": 1, "archived_at": 1, "archived_reason": 1, "smart_score": 1, "source": 1, "first_reply_at": 1}
    rows = await ctx.db.candidates.find(q, proj).sort("created_at", -1).to_list(limit + 1)
    return {
        "count": min(len(rows), limit),
        "more_available": len(rows) > limit,
        "candidates": [{
            "id": c["id"],
            "name": _name(c),
            "phone": c.get("phone"),
            "email": c.get("email"),
            "pipeline": (pipes.get(c.get("pipeline_id")) or {}).get("name"),
            "stage": c.get("stage"),
            "screening_status": c.get("screening_status"),
            "applied": _et(c.get("created_at")),
            "replied": bool(c.get("first_reply_at")),
            "interview": _et(c.get("appointment_at")),
            "attendance": c.get("attendance_status"),
            "training_start": c.get("training_start_at"),
            "training_attended": bool(c.get("training_attended")),
            "archived_reason": c.get("archived_reason") if c.get("archived_at") else None,
            "smart_score": c.get("smart_score"),
            "source": c.get("source"),
        } for c in rows[:limit]],
    }


async def _one_candidate(ctx: ToolContext, candidate_id: str, projection: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    pipes = await _pipelines(ctx)
    q = _candidate_filter(ctx, list(pipes), individual=True)
    q["id"] = candidate_id
    cand = await ctx.db.candidates.find_one(q, projection or {"_id": 0})
    if not cand:
        raise ToolError("No candidate with that id that your account can see. Use search_candidates to find the id.")
    cand["_pipeline_name"] = (pipes.get(cand.get("pipeline_id")) or {}).get("name")
    return cand


async def get_candidate(ctx: ToolContext, args: Dict[str, Any]) -> Any:
    cand = await _one_candidate(ctx, args["candidate_id"])
    cid = cand["id"]
    calls = await ctx.db.conversations.find(
        {"candidate_id": cid},
        {"_id": 0, "created_at": 1, "status": 1, "duration_seconds": 1, "call_type": 1, "summary": 1, "suitability_score": 1, "is_dnd_retry": 1},
    ).sort("created_at", 1).to_list(200)
    comms = await ctx.db.communications.find(
        {"candidate_id": cid},
        {"_id": 0, "created_at": 1, "type": 1, "template_key": 1, "status": 1, "subject": 1, "body": 1, "error": 1},
    ).sort("created_at", 1).to_list(300)
    cand.pop("chat_log", None)  # in get_candidate_transcript, with calls and SMS merged in order
    pipeline = cand.pop("_pipeline_name", None)
    return {
        "candidate": _clean({**cand, "pipeline": pipeline}),
        "calls": _clean(calls),
        "messages_sent": _clean([{**m, "body": (m.get("body") or "")[:1500]} for m in comms]),
        "note": "For what the candidate and the assistant actually said (chat, SMS and call transcripts in order), use get_candidate_transcript.",
    }


async def get_candidate_transcript(ctx: ToolContext, args: Dict[str, Any]) -> Any:
    from transcript_service import merge, summarise

    cand = await _one_candidate(ctx, args["candidate_id"], {"_id": 0, "id": 1, "first_name": 1, "last_name": 1, "pipeline_id": 1, "chat_log": 1})
    cid = cand["id"]
    sms_rows = await ctx.db.training_sms_messages.find(
        {"candidate_id": cid}, {"_id": 0, "direction": 1, "body": 1, "timestamp": 1, "was_llm_reply": 1},
    ).sort("timestamp", 1).to_list(500)
    convs = await ctx.db.conversations.find(
        {"candidate_id": cid}, {"_id": 0, "id": 1, "created_at": 1, "status": 1, "duration_seconds": 1, "suitability_score": 1, "transcript": 1},
    ).sort("created_at", 1).to_list(100)
    turns = merge(cand.get("chat_log"), sms_rows, convs)
    return {"candidate": _name(cand), "pipeline": cand.get("_pipeline_name"), **summarise(turns), "turns": _clean(turns)}


async def upcoming_interviews(ctx: ToolContext, args: Dict[str, Any]) -> Any:
    pipes = await _resolve_pipelines(ctx, args.get("pipeline"))
    now = datetime.now(timezone.utc)
    q = _candidate_filter(ctx, list(pipes), individual=True)
    q["archived_at"] = None
    q["appointment_at"] = {"$gte": now.isoformat(), "$lt": (now + timedelta(days=args["days_ahead"])).isoformat()}
    rows = await ctx.db.candidates.find(q, {"_id": 0, "id": 1, "first_name": 1, "last_name": 1, "phone": 1, "email": 1,
                                            "pipeline_id": 1, "appointment_at": 1, "appointment_sms_confirmed": 1,
                                            "rescheduled": 1, "verdict": 1, "smart_score": 1, "screening_status": 1,
                                            "screened_channel": 1}).to_list(2000)
    sessions: Dict[str, List[Dict[str, Any]]] = {}
    for c in sorted(rows, key=lambda c: c.get("appointment_at") or ""):
        k = f"{_et(c.get('appointment_at'))} | {(pipes.get(c.get('pipeline_id')) or {}).get('name')}"
        sessions.setdefault(k, []).append({
            "id": c["id"], "name": _name(c), "phone": c.get("phone"), "email": c.get("email"),
            "confirmed_by_text": bool(c.get("appointment_sms_confirmed")), "rebooked": bool(c.get("rescheduled")),
            "screened_by": c.get("screened_channel"), "verdict": c.get("verdict"), "smart_score": c.get("smart_score"),
        })
    return {"from_now_days": args["days_ahead"], "booked": len(rows), "sessions": sessions}


TOOLS: List[Tool] = [
    Tool("list_pipelines", "List pipelines",
         "The recruiting pipelines (cities) your account can see, with how many active candidates sit in each stage right now. Start here if unsure which pipeline someone means.",
         list_pipelines),
    Tool("funnel", "Recruiting funnel",
         "Follow everyone who applied in a date range through the funnel: reached, booked an interview, attended, form, close, booked training, attended training (the real end state). Group by week or pipeline to compare.",
         funnel,
         {"pipeline": _PIPELINE_ARG,
          "applied_from": _date_arg("First application date to include; default 30 days ago"),
          "applied_to": _date_arg("Last application date to include; default today"),
          "group_by": {"type": "string", "enum": ["none", "week", "pipeline"], "default": "none", "description": "Split the cohort by application week or pipeline."}}),
    Tool("speed_to_contact", "Speed to contact",
         "How fast applicants are contacted after they apply: first text, email, AI screening call, first real phone conversation, and how fast they reply - with medians, p75/p90 and splits by city and by when they applied.",
         speed_to_contact,
         {"pipeline": _PIPELINE_ARG,
          "applied_from": _date_arg("First application date; default 30 days ago"),
          "applied_to": _date_arg("Last application date; default today")}),
    Tool("interview_attendance", "Interview attendance",
         "Group-interview bookings and show-up for sessions in a date range: booked, attended, no-show, not recorded, upcoming, and show rate. Group by session (default), day, week, weekday+time slot, or pipeline.",
         interview_attendance,
         {"pipeline": _PIPELINE_ARG,
          "from": _date_arg("First interview date; default 27 days ago"),
          "to": _date_arg("Last interview date; default today"),
          "group_by": {"type": "string", "enum": ["session", "day", "week", "weekday_time", "pipeline"], "default": "session"}}),
    Tool("exit_reasons", "Why candidates left",
         "Why candidates were archived (uncontactable, no-show, rejected, withdrawn...), by application date (default, the clean read) or by date archived (matches the app's Exit Pipeline card).",
         exit_reasons,
         {"pipeline": _PIPELINE_ARG,
          "from": _date_arg("Start date; default 30 days ago"),
          "to": _date_arg("End date; default today"),
          "date_basis": {"type": "string", "enum": ["applied", "archived"], "default": "applied"}}),
    Tool("call_activity", "AI call activity",
         "Outbound AI calls per day or week: dials, screening vs no-show revival calls, answered (30s+), no answer/voicemail, and answer rate per pipeline. Use it to spot a phone number being spam-labelled.",
         call_activity,
         {"pipeline": _PIPELINE_ARG,
          "from": _date_arg("Start date; default 13 days ago"),
          "to": _date_arg("End date; default today"),
          "group_by": {"type": "string", "enum": ["day", "week"], "default": "day"}}),
    Tool("search_candidates", "Find candidates",
         "Find candidates by name, phone or email, or list them by stage, attendance or application date. Returns contact details and where each one is. Excludes archived candidates unless include_archived is true.",
         search_candidates,
         {"query": {"type": "string", "description": "Name, phone number or email (partial is fine)."},
          "pipeline": _PIPELINE_ARG,
          "stage": {"type": "string", "enum": STAGES},
          "attendance": {"type": "string", "enum": ["attended", "no_show", "not_recorded"]},
          "applied_from": _date_arg("Applied on/after"),
          "applied_to": _date_arg("Applied on/before"),
          "include_archived": {"type": "boolean", "default": False},
          "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 25}}),
    Tool("get_candidate", "Candidate details",
         "Everything on one candidate: contact details, CV text and AI score, screening answers and verdict, appointment and attendance history, training, every call (status, length, summary) and every message we sent.",
         get_candidate,
         {"candidate_id": {"type": "string", "description": "The candidate id from search_candidates."}},
         required=["candidate_id"]),
    Tool("get_candidate_transcript", "Candidate conversation",
         "Everything the candidate and our assistant said, on every channel (web chat, SMS, phone calls), in time order.",
         get_candidate_transcript,
         {"candidate_id": {"type": "string", "description": "The candidate id from search_candidates."}},
         required=["candidate_id"]),
    Tool("upcoming_interviews", "Upcoming interviews",
         "Who is booked into upcoming group interviews, session by session, with phone/email and whether they confirmed by text.",
         upcoming_interviews,
         {"pipeline": _PIPELINE_ARG,
          "days_ahead": {"type": "integer", "minimum": 1, "maximum": 30, "default": 7}}),
]

def _instructions() -> str:
    import company_profile
    return f"""CGRecruit is {company_profile.company_name()}'s recruiting system. Applicants arrive from job-board application emails, the intake webhook or the apply page, get an instant text + email, are screened by AI chat or AI phone call, book a group interview, then a form, then a training block.
Reading the data correctly:
- The end state is ATTENDING TRAINING (training_attended). The 'hired' flag is unused - never report hires from it.
- Application dates are when the applicant landed in CGRecruit, not when they clicked apply on a job board.
- Recent cohorts are still moving through the funnel; don't read their low late-stage numbers as a drop.
- 'Uncontactable' exits are mostly max_attempts_no_contact; some of them did speak to us once, then went quiet.
- Exit counts by archived date can spike after a re-engage-by-text run; by application date is the clean read.
- AI calls include interview no-show revival calls; speed-to-contact only counts screening calls.
- All times are US Eastern. Tools are read-only: nothing here can change data, send messages or place calls.
Each person only sees the pipelines and candidates their own CGRecruit account can see."""


INSTRUCTIONS = _instructions()


class CGRecruitAdapter:
    product = "CGRecruit"
    instructions = INSTRUCTIONS
    tools = TOOLS

    def __init__(self, db: Any):
        self.db = db

    async def authenticate(self, email: str, password: str) -> Dict[str, Any]:
        from auth_service import verify_password

        user = await self.db.users.find_one({"email": email}, {"_id": 0})
        hashed = (user or {}).get("password_hash")
        ok = await asyncio.to_thread(verify_password, password, hashed or _DUMMY_HASH)
        if not user or not hashed or not ok:
            raise LoginRefused("That email and password don't match a CGRecruit account.")
        return user

    async def load_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        return await self.db.users.find_one({"id": user_id}, {"_id": 0})

    def refuse_reason(self, user: Dict[str, Any]) -> Optional[str]:
        if user.get("is_demo"):
            return "The shared demo login can't be connected to Claude."
        return None

    def fingerprint(self, user: Dict[str, Any]) -> str:
        # CGRecruit has no session versioning; a password change is the reset.
        return hashlib.sha256((user.get("password_hash") or "").encode("utf-8")).hexdigest()[:32]
