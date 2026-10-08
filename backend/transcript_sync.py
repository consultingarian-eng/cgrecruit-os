"""Fetch a screening back out of ElevenLabs and make the record whole again.

Three callers need this — the outcome hook before it accuses anyone of never
being screened, the late-recovery sweep, and any future repair — and they had
started to grow their own copies. One implementation, one set of rules:

  * only ever an upgrade. A recovered transcript may promote a call from
    "no_answer" to "completed"; nothing here re-opens a conversation the
    post-call processor legitimately closed.
  * the CONVERSATION only. Restoring a day-old call must never re-run
    screening outcomes on the candidate — that would fire rejection emails,
    slot-picker links and retry dials at people whose cases have moved on.

The summary and score are part of "whole". Restoring 30 turns while leaving
`suitability_score: 0` behind gives the drawer a full transcript under a header
reading "Suitability 0/100" — a real card, where the candidate had been
correctly rescored to 78 and the call still showed a zero. Both fields are
display-only (nothing in the backend decides on the conversation's copy), so
rewriting them costs one LLM call and buys a card that doesn't contradict
itself.
"""
import asyncio
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("transcript_sync")

# A call carrying at least this many of the candidate's own turns was a real
# two-sided conversation, whatever a premature conclusion filed it as.
_REAL_CONVERSATION_TURNS = 2


def normalise_turns(data: Optional[Dict[str, Any]]) -> list:
    """ElevenLabs' transcript shape → ours, dropping empty utterances."""
    turns = [
        {"role": x.get("role", ""), "text": (x.get("message") or x.get("text") or "").strip()}
        for x in ((data or {}).get("transcript") or [])
    ]
    return [t for t in turns if t["text"]]


async def recover_conversation(db, conv: Dict[str, Any], *, rescore: bool = True) -> bool:
    """Ask ElevenLabs for this conversation's transcript and store what comes
    back. Returns True if a transcript was recovered; `conv` is updated in
    place so callers can re-read it without another query."""
    el_id = conv.get("elevenlabs_conversation_id")
    if not el_id:
        return False

    from voice_service import fetch_elevenlabs_conversation

    loop = asyncio.get_event_loop()
    try:
        # Blocking `requests` under the hood — never on the event loop.
        data = await loop.run_in_executor(None, fetch_elevenlabs_conversation, el_id)
    except Exception as e:
        logger.warning(f"recover {el_id}: fetch failed: {e}")
        return False

    turns = normalise_turns(data)
    if not turns:
        return False  # ElevenLabs agrees there was nothing to hear

    duration = (
        ((data.get("metadata") or {}).get("call_duration_secs"))
        or data.get("duration_seconds")
        or conv.get("duration_seconds")
    )
    sets: Dict[str, Any] = {"transcript": turns, "duration_seconds": duration}
    two_sided = sum(1 for t in turns if t["role"] in ("user", "human")) >= _REAL_CONVERSATION_TURNS
    if two_sided:
        sets["status"] = "completed"

    if rescore and two_sided:
        try:
            cand = await db.candidates.find_one(
                {"id": conv.get("candidate_id")}, {"_id": 0, "first_name": 1, "job_id": 1}) or {}
            job = None
            if cand.get("job_id"):
                job = await db.jobs.find_one({"id": cand["job_id"]}, {"_id": 0, "title": 1})
            from ai_service import summarize_call_transcript
            summary = await summarize_call_transcript(
                turns, cand.get("first_name") or "", (job or {}).get("title") or "",
                duration_seconds=duration)
            sets["summary"] = summary.get("summary", "")
            sets["suitability_score"] = summary.get("suitability_score")
        except Exception as e:
            # A missing score is cosmetic; a missing transcript is not. Never
            # let the summariser's bad night cost us the recovery itself.
            logger.warning(f"recover {el_id}: rescore failed, storing transcript anyway: {e}")

    await db.conversations.update_one({"id": conv["id"]}, {"$set": sets})
    conv.update(sets)
    logger.info(
        f"recover {el_id}: restored {len(turns)} turns ({duration}s) for conversation "
        f"{conv.get('id')} — the post-call step never stored them"
    )
    return True
