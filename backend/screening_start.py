"""The one place a newly-created candidate is put into screening.

Before v37 this logic was copy-pasted into all six places a candidate can be
created — the apply portal, the intake webhook, email intake, manual add and
resume upload. They had already drifted: two sent an
off-hours SMS and four didn't, each computed the warmup template slightly
differently, and every one of them wrapped the work in `except: logger.warning`,
so a failure left the candidate at `screening_status="pending"` with no
`next_call_at` — a state no sweep queries and no human sees: never called,
never texted, never archived.

So this module exists to be the single entry point, and it does three things the
copies did not:

  * honours `screening_mode`, which until now was read by nothing at all;
  * records WHY no call was scheduled on the candidate itself, so "the dialler
    never rang them" is answerable from the record instead of the log file;
  * never leaves a candidate in a state that looks healthy but isn't.

Order of operations matters: comms go out before the call is queued, because the
warmup text is what makes the call expected rather than cold, and under
chat_first it's the whole intervention.
"""
import logging
from typing import Any, Dict, Optional

from models import now_iso, resolve_screening_mode

logger = logging.getLogger("screening_start")

# Stamped on the candidate so an un-dialled candidate can be explained without
# going to the logs. Read by the pending-candidate sweep (see auto_dialer).
NO_DIAL_REASONS = {
    "chat_only": "screening_mode is chat_only — no screening calls are placed",
    "no_phone": "no phone number on the candidate",
    "auto_dial_off": "auto_dial is off for this candidate (usually a cross-pipeline duplicate)",
    "dialer_disabled": "the auto-dialler is switched off for this pipeline",
    "caller_opted_out": "the caller disabled dialling for this intake route",
    "error": "scheduling raised — see screening_start_error",
}


def arrival_template_key(settings: Dict[str, Any], mode: str) -> str:
    """Which template announces the candidate's arrival.

    Under voice_first this is the historic warmup / warmup_offhours pair, chosen
    by whether the *call* lands inside the window rather than whether the email
    does. Under the chat modes both are wrong — they promise a call in about ten
    minutes — so a dedicated template is used instead.
    """
    if mode in ("chat_first", "chat_only"):
        return "warmup_chat_first"
    from email_service import is_in_call_window

    sca = settings.get("screen_call_agent") or {}
    warmup_min = int(sca.get("warmup_delay_minutes", 10) or 10)
    return "warmup" if is_in_call_window(settings, ahead_minutes=warmup_min) else "warmup_offhours"


def dial_delay_minutes(settings: Dict[str, Any], mode: str) -> int:
    """How long after arrival the AI call should be attempted."""
    sca = settings.get("screen_call_agent") or {}
    if mode == "chat_first":
        # Long enough to give the text a real chance, short enough that the call
        # still lands the same working day. Clamped so a mis-typed 0 doesn't
        # quietly turn chat_first back into voice_first.
        return max(15, int(sca.get("chat_first_delay_minutes", 120) or 120))
    return int(sca.get("warmup_delay_minutes", 10) or 10)


