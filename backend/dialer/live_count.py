"""Single source of truth for 'who is genuinely on a live call right now'.

Both the concurrency cap (`place_call._enforce_concurrency_cap`) and the Call
Queue display (`server.get_call_queue`) MUST agree on this, otherwise the UI
shows spare capacity while the cap is silently full of ghosts (calls that ended
but whose candidate is still `screening_status='in_progress'` because the
managed-mode post-call sync hasn't landed yet). Before this module the cap
counted raw in_progress (ghost-inclusive) and the display filtered ghosts — so
a few stuck rows could block dialing for up to ~22 min with the UI none the
wiser. Keep this the ONLY place that defines 'live'.

Tenant-wide counting is conversation-first (`count_tenant_live`) because only
screening sets `screening_status='in_progress'` — revival dials archived
no-shows, which burn a real ElevenLabs line the tenant cap exists to protect
without ever setting that flag.

Scope, so nobody sizes a cap against a number that doesn't mean what it says:
this counts OUTBOUND calls only. An inbound call has no conversations row while
it is live — the only insert on that path is in the post-call webhook, after
the call has ended — so it is invisible here and to the cap. Real line usage is
this number plus whatever is ringing in.
"""
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set

from motor.motor_asyncio import AsyncIOMotorDatabase

# A conversation in one of these statuses represents a call that is (or could
# still be) on the phone. Anything else — completed/no_answer/failed — is done.
ACTIVE_CONV_STATUSES: List[str] = ["in_progress", "ringing", "queued", "initiated"]

# A call older than this with no terminal status is treated as a ghost, not a
# live call. Matches the display cutoff; the reconcile sweep uses a slightly
# larger value so it never resets something still shown as live.
GHOST_CUTOFF_MINUTES = 20


async def filter_live_call_ids(
    db: AsyncIOMotorDatabase,
    cand_ids: Iterable[str],
    ghost_cutoff_minutes: int = GHOST_CUTOFF_MINUTES,
) -> Set[str]:
    """Given candidate ids already known to be `screening_status='in_progress'`,
    return the subset that are on a GENUINELY live call — i.e. have a
    conversation in an active status created within the ghost cutoff.

    Returns a set for O(1) membership tests by callers."""
    cand_ids = list(cand_ids)
    if not cand_ids:
        return set()
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=ghost_cutoff_minutes)).isoformat()
    active = await db.conversations.find(
        {
            "candidate_id": {"$in": cand_ids},
            "status": {"$in": ACTIVE_CONV_STATUSES},
            "created_at": {"$gt": cutoff},
        },
        {"_id": 0, "candidate_id": 1},
    ).to_list(len(cand_ids) + 100)
    return {c["candidate_id"] for c in active}


async def count_tenant_live(
    db: AsyncIOMotorDatabase,
    user_id: str,
    ghost_cutoff_minutes: int = GHOST_CUTOFF_MINUTES,
    exclude_candidate_id: Optional[str] = None,
) -> int:
    """Every outbound AI call genuinely on the phone for this tenant —
    screening AND no_show_revival — counted from conversations, not
    screening_status.

    Every outbound dial path inserts a conversations row at dial time, but only
    screening also flips the candidate to `screening_status='in_progress'`.
    Counting candidates therefore under-reports the plan ceiling by every
    revival call in flight; counting conversations picks them up by
    construction.

    Inbound is out of scope and cannot be folded in here: an inbound call gets
    its conversations row only when the post-call webhook materialises one, and
    that row carries no candidate_id anyway. See the module docstring.

    Distinct candidates, not rows: a ghost row plus the live retry that
    superseded it is one phone call and must not eat two cap slots.

    `exclude_candidate_id` leaves the candidate we are about to dial out of the
    count — a cap gate must never count someone against himself. His previous
    conversation is non-terminal only because the post-call webhook is still
    summarising, which is routinely slower than the 25s DND redial; without the
    exclusion that redial is postponed out of the iOS repeat-caller window by
    its own predecessor. Display callers count everyone and must not pass it."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=ghost_cutoff_minutes)).isoformat()
    query: Dict[str, Any] = {
        "user_id": user_id,
        "status": {"$in": ACTIVE_CONV_STATUSES},
        "created_at": {"$gt": cutoff},
    }
    if exclude_candidate_id:
        query["candidate_id"] = {"$ne": exclude_candidate_id}
    active = await db.conversations.find(
        query, {"_id": 0, "candidate_id": 1},
    ).to_list(2000)
    return len({c["candidate_id"] for c in active if c.get("candidate_id")})


async def count_pipeline_live(
    db: AsyncIOMotorDatabase,
    user_id: str,
    pipeline_id: str,
    ghost_cutoff_minutes: int = GHOST_CUTOFF_MINUTES,
    exclude_candidate_id: Optional[str] = None,
) -> int:
    """The same 'genuinely on the phone' count as `count_tenant_live`, narrowed
    to one pipeline — the denominator for `pipe_cap`.

    Conversations carry no pipeline_id, so this resolves candidates first and
    then applies the one live definition. The candidate set must be a superset
    of every dial type: screening flips `screening_status='in_progress'`, while
    revival deliberately leaves the candidate archived and is identifiable only
    by its `revival_last_attempt_at` stamp. Counting in_progress alone made
    pipe_cap structurally blind to revival, so one office's hourly revival tick
    could spend the whole tenant budget and starve the other office's dialer —
    the exact thing pipe_cap exists to prevent."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=ghost_cutoff_minutes)).isoformat()
    query: Dict[str, Any] = {
        "user_id": user_id,
        "pipeline_id": pipeline_id,
        "$or": [
            {"screening_status": "in_progress"},
            {"revival_last_attempt_at": {"$gt": cutoff}},
        ],
    }
    if exclude_candidate_id:
        query["id"] = {"$ne": exclude_candidate_id}
    maybe_live = await db.candidates.find(query, {"_id": 0, "id": 1}).to_list(2000)
    return len(await filter_live_call_ids(
        db, [c["id"] for c in maybe_live], ghost_cutoff_minutes=ghost_cutoff_minutes,
    ))
