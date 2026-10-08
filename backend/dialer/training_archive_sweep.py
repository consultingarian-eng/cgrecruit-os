"""Training-lapsed archive sweep — clears the Training column of candidates
whose scheduled training day is 4+ days gone.

The Training column is a to-do list, not a ledger. Once a candidate's training
date is days in the past there is nothing left to do from the kanban: either
they attended and have moved on to onboarding/sales in CG1 (outside the
recruitment process), or they no-showed, we chased, and no new date was ever
locked in. Both kinds sat on the board forever, burying the people who still
need action.

Four days is the grace window: reschedule conversations happen inside it, and a
candidate who rebooks gets a FUTURE training_start_at — which resets the clock
by itself, so rescheduled people are never swept early.

Coming back is cheap by design. The SMS matcher already ranks archived
candidates as valid matches ("a no-show asking to rebook"), and training_sms /
email_replies un-archive an archived candidate the moment they get back in
touch — whether this sweep filed them (`training_lapsed`) or a recruiter
archived the no-shows by hand — restoring them to the board at their old stage
with a fresh 4-day hold (`training_lapse_hold_until`) so this sweep doesn't
re-file them mid-conversation before a new date is agreed. Rejected and
withdrawn candidates stay archived: those are decisions, not filing.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from . import state
from .state import now_utc

logger = logging.getLogger("auto_dialer")

TRAINING_LAPSE_DAYS = 4
ARCHIVED_REASON = "training_lapsed"

# Archives that mean "we said goodbye", not "we filed a no-show": these never
# come back on their own — a rejected or withdrawn candidate texting again is
# a conversation for a human, not an automatic seat on the board.
NEVER_RESTORE_REASONS = frozenset({"rejected", "withdrawn"})


def _parse_training_iso(value: Optional[str]) -> Optional[datetime]:
    """training_start_at / sms_proposed_reschedule_date arrive as naive local
    (ET) ISO from some paths and tz-aware from others. An explicit offset is
    respected as written (our own hold stamps are aware UTC); naive values go
    through the SMS module's ET parser so the sweep and the reminder engine can
    never disagree on when a training 'was'."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            return dt
    except Exception:
        pass
    try:
        from training_sms import _parse_training_dt
        return _parse_training_dt(str(value))
    except Exception:
        return None


def should_archive_training(cand: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """Pure decision: is this TRAINING candidate 4+ days past their date with
    no future reschedule in play and no fresh-contact hold?"""
    now = now or datetime.now(timezone.utc)
    if (cand.get("stage") or "").upper() != "TRAINING" or cand.get("archived_at"):
        return False
    start = _parse_training_iso(cand.get("training_start_at"))
    if start is None:
        # No date, no clock — a TRAINING candidate without a scheduled day is
        # still visible work, not a lapsed one.
        return False
    if start + timedelta(days=TRAINING_LAPSE_DAYS) > now:
        return False
    # They got back in touch recently — hold the sweep so the conversation can
    # land a new date without them vanishing off the board mid-thread.
    hold = _parse_training_iso(cand.get("training_lapse_hold_until"))
    if hold is not None and hold > now:
        return False
    # A reschedule proposal is on the table but not yet confirmed — let the
    # confirmation flow finish rather than archiving under it.
    proposed = _parse_training_iso(cand.get("sms_proposed_reschedule_date"))
    if proposed is not None and proposed > now:
        return False
    return True


async def training_lapsed_sweep() -> Dict[str, Any]:
    """Archive every TRAINING candidate whose date lapsed 4+ days ago."""
    if state._db is None:
        return {"checked": 0, "archived": 0}
    db = state._db
    candidates = await db.candidates.find(
        {
            "stage": "TRAINING",
            "archived_at": None,
            "training_start_at": {"$exists": True, "$nin": [None, ""]},
        },
        {"_id": 0},
    ).to_list(1000)
    archived = 0
    now_iso = now_utc().isoformat()
    for cand in candidates:
        try:
            if not should_archive_training(cand):
                continue
            await db.candidates.update_one(
                {"id": cand["id"], "archived_at": None},
                {"$set": {
                    "archived_at": now_iso,
                    "archived_reason": ARCHIVED_REASON,
                    "updated_at": now_iso,
                }},
            )
            archived += 1
            logger.info(
                f"training-lapsed sweep: archived {cand.get('first_name')} "
                f"{cand.get('last_name')} ({cand['id']}) — training was "
                f"{cand.get('training_start_at')}"
            )
            # Deliberately NO CG1 page (owner's rule, 2026-08-18): a lapsed
            # archive is a status flag, and admins only want genuine trainee
            # questions/correspondence. The log line above is the record.
        except Exception as e:
            logger.warning(f"training-lapsed sweep failed for {cand.get('id')}: {e}")
    if candidates:
        logger.info(f"training-lapsed sweep: checked {len(candidates)}, archived {archived}")
    return {"checked": len(candidates), "archived": archived}


async def unarchive_on_contact(db, cand: Dict[str, Any]) -> bool:
    """An archived candidate got back in touch — put them back on the board at
    their old stage with a fresh 4-day hold. Returns True (and mutates `cand`
    in place) when an un-archive happened, so callers can keep working with
    the live state.

    Covers two kinds of archive: the sweep's own `training_lapsed`, and manual
    archives of TRAINING candidates — the hand-swept no-show. Found live
    2026-08-10: three Aug-3 no-shows were bulk-archived by hand on Aug 5;
    one texted the same morning and rebooked himself onto the next Monday, but
    because only `training_lapsed` came back then, he stayed filed in the
    archive — fully booked, invisible on the kanban and roster, and a surprise
    when he walked in. Rejected/withdrawn/merged candidates never auto-restore:
    those archives are a decision, not a filing."""
    if not cand.get("archived_at"):
        return False
    reason = cand.get("archived_reason") or ""
    if reason != ARCHIVED_REASON:
        if (cand.get("stage") or "").upper() != "TRAINING":
            return False
        if reason in NEVER_RESTORE_REASONS or reason.startswith("merged_into:"):
            return False
    hold_until = (now_utc() + timedelta(days=TRAINING_LAPSE_DAYS)).isoformat()
    await db.candidates.update_one(
        {"id": cand["id"]},
        {"$set": {
            "archived_at": None,
            "archived_reason": None,
            "training_lapse_hold_until": hold_until,
            "updated_at": now_utc().isoformat(),
        }},
    )
    cand["archived_at"] = None
    cand["archived_reason"] = None
    cand["training_lapse_hold_until"] = hold_until
    logger.info(
        f"unarchive-on-contact: restored {cand.get('id')} "
        f"(was archived as {reason or 'unknown'!r}) — 4-day hold set"
    )
    return True


def schedule_training_lapsed_sweep() -> None:
    """Hourly, first run 5 minutes after startup so a deploy drains the backlog."""
    if state._scheduler is None:
        return
    try:
        state._scheduler.add_job(
            training_lapsed_sweep,
            "interval",
            hours=1,
            id="training-lapsed-sweep",
            replace_existing=True,
            misfire_grace_time=3600,
            max_instances=1,
            coalesce=True,
            next_run_time=now_utc() + timedelta(minutes=5),
        )
        logger.info("training-lapsed sweep scheduled (hourly, first run in 5 min)")
    except Exception as e:
        logger.warning(f"training-lapsed sweep schedule failed: {e}")
