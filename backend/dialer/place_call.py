"""Place an outbound AI screening call (the heaviest piece of the dialer).

Resolves per-pipeline overrides, enforces dual concurrency caps (per-pipeline
fairness + tenant-wide plan ceiling), and dispatches via either the managed
ElevenLabs route or the TwiML Bridge route depending on `pipeline.calling_mode`.

NOTE on circular imports: this module sits in a 3-way cycle with `queue.py`
and `follow_up.py` — `place_call_now` → `sched_call` (queue) → `place_call_now`
(via APScheduler), and `place_call_now` → `sched_call_followup` (follow_up) →
`place_call_now` (via the retry path). We break the cycle with deliberate
function-local imports (`from .queue import sched_call` inside the cap-hit
branch) — module-load order stays clean. Don't move these to the top of the
file or you'll get an ImportError at first run.
"""
import logging
import os
from datetime import timedelta
from typing import Any, Dict, Optional, Tuple

from . import state
from ._call_helpers import (
    build_dynamic_variables,
    compute_caps,
    is_blocked,
    is_dialer_disabled,
    resolve_agent_config,
    resolve_caller_id,
    should_skip_call,
)
from .state import now_utc, settings_for
from .window import next_in_window, parse_hhmm, parse_next_call_at, store_next_call_at
from app_tz import app_zone, default_tz_name  # APP_TIMEZONE (noqa: F401)

logger = logging.getLogger("auto_dialer")


async def _clear_next_call_at(db, candidate_id: str, user_id: str) -> None:
    """Used after we decide to skip — leave the candidate doc in a sane state.

    Every `should_skip_call` reason is terminal, so the ETA has to go: a row left
    at screening_status='queued' with a past next_call_at renders as "Calling
    soon" forever, is counted in the Queued tile, and gets re-armed by
    `_startup_queue_recovery` on every deploy.

    Only next_call_at. screening_status stays as it is — 'paused' is
    recruiter-owned state that the Call Queue renders as "pulled out manually"
    with a Resume button, so filing a machine skip there would blacklist a
    candidate the recruiter never touched. The candidate-initiated grant does go,
    though: the request it represents has been resolved, and a stale grant would
    let a later recruiter campaign dial straight through the call window."""
    await db.candidates.update_one(
        {"id": candidate_id, "user_id": user_id},
        {"$set": {
            "next_call_at": None,
            "candidate_initiated_call": False,
            "candidate_initiated_at": None,
            "updated_at": now_utc().isoformat(),
        }},
    )


async def _mark_call_initiated(
    db,
    candidate_id: str,
    user_id: str,
    cand: Dict[str, Any],
    result: Dict[str, Any],
) -> None:
    """Persist the post-dial candidate state. Same shape for both managed and
    twiml_bridge calling modes."""
    # DND retries are part of the same "set" — don't count them as a new attempt.
    # This keeps max_retry_attempts as a "sets" limit, not a raw-calls limit,
    # and prevents the idempotency check in schedule_dnd_retry from misfiring
    # if the DND retry itself hits voicemail (dnd_retry_for_attempt stays equal
    # to call_attempts, so a second DND retry is correctly suppressed).
    new_attempts = (cand.get("call_attempts") or 0) + (0 if cand.get("dnd_retry_active") else 1)
    await db.candidates.update_one(
        {"id": candidate_id, "user_id": user_id},
        {"$set": {
            "stage": "SCREENING",
            # A dial we could not place is a failed ATTEMPT, not a pending one.
            # Parking it at 'queued' with next_call_at None stranded it in a state
            # no bucket and no sweep matches — the Call Queue's queued bucket and
            # scheduler's orphan recovery both require next_call_at — so it sat on
            # the board in Screening and was never dialed again. 'no_answer' hands
            # it to the stranded-retry recovery, which re-enters the normal cadence.
            # The real provider status is preserved in last_call_status below.
            "screening_status": "in_progress" if result.get("status") == "initiated" else "no_answer",
            "call_attempts": new_attempts,
            "last_call_status": result.get("status"),
            "next_call_at": None,
            "dnd_retry_active": False,
            # The request has been answered — the next dial is a campaign dial
            # again and must go back through the call window. The stamp goes with
            # it so the NEXT grant starts its TTL from its own request.
            "candidate_initiated_call": False,
            "candidate_initiated_at": None,
            "updated_at": now_utc().isoformat(),
        }},
    )


