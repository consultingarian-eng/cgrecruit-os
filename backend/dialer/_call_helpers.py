"""Helpers for `dialer.place_call.place_call_now`. Pure functions kept here so
the orchestrator stays readable. No state — all dependencies are passed in
explicitly. Each function has a single responsibility:

- `should_skip_call`         : eligibility gate (already booked, finalised, etc.)
- `is_dialer_disabled`       : fire-time check of the auto-dialer master switch
- `check_concurrency_caps`   : dual cap math (per-pipeline + tenant-wide)
- `build_dynamic_variables`  : ElevenLabs `dynamic_variables` payload
- `resolve_caller_id`        : per-pipeline → global → default Twilio number
"""
from typing import Any, Dict, Optional, Tuple


SkipReason = Tuple[bool, Optional[str]]


_CALLABLE_STAGES = frozenset({"APPLICANT", "SCREENING", None, ""})


def should_skip_call(cand: Dict[str, Any]) -> SkipReason:
    """Pre-flight eligibility check. Returns (skip?, reason). Reasons match the
    legacy responses so callers/logs/tests don't change shape.

    Order matters — matches the historical sequence so behaviour stays identical:
        1. No phone → fail
        2. archived_at set → skip (soft-rejected, never dial)
        3. auto_dial=False → skip
        4. stage past screening (APPOINTMENT/FORM/CLOSE/TRAINING) → skip
        5. appointment_at set → skip (already booked through web)
        6. screening already finalised → skip
        7. screening paused → skip (recruiter pulled them out of the queue)
    """
    if not cand.get("phone"):
        return True, "__failed__:no phone"
    if cand.get("sms_opted_out"):
        # A standalone "stop" means all contact stops — texts AND calls. The
        # SMS layer already honours the flag; without this line the dialler
        # kept ringing people who had opted out in the plainest possible way.
        return True, "candidate opted out of contact"
    if cand.get("archived_at"):
        return True, "candidate archived"
    if not cand.get("auto_dial", True):
        return True, "auto_dial disabled for this candidate"
    stage = cand.get("stage") or ""
    if stage not in _CALLABLE_STAGES:
        return True, f"stage={stage} (past screening — no auto-dial)"
    if cand.get("appointment_at"):
        return True, "appointment already booked via web"
    finalised = cand.get("screening_status") in ("approved", "rejected")
    if finalised:
        return True, f"screening_status={cand.get('screening_status')}"
    # 'paused' was enforced in exactly one place (dialer.retry), so every other
    # path — the candidate-initiated callbacks especially — dialled straight
    # through a manual pause while the Call Queue still promised the recruiter
    # the candidate had been pulled out of the dial queue.
    if cand.get("screening_status") == "paused":
        return True, "dialing paused by recruiter"
    return False, None


def is_dialer_disabled(settings: Dict[str, Any], bypass_master: bool = False) -> SkipReason:
    """Fire-time half of the auto-dialer master switch. Returns (skip?, reason).

    `auto_dialer.enabled` is consulted only where calls are SCHEDULED, so a
    recruiter who flips it off to stop the AI phoning people right now still
    watches every already-armed job dial — the one scenario the control exists
    for. Fire time is the only moment that reflects the switch's current state.

    Unlike every `should_skip_call` reason this one is NOT terminal: the switch
    can be flipped back on, so callers must leave next_call_at alone rather than
    clearing it, keeping the row recoverable by the startup queue sweep.

    `bypass_master=True` is for candidate-initiated callbacks ("call me now",
    retry portal): the switch governs the recruiter's outbound campaign, not our
    ability to finish a conversation the candidate themselves just asked for.
    """
    if bypass_master:
        return False, None
    if not (settings.get("auto_dialer") or {}).get("enabled", True):
        return True, "auto_dialer disabled"
    return False, None


def compute_caps(settings: Dict[str, Any]) -> Tuple[int, int, int]:
    """Pull and normalise both concurrency ceilings + the postpone interval.
    Returns (pipe_cap, tenant_cap, postpone_secs)."""
    ad = (settings.get("auto_dialer") or {})
    pipe_cap = int(ad.get("max_concurrent_calls") or 5)
    tenant_cap = int(settings.get("tenant_max_concurrent_calls") or 5)
    postpone_secs = max(15, int(ad.get("batch_interval_seconds") or 30))
    return pipe_cap, tenant_cap, postpone_secs


def is_blocked(live_pipe: int, live_total: int, pipe_cap: int, tenant_cap: int) -> Tuple[bool, str]:
    """Decide whether to postpone given the current in-flight counts. Returns
    (blocked?, human_reason). Tenant cap takes priority in the reason string
    when both are hit (matches v34.2 behaviour)."""
    pipe_blocked = pipe_cap > 0 and live_pipe >= pipe_cap
    tenant_blocked = tenant_cap > 0 and live_total >= tenant_cap
    if not (pipe_blocked or tenant_blocked):
        return False, ""
    if tenant_blocked:
        return True, f"tenant cap {live_total}/{tenant_cap}"
    return True, f"pipeline cap {live_pipe}/{pipe_cap}"


def build_dynamic_variables(
    cand: Dict[str, Any],
    pipe: Dict[str, Any],
    profile: Dict[str, Any],
    job: Optional[Dict[str, Any]],
    agent_name: str,
) -> Dict[str, Any]:
    """Materialise the dict ElevenLabs interpolates into the system prompt and
    first message. Pure function — tests can drive this directly."""
    full_name = f"{cand.get('first_name', '')} {cand.get('last_name', '')}".strip()
    return {
        "first_name": cand.get("first_name", ""),
        "full_name": full_name,
        "role": (job or {}).get("title", ""),
        "company": profile.get("company_name", ""),
        "city": (pipe.get("name", "") or "").split(",")[0].strip(),
        "agent_name": agent_name,
        "phone": cand.get("phone", ""),
        "email": cand.get("email", ""),
        "pipeline_slug": pipe.get("public_slug", ""),
        # Resume context — populated from prior call transcripts / retry chat so the
        # agent can skip already-answered questions on retry calls.
        "previous_context": cand.get("previous_context", ""),
        "is_dnd_retry": "true" if cand.get("dnd_retry_active") else "false",
    }


def resolve_caller_id(pipe: Dict[str, Any], sca: Dict[str, Any]) -> str:
    """Pipeline-owned number → super-admin global override → TWILIO_PHONE_NUMBER."""
    from company_profile import default_twilio_number
    return (
        (pipe.get("twilio_phone_number") or "").strip()
        or (sca.get("custom_caller_id") or "").strip()
        or default_twilio_number()
    )


def resolve_agent_config(pipe: Dict[str, Any], sca: Dict[str, Any]) -> Dict[str, str]:
    """Pull the four ElevenLabs agent identifiers, applying per-pipeline overrides."""
    return {
        "agent_id": pipe.get("elevenlabs_agent_id_override") or sca.get("elevenlabs_agent_id", ""),
        "phone_number_id": pipe.get("elevenlabs_phone_number_id_override") or sca.get("elevenlabs_phone_number_id", ""),
        "voice_id": pipe.get("voice_id_override") or sca.get("voice_id", ""),
        "agent_name": pipe.get("agent_name_override") or sca.get("agent_name", "Olivia"),
    }