async def start_screening(
    user_id: str,
    candidate: Dict[str, Any],
    settings: Dict[str, Any],
    *,
    send_comms: bool = True,
    allow_dial: bool = True,
    dial_opt_out_reason: str = "caller_opted_out",
) -> Dict[str, Any]:
    """Send the arrival comms and queue the screening call, per `screening_mode`.

    `allow_dial=False` is for callers with their own reason to stay quiet — the
    email intake's per-mailbox auto_dial
    setting. `send_comms=False` is for the manual-add `skip_warmup` flag.

    Never raises. Returns what happened, and stamps the same on the candidate.
    """
    # Imported late — deps.py builds the Mongo client at import time, and this
    # module is imported by route modules that deps.py itself pulls in.
    from deps import db, send_stage_comms, send_stage_email

    mode = resolve_screening_mode(settings)
    cand_id = candidate.get("id")
    result: Dict[str, Any] = {"mode": mode, "comms": None, "dial": None}

    # ── will a call actually be scheduled? ───────────────────────────────────
    # Decided BEFORE comms so the arrival copy can be honest. The old order
    # sent the warmup first ("You'll receive a call from [Agent Name] in about
    # [Call Delay Minutes] minutes.") and only then discovered no call would
    # ever be queued — cross-pipeline duplicates, email-only candidates and
    # dialler-off pipelines all cleared their next 15 minutes for a phone that
    # never rang, as their first impression of us.
    no_dial: Optional[str] = None
    if mode == "chat_only":
        no_dial = "chat_only"
    elif not allow_dial:
        no_dial = dial_opt_out_reason
    elif not candidate.get("phone"):
        no_dial = "no_phone"
    elif candidate.get("auto_dial") is False:
        no_dial = "auto_dial_off"
    elif not (settings.get("auto_dialer") or {}).get("enabled", True):
        no_dial = "dialer_disabled"

    # ── arrival comms ────────────────────────────────────────────────────────
    if send_comms:
        template_key = arrival_template_key(settings, mode)
        if no_dial and mode == "voice_first":
            # No call is coming, so the call-promising warmup pair is a lie.
            # The chat-first EMAIL leads with the self-serve chat link instead —
            # but only the email: its SMS opens with screening question 1, and
            # under voice_first the SMS thread is a concierge that must not run
            # the screening, so texting the question would break a different
            # promise. (The web chat link screens fine in every mode.)
            template_key = "warmup_chat_first"
        try:
            if mode == "voice_first" and template_key == "warmup":
                # In-hours under voice_first the SMS is deliberately withheld: the
                # pre-call text fires three minutes before the dial, and two texts
                # ten minutes apart reads as spam. Every other case wants both.
                result["comms"] = {"template": template_key,
                                   "sent": await send_stage_email(user_id, candidate, template_key)}
            elif no_dial and mode == "voice_first":
                result["comms"] = {"template": template_key,
                                   "sent": await send_stage_email(user_id, candidate, template_key)}
            else:
                result["comms"] = {"template": template_key,
                                   "sent": await send_stage_comms(user_id, candidate, template_key)}
        except Exception as e:
            logger.warning(f"start_screening: arrival comms failed for {cand_id}: {e}")
            result["comms"] = {"template": template_key, "error": str(e)}

    if no_dial is None:
        try:
            from auto_dialer import schedule_call_with_window

            sched = await schedule_call_with_window(
                user_id, cand_id, base_delay_minutes=dial_delay_minutes(settings, mode)
            )
            result["dial"] = sched
            # schedule_call_with_window has its own reasons to decline (the
            # dialler being off, mainly). Treat that as a no-dial too rather than
            # reporting a call that was never queued.
            if sched.get("status") != "scheduled":
                no_dial = "dialer_disabled"
        except Exception as e:
            logger.warning(f"start_screening: call scheduling failed for {cand_id}: {e}")
            result["dial"] = {"status": "failed", "error": str(e)}
            no_dial = "error"

    # ── leave a readable trail ───────────────────────────────────────────────
    # The point of this block: a candidate nobody ever rang should say so on the
    # record. Previously that information existed only as a log line, which is
    # why the 13 stranded candidates went unnoticed for months.
    update: Dict[str, Any] = {"screening_mode_used": mode}
    # One half of the speed-to-contact measurement. Stamped only when comms
    # actually went out, so a candidate we never messaged doesn't look like one
    # who ignored us. Never overwritten — this is the *first* time we reached out.
    if send_comms and result.get("comms") and not (result["comms"] or {}).get("error"):
        if not candidate.get("first_outreach_at"):
            update["first_outreach_at"] = now_iso()
    if no_dial:
        update["screening_no_dial_reason"] = no_dial
        if no_dial == "error":
            update["screening_start_error"] = str(result.get("dial", {}).get("error", ""))[:500]
    else:
        # Clear any reason left by an earlier attempt so a re-queue looks clean.
        update["screening_no_dial_reason"] = None
        update["screening_start_error"] = None
    try:
        await db.candidates.update_one({"id": cand_id, "user_id": user_id}, {"$set": update})
    except Exception as e:  # pragma: no cover — a failed stamp must not fail intake
        logger.warning(f"start_screening: could not stamp {cand_id}: {e}")

    result["no_dial_reason"] = no_dial
    logger.info(
        f"start_screening: {cand_id} mode={mode} "
        f"comms={(result.get('comms') or {}).get('template')} "
        f"dial={'-' if no_dial else (result.get('dial') or {}).get('scheduled_at')}"
        + (f" no_dial={no_dial}" if no_dial else "")
    )
    return result