def _schedule_followup_if_initiated(
    user_id: str,
    candidate_id: str,
    settings: Dict[str, Any],
    result: Dict[str, Any],
) -> None:
    """Lazy import to avoid the 3-way circular described in the module docstring."""
    if result.get("status") != "initiated":
        return
    from .follow_up import sched_call_followup
    ad = (settings.get("auto_dialer") or {})
    sync_min = int(ad.get("sync_after_call_minutes", 8))
    sched_call_followup(user_id, candidate_id, run_at=now_utc() + timedelta(minutes=sync_min))


async def _enforce_concurrency_cap(
    db,
    cand: Dict[str, Any],
    candidate_id: str,
    user_id: str,
    settings: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """If we're at cap, postpone the call and return the response. Returns None
    when the call is allowed to proceed."""
    pipe_cap, tenant_cap, postpone_secs = compute_caps(settings)
    if not cand.get("pipeline_id") or (pipe_cap <= 0 and tenant_cap <= 0):
        return None
    # Count only GENUINELY live calls (ghost-aware) so stale in_progress rows
    # whose call already ended don't silently eat cap slots. Uses the exact same
    # definition as the Call Queue display — see dialer.live_count. Both counts
    # are conversation-first: revival holds an ElevenLabs line without ever
    # setting screening_status='in_progress', so counting candidates alone lets
    # it overshoot the plan ceiling while the meter shows room. (Inbound calls
    # are invisible to both counts — live_count's docstring says why.)
    # Both exclude the candidate we're about to dial — his own unreconciled prior
    # conversation is the call we are replacing, not competition for a line.
    from .live_count import count_pipeline_live, count_tenant_live
    live_total = await count_tenant_live(db, user_id, exclude_candidate_id=candidate_id)
    live_pipe = await count_pipeline_live(
        db, user_id, cand["pipeline_id"], exclude_candidate_id=candidate_id,
    )
    blocked, reason = is_blocked(live_pipe, live_total, pipe_cap, tenant_cap)
    if not blocked:
        return None
    # Lazy import — see module docstring re: circulars.
    from .queue import sched_call
    run_at = now_utc() + timedelta(seconds=postpone_secs)
    sched_call(user_id, candidate_id, run_at=run_at)
    tz_name = (settings.get("region_language") or {}).get("timezone") or default_tz_name()
    next_call_at = store_next_call_at(run_at, tz_name)
    await db.candidates.update_one(
        {"id": candidate_id, "user_id": user_id},
        {"$set": {"next_call_at": next_call_at, "updated_at": now_utc().isoformat()}},
    )
    logger.info(
        f"concurrency cap hit ({reason}) for pipeline {cand['pipeline_id']} — "
        f"postponed {candidate_id} by {postpone_secs}s"
    )
    return {"status": "postponed", "reason": reason, "next_call_at": next_call_at}


NO_SLOT_POSTPONE_MINUTES = 60

# How long to park an armed dial while the auto-dialer master switch is off.
# Short enough that flipping the switch back on resumes calling without waiting
# for a deploy, long enough that a switch left off overnight isn't a busy loop.
DIALER_DISABLED_RECHECK_MINUTES = 15

# How long a DND-bypass grant stays valid. The redial itself fires ~25s out, so
# a flag older than this was stranded by a crash or a deploy rather than being a
# live bypass, and must not exempt the dial from the call window.
DND_BYPASS_TTL_MINUTES = 10

# Same idea for the candidate-initiated grant. Every postpone below re-arms
# through `sched_call` and deliberately keeps the flag, so without a TTL a "call
# me now" that hits the hourly no-slot re-check at 18:50 still carries its
# window bypass when a slot finally frees at 1am. Wide enough to survive a few
# concurrency postpones (batch_interval_seconds), far short of one no-slot hop.
CANDIDATE_BYPASS_TTL_MINUTES = 15

# The hours no bypass may cross, pipeline-local. A bypass excuses a dial from the
# recruiter's campaign window, not from being a call to a human being at 3am —
# and something has to: the public /retry/{token}/schedule-call endpoint accepts
# any timestamp up to 14 days out with no hour-of-day validation (the SMS path
# feeds it an LLM-parsed time), and a grant stranded by a failed dial is re-armed
# by `_startup_queue_recovery` at whatever hour the deploy happens.
HARD_DIAL_FLOOR_HHMM = "08:00"
HARD_DIAL_CEILING_HHMM = "21:00"


def _absolute_dial_bounds(ad: Dict[str, Any]) -> Tuple[str, str]:
    """Widest hours a bypassed dial may use: the humane floor/ceiling, stretched
    to the recruiter's own window wherever that window is wider still — a shop
    that calls until 22:00 has already said 21:30 is a fine time to ring, and
    clamping its DND redial to tomorrow morning would break the redial."""
    start = ad.get("call_window_start") or "09:00"
    end = ad.get("call_window_end") or "19:00"
    return (
        start if parse_hhmm(start) < parse_hhmm(HARD_DIAL_FLOOR_HHMM) else HARD_DIAL_FLOOR_HHMM,
        end if parse_hhmm(end) > parse_hhmm(HARD_DIAL_CEILING_HHMM) else HARD_DIAL_CEILING_HHMM,
    )


def _candidate_bypass_is_fresh(cand: Dict[str, Any]) -> bool:
    """True while the grant on the doc still stands for a request the candidate
    made recently. A missing stamp is the grant's first dial attempt —
    `schedule_call_with_window(force=True)` writes the flag when it arms the job
    and the stamp goes on below, before any postpone can re-arm it."""
    stamped_at = cand.get("candidate_initiated_at")
    if not stamped_at:
        return True
    parsed = parse_next_call_at(stamped_at, "UTC")
    if parsed is None:
        return True
    return parsed > now_utc() - timedelta(minutes=CANDIDATE_BYPASS_TTL_MINUTES)


def _dnd_bypass_is_fresh(cand: Dict[str, Any]) -> bool:
    """True only for a DND redial armed recently enough to still be the call it
    claims to be.

    Fails CLOSED on every unknown: `schedule_dnd_retry` writes
    `dnd_retry_armed_at` in the same `$set` as the flag, so a flag with no
    readable stamp is not a legacy doc one retry cycle old — it is a doc whose
    redial never completed (crash, deploy, a booking landing mid-arm), which is
    exactly the population the TTL was added to defuse. `dnd_retry_active` has
    no other clearer than a placed call, so treating those as fresh would hand
    them a permanent, unexpirable call-window bypass. A grant we cannot date is
    not a grant."""
    if not cand.get("dnd_retry_active"):
        return False
    parsed = parse_next_call_at(cand.get("dnd_retry_armed_at"), "UTC")
    if parsed is None:
        return False
    return parsed > now_utc() - timedelta(minutes=DND_BYPASS_TTL_MINUTES)


async def _enforce_slot_availability(
    db,
    cand: Dict[str, Any],
    candidate_id: str,
    user_id: str,
    pipe: Dict[str, Any],
    settings: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """The AI call's whole purpose is booking an appointment — dialing with
    zero offerable slots burns the candidate. If the pipeline has availability
    rules but no open slot in the next 5 days, postpone and re-check hourly;
    dialing resumes automatically once slots open up. Returns None when the
    call may proceed."""
    if not pipe or not pipe.get("availability_rules"):
        return None  # no schedule configured — legacy/manual booking, don't block
    from availability_service import compute_available_slots
    region = settings.get("region_language") or {}
    appt_settings = settings.get("appointments") or {}
    booked = await db.candidates.find(
        {"pipeline_id": pipe["id"], "appointment_at": {"$ne": None}},
        {"_id": 0, "appointment_at": 1},
    ).to_list(2000)
    slots = compute_available_slots(
        pipe,
        [c["appointment_at"] for c in booked if c.get("appointment_at")],
        days_ahead=5,
        tz_name=region.get("timezone") or default_tz_name(),
        default_capacity=int(appt_settings.get("applicant_limit") or 50),
        max_slots=1,
    )
    if slots:
        return None
    from .queue import sched_call
    run_at = now_utc() + timedelta(minutes=NO_SLOT_POSTPONE_MINUTES)
    sched_call(user_id, candidate_id, run_at=run_at)
    next_call_at = store_next_call_at(run_at, region.get("timezone") or default_tz_name())
    await db.candidates.update_one(
        {"id": candidate_id, "user_id": user_id},
        {"$set": {"next_call_at": next_call_at, "updated_at": now_utc().isoformat()}},
    )
    logger.warning(
        f"no bookable slots in pipeline {pipe.get('name') or pipe.get('id')} — "
        f"call for {candidate_id} postponed {NO_SLOT_POSTPONE_MINUTES}m; add availability to resume dialing"
    )
    return {
        "status": "postponed",
        "reason": "no bookable slots in the next 5 days — add availability to resume calls",
        "next_call_at": next_call_at,
    }


async def _dial_via_twiml_bridge(
    db,
    cand: Dict[str, Any],
    candidate_id: str,
    user_id: str,
    pipe: Dict[str, Any],
    sca: Dict[str, Any],
    agent: Dict[str, str],
    dyn: Dict[str, Any],
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    """TwiML Bridge mode: Twilio dials FROM our verified caller-id and we
    bridge audio to ElevenLabs over a WebSocket."""
    from twiml_bridge import place_outbound_call_via_twiml
    from models import Conversation

    from_number = resolve_caller_id(pipe, sca)
    if not from_number or not agent["agent_id"]:
        return {"status": "failed", "error": "twiml_bridge mode needs from_number + agent_id"}

    conv = Conversation(
        candidate_id=candidate_id, user_id=user_id,
        status="initiated", agent_id=agent["agent_id"],
        calling_mode="twiml_bridge", dynamic_variables=dyn,
        is_dnd_retry=bool(cand.get("dnd_retry_active")),
    )
    await db.conversations.insert_one(conv.model_dump())

    public_base = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
    result = place_outbound_call_via_twiml(
        from_number=from_number, to_number=cand["phone"],
        conversation_id=conv.id, public_base_url=public_base,
    )
    await db.conversations.update_one(
        {"id": conv.id, "user_id": user_id},
        {"$set": {
            "twilio_call_sid": result.get("call_sid"),
            "status": "initiated" if result.get("status") == "initiated" else "failed",
        }},
    )
    await _mark_call_initiated(db, candidate_id, user_id, cand, result)
    _schedule_followup_if_initiated(user_id, candidate_id, settings, result)
    return result


async def _dial_via_managed(
    db,
    cand: Dict[str, Any],
    candidate_id: str,
    user_id: str,
    agent: Dict[str, str],
    dyn: Dict[str, Any],
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    """Managed mode: ElevenLabs places the call from a Twilio-owned number it
    has registered. Lowest-latency path."""
    from voice_service import initiate_elevenlabs_outbound_call
    from models import Conversation

    # NOTE: see /api/candidates/{id}/call comment — overrides disabled at runtime, baked at sync.
    overrides: Dict[str, Any] = {}

    result = initiate_elevenlabs_outbound_call(
        candidate_phone=cand["phone"],
        agent_id=agent["agent_id"],
        agent_phone_number_id=agent["phone_number_id"],
        dynamic_variables=dyn,
        conversation_config_override=overrides or None,
    )
    # Same guard as the manual /call endpoint: ElevenLabs answers 2xx for
    # requests it never turns into a call, and the row that leaves behind has no
    # conversation_id — the key both the post-call webhook and
    # `_auto_sync_stuck_conversations` match on, so nothing can ever terminate
    # it. The candidate would show "On a call now" and hold a slot in both
    # concurrency counts for the full ghost cutoff. A dial we cannot identify is
    # a failed dial, not a live one.
    if result.get("status") == "initiated" and not result.get("conversation_id"):
        result = {**result, "status": "failed",
                  "error": "ElevenLabs accepted the request but returned no conversation_id"}
    conv = Conversation(
        candidate_id=candidate_id, user_id=user_id,
        elevenlabs_conversation_id=result.get("conversation_id"),
        twilio_call_sid=result.get("call_sid"),
        status="initiated" if result.get("status") == "initiated" else "failed",
        is_dnd_retry=bool(cand.get("dnd_retry_active")),
    ).model_dump()
    await db.conversations.insert_one(conv)
    await _mark_call_initiated(db, candidate_id, user_id, cand, result)
    _schedule_followup_if_initiated(user_id, candidate_id, settings, result)
    return result


async def place_call_now(
    user_id: str,
    candidate_id: str,
    candidate_initiated: bool = False,
) -> Dict[str, Any]:
    """Internal: actually place the AI call. Reuses the same logic as the manual /call endpoint.

    `candidate_initiated=True` marks a call the CANDIDATE asked for — "Call me
    now" on the retry portal, the same request by SMS or email reply, or a time
    they picked themselves. Those answer a request instead of running the
    recruiter's campaign, so they ignore the call window and the auto-dialer
    master switch, the same contract `queue.schedule_call_with_window` documents
    for `force=True`. Deferring them silently is worse than useless — all three
    callers tell the candidate the call is happening. Eligibility, concurrency
    caps and slot availability still apply."""
    if state._db is None:
        return {"status": "failed", "error": "db not initialized"}
    db = state._db

    cand = await db.candidates.find_one({"id": candidate_id, "user_id": user_id}, {"_id": 0})
    if not cand:
        return {"status": "failed", "error": "candidate not found"}

    # Dial-site backstop: the telephony APIs require E.164, but phone numbers
    # reach this point from six intake paths and several of them store the raw
    # string the candidate typed. "(617) 555-0134" dialled raw fails silently —
    # the candidate was promised a call and never gets one.
    if cand.get("phone"):
        from deps import normalize_phone_e164
        cand["phone"] = normalize_phone_e164(cand["phone"])

    skip, reason = should_skip_call(cand)
    if skip:
        # Every reason is terminal — no phone, archived, auto_dial off and past
        # screening are as permanent here as booked/finalised, so all of them
        # clear the ETA. Anything less leaves an undialable row in the Queued
        # list reading "Calling soon" forever.
        logger.info(f"skipping retry call for {candidate_id}: {reason}")
        await _clear_next_call_at(db, candidate_id, user_id)
        # Reasons starting with `__failed__:` map to "failed" status.
        if reason and reason.startswith("__failed__:"):
            return {"status": "failed", "error": reason.split(":", 1)[1]}
        return {"status": "skipped", "reason": reason}

    # A call the candidate booked for a specific time is armed minutes-to-days
    # ahead, and APScheduler carries none of our arguments across a restart, so
    # provenance lives on the doc: `schedule_call_with_window(force=...)` writes
    # the flag on both branches, which also means an ordinary recruiter re-queue
    # clears a stale grant. Time-bounded like the DND one — see the TTL.
    doc_grant = bool(cand.get("candidate_initiated_call"))
    candidate_initiated = candidate_initiated or (doc_grant and _candidate_bypass_is_fresh(cand))
    if doc_grant and not candidate_initiated:
        # The grant outlived the request it stood for. Drop it here rather than
        # leaving it for the next postpone to inherit, and take the window guard
        # below like any other campaign dial.
        await db.candidates.update_one(
            {"id": candidate_id, "user_id": user_id},
            {"$set": {"candidate_initiated_call": False, "candidate_initiated_at": None}},
        )
    # The DND redial is the 25-second continuation of a call the candidate just
    # took; deferring it destroys the 3-minute iOS "Allow Repeated Callers"
    # window the feature exists for. Bounded on its own stamp for the same reason:
    # `dnd_retry_active` is cleared only when the redial is actually placed, so a
    # crash in that ~25s gap strands it set, and an unbounded bypass would let a
    # later recovery dial ring someone at 3am.
    dnd_grant = bool(cand.get("dnd_retry_active"))
    dnd_fresh = _dnd_bypass_is_fresh(cand)
    if dnd_grant and not dnd_fresh:
        # Retire it off the doc rather than just ignoring it here. The flag does
        # three things — window bypass, `is_dnd_retry=true` (the agent hangs up
        # silently on voicemail) and suppressing the call_attempts increment —
        # so a stranded one turns every later dial into a silent call nobody
        # counts, which no amount of window checking fixes.
        await db.candidates.update_one(
            {"id": candidate_id, "user_id": user_id},
            {"$set": {"dnd_retry_active": False}},
        )
        cand["dnd_retry_active"] = False
    bypass_window = candidate_initiated or dnd_fresh

    settings = await settings_for(user_id, cand.get("pipeline_id"))
    sca = settings.get("screen_call_agent", {}) or {}
    profile = settings.get("recruiter_profile", {}) or {}
    pipe = await db.pipelines.find_one(
        {"id": cand.get("pipeline_id"), "user_id": user_id}, {"_id": 0},
    ) or {}

    # Fire-time half of the master switch: a recruiter who turns the dialer off
    # to stop it phoning people right now otherwise watches every already-armed
    # job dial anyway. The switch is temporary by design, so RE-ARM rather than
    # return — the job is a one-shot `date` trigger, and returning consumes it,
    # turning a pause into a silent purge only a deploy could undo.
    disabled, disabled_reason = is_dialer_disabled(settings, bypass_master=candidate_initiated)
    if disabled:
        from .queue import sched_call
        run_at = now_utc() + timedelta(minutes=DIALER_DISABLED_RECHECK_MINUTES)
        sched_call(user_id, candidate_id, run_at=run_at)
        tz_name = (settings.get("region_language") or {}).get("timezone") or default_tz_name()
        next_call_at = store_next_call_at(run_at, tz_name)
        await db.candidates.update_one(
            {"id": candidate_id, "user_id": user_id},
            {"$set": {"next_call_at": next_call_at, "updated_at": now_utc().isoformat()}},
        )
        logger.info(f"{disabled_reason} — call for {candidate_id} re-armed for {next_call_at}")
        return {"status": "postponed", "reason": disabled_reason, "next_call_at": next_call_at}

    # FIRE-TIME window guard. Scheduling clamps into the call window, but
    # postpone chains do not: the hourly no-slot re-check marches through
    # 19:00 into the night, and the moment the blocker clears (a candidate
    # frees a slot on the 24/7 self-serve portal at 1am) the next tick dialled
    # for real. Startup recovery after a night deploy had the same hole. One
    # re-check here, at the moment of dialling, closes every path at once —
    # the allocated hours (currently 09:00-19:00) are absolute. Absolute for the
    # recruiter's campaign, that is: `bypass_window` calls were asked for by the
    # candidate (or are the redial of a call he just took), and holding those to
    # tomorrow morning breaks a promise the portal/SMS/email already made. Those
    # still get the guard, just against the wider absolute bounds — every hour of
    # the day is reachable through a bypass, and none of them may be 3am.
    try:
        _ad = settings.get("auto_dialer") or {}
        _tz = (settings.get("region_language") or {}).get("timezone") or default_tz_name()
        if bypass_window:
            _start, _end = _absolute_dial_bounds(_ad)
            _days = [0, 1, 2, 3, 4, 5, 6]
            _out_reason = "outside allowed calling hours"
        else:
            _start = _ad.get("call_window_start") or "09:00"
            _end = _ad.get("call_window_end") or "19:00"
            _days = _ad.get("call_window_days") or [0, 1, 2, 3, 4, 5]
            _out_reason = "outside call window"
        _allowed_at = next_in_window(now_utc(), _tz, _start, _end, _days)
        if _allowed_at > now_utc() + timedelta(minutes=2):
            from .queue import sched_call
            sched_call(user_id, candidate_id, run_at=_allowed_at)
            _next_call_at = store_next_call_at(_allowed_at, _tz)
            await db.candidates.update_one(
                {"id": candidate_id, "user_id": user_id},
                {"$set": {"next_call_at": _next_call_at}},
            )
            logger.info(
                f"call for {candidate_id} {_out_reason} — rescheduled to {_next_call_at}")
            return {"status": "postponed", "reason": _out_reason, "next_call_at": _next_call_at}
    except Exception as e:
        # The guard must never break dialling itself — but log loudly.
        logger.warning(f"call-window fire-time guard failed for {candidate_id}: {e}")

    # The gates below re-arm the job through the scheduler, which drops our
    # arguments — persist the grant so a candidate-initiated call that waits on
    # a cap or a slot doesn't come back as an ordinary campaign dial and get
    # deferred to tomorrow morning after all. The stamp is what stops that from
    # being open-ended: it dates the request, is written once at its first dial
    # attempt, and is never refreshed by the postpones that re-arm the job.
    _stamped_at = cand.get("candidate_initiated_at")
    if candidate_initiated and not (_stamped_at and _candidate_bypass_is_fresh(cand)):
        await db.candidates.update_one(
            {"id": candidate_id, "user_id": user_id},
            {"$set": {"candidate_initiated_call": True,
                      "candidate_initiated_at": now_utc().isoformat()}},
        )

    # Postpone if at cap; abort the dial.
    cap_resp = await _enforce_concurrency_cap(db, cand, candidate_id, user_id, settings)
    if cap_resp is not None:
        return cap_resp

    # Postpone if there's nothing to offer — no point calling without a slot.
    slot_resp = await _enforce_slot_availability(db, cand, candidate_id, user_id, pipe, settings)
    if slot_resp is not None:
        return slot_resp

    # NOTE: retry_chat_last_at is deliberately NOT cleared here any more. It
    # used to be, so that someone genuinely reached by phone stopped getting
    # "you never finished your chat" nudges — but clearing at DIAL time meant
    # an unanswered fallback call silently killed the 2h/6h/24h text chase for
    # candidates who were mid-text-screening minutes earlier (a candidate answered two
    # gates, the 120-min fallback rang out, and no nudge ever came).
    # The clear now lives in follow_up, gated on the call actually connecting.

    job = None
    if cand.get("job_id"):
        job = await db.jobs.find_one({"id": cand["job_id"], "user_id": user_id}, {"_id": 0})

    agent = resolve_agent_config(pipe, sca)
    dyn = build_dynamic_variables(cand, pipe, profile, job, agent["agent_name"])

    calling_mode = (pipe.get("calling_mode") or "managed").lower()
    if calling_mode == "twiml_bridge":
        return await _dial_via_twiml_bridge(db, cand, candidate_id, user_id, pipe, sca, agent, dyn, settings)
    return await _dial_via_managed(db, cand, candidate_id, user_id, agent, dyn, settings)
