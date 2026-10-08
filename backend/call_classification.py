"""Shared post-call voicemail classification.

Three code paths classify a finished call — the ElevenLabs post-call webhook
(routes/webhooks.py), the auto-sync sweep (dialer/scheduler.py) and the manual
Sync endpoint (server.py) — and all need the same two answers: did we reach a
voicemail, and did a live human actually engage?

The ElevenLabs voicemail_detection system tool can MISFIRE mid-call on a live
human — most often during the silent pause while get_available_slots loads —
ending a real conversation with termination_reason=voicemail. Honouring that
flag blindly discards the screening verdict, stomps the candidate back to
no_answer and fires a DND redial at a person who was just talking to us.
`resolve_voicemail` therefore vetoes the flag whenever the transcript shows a
real back-and-forth.
"""
from typing import Any, Dict, List, Tuple

import company_profile

# Phrases that appear in voicemail greetings, which the ElevenLabs native
# voicemail tool transcribes as user turns.
VM_PHRASES = (
    "at the tone", "after the tone", "please leave", "leave a message",
    "not available", "please record", "record your message", "press 1 to leave",
)


def transcript_mentions_voicemail(transcript: List[Dict[str, Any]]) -> bool:
    """Scan the first few user turns for voicemail greeting phrases."""
    for turn in transcript[:5]:
        if turn.get("role") in ("user", "human"):
            txt = (turn.get("text") or "").lower()
            if any(p in txt for p in VM_PHRASES):
                return True
    return False


def count_real_user_turns(transcript: List[Dict[str, Any]]) -> int:
    """User turns that are non-empty and not voicemail-greeting boilerplate —
    i.e. evidence of a live human engaging with the agent."""
    n = 0
    for turn in transcript:
        if turn.get("role") not in ("user", "human"):
            continue
        txt = (turn.get("text") or "").strip()
        if not txt:
            continue
        if any(p in txt.lower() for p in VM_PHRASES):
            continue
        n += 1
    return n


def resolve_voicemail(voicemail_flag: bool, transcript: List[Dict[str, Any]]) -> Tuple[bool, bool]:
    """Final voicemail verdict for a call: (voicemail, misfired).

    Combines the platform flag (termination_reason) with the transcript phrase
    scan, then vetoes the result when the transcript shows a live conversation
    (2+ real user turns) — that's a voicemail_detection misfire, not a
    voicemail. Real voicemails produce at most one user turn (the greeting),
    which the phrase filter already discounts."""
    voicemail = bool(voicemail_flag) or transcript_mentions_voicemail(transcript)
    if voicemail and count_real_user_turns(transcript) >= 2:
        return False, True
    return voicemail, False


# ── one post-call outcome, whoever processes the call ───────────────────────
# FOUR different code paths file a finished screening call — the ElevenLabs
# post-call webhook, the auto-sync sweep, the TwiML-mode status handler and the
# +8-minute follow-up job — and each had grown its own is_complete threshold
# and its own verdict ladder. The same borderline call could be congratulated
# ("Great news — you passed!"), silently rejected, or texted "we missed you"
# depending on which processor won the race. These two functions are the single
# definition every path must use.

# 30 seconds, not 90: a 45-second answered call where the candidate said "can't
# talk right now" is a real (short) conversation. Filing it no_answer texted
# "we missed you on the screening call" to someone who picked up; letting the
# summariser run instead files it incomplete and retries with honest copy.
COMPLETE_CALL_MIN_SECONDS = 30

# Raw disqualification codes → the human-readable label stored on the
# candidate and rendered into the rejection template's [Disqualification
# Reason] variable.
DISQ_LABELS = {
    "age": "minimum age requirement (18+)",
    "work_authorization": "right to work in this country",
    "schedule_availability": f"schedule availability ({company_profile.get('work_schedule', 'the advertised hours')})",
    "commute": "commutability to our office",
    "withdrawn": "candidate withdrew / no longer interested",
}


def is_complete_call(candidate_spoke: bool, duration_seconds, voicemail: bool) -> bool:
    """Did a real conversation happen? One answer, shared by every classifier.

    Requires the candidate to have actually spoken (a transcript existing is
    not engagement — agent-only transcripts are the agent talking to a dead
    line), a minimum duration, and no voicemail verdict."""
    return bool(candidate_spoke) and (duration_seconds or 0) > COMPLETE_CALL_MIN_SECONDS and not voicemail


def screening_status_after_call(
    *, is_complete: bool, verdict, disq_reason, has_appointment: bool
) -> str:
    """The one verdict ladder.

    - no real conversation        → no_answer        (retry cadence)
    - hard gate failed            → rejected         (+ archive + rejection comms at the site)
    - slot booked during the call → approved
    - any real verdict — strong, good, borderline, review, WEAK — and no slot
      yet → appointment_pending   (slot-picker outreach; the hourly
      unbooked-screened sweep is the net for paths that don't send it)
    - no verdict / incomplete     → incomplete_info  (finish-online retry)

    Weak books on purpose: the agent's own prompt promises "you MUST always
    proceed to booking — the quality of their answers is for the human
    recruiter to judge, not you". The auto-sync sweep used to silently reject
    weak verdicts the webhook was congratulating.
    """
    if not is_complete:
        return "no_answer"
    if disq_reason:
        return "rejected"
    if has_appointment:
        return "approved"
    v = (verdict or "").strip().lower()
    if v and v != "incomplete":
        return "appointment_pending"
    return "incomplete_info"
