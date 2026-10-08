"""One transcript for a candidate, whichever channels they used.

A candidate's words currently live in three unrelated stores. Phone calls go to
`conversations.transcript`, the web chat to `candidates.chat_log`, and SMS to
`training_sms_messages` — different collections, different field names, and
different ideas of who said what (`role` vs `direction`). Only the first was
ever shown in the ATS, so a recruiter could listen back to a call but had no way
at all to read a chat, and no way to see that the two were part of one
conversation.

That was tolerable while the phone was the real screening. Now that most people
will be screened by text, "the recruiter cannot read the screening" is the whole
product.

So this merges the three into one chronological list and labels each turn with
where it happened. Pure functions — the endpoint fetches, this shapes.
"""
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("transcript")

CALL, CHAT, SMS, SYSTEM = "call", "chat", "sms", "system"

# The candidate is "them" on every channel; everything else is us. Each store
# spells this differently, which is exactly why nothing could merge them before.
_CANDIDATE_ROLES = {"applicant", "candidate", "user", "human"}


def _ts(value: Any) -> str:
    """A sortable timestamp string, normalised to UTC. Missing sorts first, not
    last — an entry with no time is almost always an early system note, and
    burying it at the end would put "Resume received via email" after the
    interview was booked.

    The UTC normalisation is load-bearing: SMS rows are stamped in Eastern
    (-04:00) and chat turns in UTC, and comparing the raw ISO strings ordered
    a 9:16pm ET text before a 10pm UTC chat message that actually preceded it
    by hours. Any candidate who switches channels mid-screening hits this."""
    from datetime import timezone

    from models import parse_appointment_at

    dt = parse_appointment_at(value)
    return dt.astimezone(timezone.utc).isoformat() if dt else ""


def _turn(channel: str, who: str, text: str, at: Any, **meta) -> Dict[str, Any]:
    return {"channel": channel, "who": who, "text": (text or "").strip(),
            "at": _ts(at), "meta": {k: v for k, v in meta.items() if v is not None}}


def from_chat_log(chat_log: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`candidates.chat_log` — the web chat, plus system notes.

    System notes ("Resume received via email…") are written with role=agent and
    are not conversation. They are kept but labelled, because a recruiter reading
    this wants the story, and losing them would make some threads inexplicable —
    they just must not be mistaken for something the assistant said to a person.
    """
    out = []
    for m in chat_log or []:
        role = (m.get("role") or "").lower()
        channel = (m.get("channel") or "").lower()
        if channel in ("system", "reschedule") or (channel == "" and role == "agent"):
            out.append(_turn(SYSTEM, "system", m.get("text"), m.get("at")))
            continue
        who = "candidate" if role in _CANDIDATE_ROLES else "assistant"
        out.append(_turn(SMS if channel == "sms" else CHAT, who, m.get("text"), m.get("at")))
    return out


def from_sms_messages(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`training_sms_messages` — direction in/out rather than a role."""
    return [
        _turn(
            SMS,
            "candidate" if (r.get("direction") == "in") else "assistant",
            r.get("body"),
            r.get("timestamp"),
            ai=r.get("was_llm_reply") or None,
        )
        for r in rows or []
    ]


def from_conversations(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`conversations` — phone calls, each with its own nested transcript.

    Turns inside a call carry no timestamps of their own, so they are anchored to
    the call's start and kept in order. A call is emitted even when its transcript
    is empty, because "we rang and nobody spoke" is information a recruiter wants
    — that is most calls that never connect.
    """
    out = []
    for conv in rows or []:
        started = conv.get("created_at")
        out.append(_turn(
            CALL, "system",
            f"Call — {conv.get('status') or 'unknown'}"
            + (f", {int(conv['duration_seconds'])}s" if conv.get("duration_seconds") else ""),
            started,
            conversation_id=conv.get("id"),
            duration_seconds=conv.get("duration_seconds"),
            suitability_score=conv.get("suitability_score"),
            call_marker=True,
        ))
        for i, t in enumerate(conv.get("transcript") or []):
            role = (t.get("role") or "").lower()
            out.append(_turn(
                CALL,
                "candidate" if role in _CANDIDATE_ROLES else "assistant",
                t.get("text") or t.get("message"),
                started,
                seq=i,
                conversation_id=conv.get("id"),
            ))
    return out


def merge(
    chat_log: Optional[List[Dict[str, Any]]] = None,
    sms_rows: Optional[List[Dict[str, Any]]] = None,
    conversations: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Everything the candidate and the assistant said, oldest first."""
    turns = from_chat_log(chat_log) + from_sms_messages(sms_rows) + from_conversations(conversations)
    turns = [t for t in turns if t["text"]]
    # Secondary key keeps a call's turns in spoken order — they all share the
    # call's start time, so a plain timestamp sort would scramble them.
    turns.sort(key=lambda t: (t["at"], t["meta"].get("seq", -1)))
    return turns


def summarise(turns: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Counts for the header, so a recruiter can see the shape before reading.

    The count key is `turn_count`, NOT `turns` — the endpoint returns
    `{"turns": <the list>, **summarise(...)}`, and when this dict also carried
    a "turns" key the unpack silently replaced the entire transcript with an
    integer. The drawer showed "Nothing said yet" for a candidate with a
    21-turn screening on the very first live night."""
    channels: Dict[str, int] = {}
    for t in turns:
        channels[t["channel"]] = channels.get(t["channel"], 0) + 1
    return {
        "turn_count": len(turns),
        "by_channel": channels,
        "candidate_replies": sum(1 for t in turns if t["who"] == "candidate"),
        "channels_used": sorted(c for c in channels if c != SYSTEM),
    }


def screening_thread(
    chat_log: Optional[List[Dict[str, Any]]],
    sms_rows: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """The candidate-facing screening conversation, across web chat AND SMS,
    in the retry page's message shape: [{role, channel, text, at}], UTC-sorted.

    A candidate who starts by text and taps into the web chat later must land
    in the SAME conversation — until this existed the page only loaded the
    web-chat turns, so it greeted them from scratch and re-asked question 1
    at someone who was three gates in over SMS."""
    msgs: List[Dict[str, Any]] = []
    for m in chat_log or []:
        if (m.get("channel") or "") != "retry":
            continue
        msgs.append({
            "role": m.get("role") or "agent",
            "channel": "retry",
            "text": m.get("text") or "",
            "at": _ts(m.get("at")),
        })
    for r in sms_rows or []:
        text = (r.get("body") or "").strip()
        if not text:
            continue
        msgs.append({
            "role": "applicant" if r.get("direction") == "in" else "agent",
            "channel": "sms",
            "text": text,
            "at": _ts(r.get("timestamp")),
        })
    msgs.sort(key=lambda m: m["at"])
    return msgs
