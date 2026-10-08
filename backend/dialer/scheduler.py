"""APScheduler lifecycle helpers."""
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from motor.motor_asyncio import AsyncIOMotorDatabase

from . import state
from .live_count import filter_live_call_ids
from .window import parse_next_call_at, store_next_call_at
from app_tz import app_zone, default_tz_name  # APP_TIMEZONE (noqa: F401)

logger = logging.getLogger("auto_dialer")


def start_scheduler(db: AsyncIOMotorDatabase) -> AsyncIOScheduler:
    if state._scheduler:
        return state._scheduler
    state._db = db
    state._scheduler = AsyncIOScheduler(timezone="UTC")
    state._scheduler.start()
    # Periodic ghost-call sweep — see comments in `_periodic_reconcile`. Runs
    # every 5 minutes so stale `in_progress` candidates self-clear quickly.
    state._scheduler.add_job(
        _periodic_reconcile,
        "interval",
        minutes=5,
        id="call-queue-reconcile-sweep",
        replace_existing=True,
        misfire_grace_time=120,
        max_instances=1,
    )
    # Recovery sweep — runs once 30 seconds after startup to re-queue any
    # candidate whose scheduled call job was lost when the server restarted.
    # APScheduler is in-memory, so every deploy orphans jobs for candidates
    # stuck at screening_status='queued' with next_call_at in the past.
    from datetime import datetime, timezone, timedelta
    state._scheduler.add_job(
        _startup_queue_recovery,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=30),
        id="startup-queue-recovery",
        replace_existing=True,
        misfire_grace_time=120,
    )
    # Auto-sync sweep — every 60 seconds, find any ElevenLabs conversation stuck
    # at in_progress with no transcript (post-call webhook missed) and fetch it
    # directly from ElevenLabs so recruiters never have to press Sync manually.
    state._scheduler.add_job(
        _auto_sync_stuck_conversations,
        "interval",
        seconds=60,
        id="conversation-auto-sync",
        replace_existing=True,
        misfire_grace_time=60,
        max_instances=1,
    )
    # No-show auto-archive — every hour, archive APPOINTMENT candidates who were
    # marked no-show 48+ hours ago and never rebooked (attendance_status still "no_show").
    state._scheduler.add_job(
        _auto_archive_stale_no_shows,
        "interval",
        hours=1,
        id="no-show-auto-archive",
        replace_existing=True,
        misfire_grace_time=300,
    )
    # Screening staleness sweep — every 6 hours, archive SCREENING candidates
    # who have been called but haven't progressed in 48+ hours (gone cold).
    state._scheduler.add_job(
        _auto_archive_stale_screening,
        "interval",
        hours=6,
        id="screening-auto-archive",
        replace_existing=True,
        misfire_grace_time=300,
    )
    # Form staleness sweep — every 6 hours, archive FORM candidates who were
    # sent the form but haven't submitted within 48 hours.
    state._scheduler.add_job(
        _auto_archive_stale_form,
        "interval",
        hours=6,
        id="form-auto-archive",
        replace_existing=True,
        misfire_grace_time=300,
    )
    # Max-attempts sweep — every 6 hours, archive SCREENING candidates whose
    # pipeline retry limit is fully exhausted and 24h have passed since last contact.
    state._scheduler.add_job(
        _auto_archive_exhausted_attempts,
        "interval",
        hours=6,
        id="exhausted-attempts-auto-archive",
        replace_existing=True,
        misfire_grace_time=300,
    )
    # Weak-verdict sweep — daily, archive SCREENING candidates the AI rated
    # "weak" 7+ days ago that recruiters never acted on.
    state._scheduler.add_job(
        _auto_archive_weak_verdict,
        "interval",
        hours=24,
        id="weak-verdict-auto-archive",
        replace_existing=True,
        misfire_grace_time=600,
    )
    # No-show revival tick — hourly, per enabled pipeline, queue AI courtesy
    # calls to archived no-shows (inside call window, under the daily cap).
    from .revival import _no_show_revival_tick
    state._scheduler.add_job(
        _no_show_revival_tick,
        "interval",
        hours=1,
        id="no-show-revival-tick",
        replace_existing=True,
        misfire_grace_time=600,
        max_instances=1,
    )
    logger.info("auto-dialer scheduler started + periodic reconcile + startup recovery + auto-sync + no-show/screening/form/exhausted-attempts/weak-verdict archive + no-show revival tick wired")
    return state._scheduler


async def _recover_all(cursor, cap: int = 20000) -> list:
    """Drain a recovery cursor instead of keeping only its first page.

    APScheduler's jobstore is in-memory, so a candidate this sweep skips has no
    call job at all until someone restarts the process again — while the Call
    Queue keeps counting them and running their ETA down to "Calling soon". The
    old `.to_list(500)` was tenant-wide and unsorted, so past that cap it was
    Mongo natural order that decided who never got called. The ceiling here only
    exists so a runaway collection can't exhaust memory, and it says so in the
    log if it is ever reached.
    """
    docs: list = []
    async for doc in cursor:
        docs.append(doc)
        if len(docs) >= cap:
            logger.warning(f"startup queue recovery: hit the {cap}-document ceiling — the remainder keep no call job until the next restart")
            break
    return docs


# How far back the no_answer/incomplete_info recovery scan reaches. Comfortably
# longer than any configurable retry cadence, short enough that a tenant's
# historical backlog isn't walked candidate-by-candidate on every boot.
RETRY_RECOVERY_MAX_AGE_DAYS = 14


async def _startup_queue_recovery() -> None:
    """Re-wire APScheduler jobs that were dropped on server restart.

    APScheduler is in-memory — every deploy orphans all scheduled call jobs.
    We sweep ALL candidates with screening_status='queued' and split them:

    - next_call_at in the PAST  → already overdue; re-schedule soon (staggered
      30s apart) so they're called as soon as possible without thundering herd.
    - next_call_at in the FUTURE → not yet due; re-schedule at their original
      time so a deploy never causes a premature call.
    """
    if state._db is None:
        return
    db = state._db
    from datetime import datetime, timezone, timedelta
    from .queue import sched_call  # noqa: import inside fn to avoid circular import at module load
    now_dt = datetime.now(timezone.utc)

    orphans = await _recover_all(db.candidates.find(
        {
            "screening_status": "queued",
            "next_call_at": {"$ne": None},
            "archived_at": None,
        },
        {"_id": 0, "id": 1, "user_id": 1, "next_call_at": 1},
    ))

    # no_answer/incomplete_info candidates whose retry job this restart orphaned.
    # Fetched with NO next_call_at predicate: the stored value is an ET-naive
    # wall clock, so any Mongo comparison against a UTC `now` is a raw string
    # compare that files every retry due inside the tz offset (~4-5h) as
    # past-due — and the default retry delay is 2h, i.e. the whole backlog gets
    # pulled forward on every deploy. Partition them in Python below, exactly
    # as the `queued` orphans are.
    #
    # `updated_at` bounds the scan in its place. It is a true UTC ISO stamp, so
    # comparing it as a string is sound (same idiom as the archive sweeps), and
    # every writer that arms or re-arms a retry refreshes it — a row untouched
    # for weeks has no orphaned job left to recover, only exhausted-attempt
    # backlog that costs two round trips each just to be skipped. Unbounded this
    # matched every no_answer candidate the tenant has ever had, and each one
    # that survives the attempt cap reaches the slot-packing loop in
    # schedule_call_with_window — thousands of sequential queries, 30s after boot.
    retry_cutoff = (now_dt - timedelta(days=RETRY_RECOVERY_MAX_AGE_DAYS)).isoformat()
    retry_orphans = await _recover_all(db.candidates.find(
        {
            "screening_status": {"$in": ["no_answer", "incomplete_info"]},
            "archived_at": None,
            "appointment_at": None,
            "updated_at": {"$gte": retry_cutoff},
        },
        {"_id": 0, "id": 1, "user_id": 1, "next_call_at": 1},
    ))

    # next_call_at is stored as a timezone-naive ISO string in the user's
    # pipeline timezone (default America/New_York), so it only means anything
    # once localised — "09:00:00" ET is not overdue at "11:xx" UTC even though
    # "09" < "11" lexicographically. Resolve each user's timezone once and hand
    # it to window.parse_next_call_at, which also tolerates the offset-aware
    # rows older writers left behind (pytz.localize raises on those, and the
    # bare except that caught it filed the row as datetime.min — i.e. overdue,
    # dialled 30s after boot).
    from .state import settings_for as _settings_for
    unique_uids = {c["user_id"] for c in orphans} | {c["user_id"] for c in retry_orphans}
    _user_tz: dict = {}
    for uid in unique_uids:
        try:
            s = await _settings_for(uid, None)
            _user_tz[uid] = (s.get("region_language") or {}).get("timezone") or default_tz_name()
        except Exception:
            _user_tz[uid] = default_tz_name()

    def _tz_name(cand: dict) -> str:
        return (_user_tz.get(cand["user_id"]) or default_tz_name())

    def _next_call_utc(cand: dict):
        return parse_next_call_at(cand.get("next_call_at"), _tz_name(cand))

    overdue, future, unreadable = [], [], []
    for cand in orphans:
        due = _next_call_utc(cand)
        if due is None:
            unreadable.append(cand)
        elif due < now_dt:
            overdue.append(cand)
        else:
            future.append(cand)
    # Dial in due order: the page sorts the Queued list by next_call_at, and on
    # this path it is the stagger below — not the query — that decides who is
    # actually called first.
    overdue.sort(key=_next_call_utc)
    logger.info(f"startup queue recovery: {len(overdue)} overdue, {len(future)} future-scheduled")
    if unreadable:
        # Arming these would dial someone off a timestamp we cannot read.
        logger.warning(f"startup queue recovery: {len(unreadable)} queued candidates have an unparseable next_call_at — left unscheduled")

    # Overdue: call soon, staggered 30s apart so we don't spike concurrency.
    for i, cand in enumerate(overdue):
        try:
            run_at = now_dt + timedelta(seconds=30 + i * 30)
            sched_call(cand["user_id"], cand["id"], run_at)
            # Persist the staggered time — sched_call only touches APScheduler.
            # Without this the row keeps its original past next_call_at and the
            # queue reads "Calling soon" for the whole stagger, which on a large
            # backlog runs for hours.
            await db.candidates.update_one(
                {"id": cand["id"], "user_id": cand["user_id"]},
                {"$set": {"next_call_at": store_next_call_at(run_at, _tz_name(cand))}},
            )
        except Exception as e:
            logger.warning(f"startup recovery (overdue) failed for {cand['id']}: {e}")

    # Future: reschedule at the original next_call_at (converted to UTC) so a
    # deploy never causes a premature call.
    for cand in future:
        try:
            run_at = _next_call_utc(cand)
            sched_call(cand["user_id"], cand["id"], run_at)
        except Exception as e:
            logger.warning(f"startup recovery (future) failed for {cand['id']}: {e}")

    # Split the retry population the same way: a readable FUTURE next_call_at
    # means the job only needs re-creating at its original time; anything else
    # (past, missing, unreadable) is stranded and goes through the window-aware
    # retry path below.
    future_retry_orphans, stranded = [], []
    for cand in retry_orphans:
        due = _next_call_utc(cand)
        if due is not None and due >= now_dt:
            future_retry_orphans.append(cand)
        else:
            stranded.append(cand)

    # ---- no_answer/incomplete_info with a future next_call_at ----
    # These had real APScheduler jobs (scheduled by maybe_schedule_incomplete_retry)
    # that were just killed by this restart. Re-create them at the original time.
    logger.info(f"startup queue recovery: {len(future_retry_orphans)} no_answer/incomplete with future next_call_at — re-creating orphaned jobs")
    for cand in future_retry_orphans:
        try:
            run_at = _next_call_utc(cand)
            sched_call(cand["user_id"], cand["id"], run_at)
        except Exception as e:
            logger.warning(f"startup recovery (future retry orphan) failed for {cand['id']}: {e}")

    # ---- Orphaned no_answer / incomplete_info candidates ----
    # Candidates whose post-call webhook ran before retry scheduling was wired
    # up correctly end up stuck at no_answer/incomplete_info with no next_call_at
    # and no APScheduler job. The normal startup sweep above misses them because
    # they're not at screening_status='queued'. Re-trigger maybe_schedule_incomplete_retry
    # for all of them so they get back into the retry loop.
    from .retry import maybe_schedule_incomplete_retry
    if stranded:
        logger.info(f"startup queue recovery: {len(stranded)} stranded no_answer/incomplete_info candidates — scheduling retries (30s stagger)")
        for i, cand in enumerate(stranded):
            try:
                # base_delay_minutes=0: these candidates are past-due — the
                # configured retry delay already elapsed. Schedule them for the
                # next available window slot immediately, don't add another 2h.
                result = await maybe_schedule_incomplete_retry(cand["user_id"], cand["id"], extra_stagger_seconds=i * 30, base_delay_minutes=0)
                logger.info(f"stranded retry for {cand['id']}: {result.get('status')} (+{i*30}s stagger)")
            except Exception as e:
                logger.warning(f"stranded retry failed for {cand['id']}: {e}")
    else:
        logger.info("startup queue recovery: no stranded no_answer/incomplete_info candidates")


async def _periodic_reconcile() -> None:
    """Sweep candidates that are stuck at `screening_status='in_progress'`
    with no live conversation. Mirrors `POST /pipelines/{id}/call-queue/
    reconcile` but tenant-wide, fire-and-forget.

    Why: managed-mode calls go EL ↔ Twilio direct, so our Twilio
    StatusCallback hook (`/api/twiml/status/...`) never fires for them.
    Without periodic cleanup, candidate docs sit at in_progress forever
    and the Call Queue page shows ghosts (e.g. "1 / 5 agents are dialling
    now" with someone's old test call). This sweep promotes them out of
    in_progress based on what the conversations collection says happened.
    """
    if state._db is None:
        return
    db = state._db
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=22)).isoformat()
    # Only sweep candidates whose updated_at is at least 22 min old. Matches
    # the 20-min ghost cap in get_call_queue — candidates older than this with
    # no live conversation are definitely stale.
    stuck = await db.candidates.find(
        {"screening_status": "in_progress", "updated_at": {"$lt": cutoff}},
        {"_id": 0, "id": 1, "user_id": 1},
    ).to_list(2000)
    if not stuck:
        return
    cand_ids = [c["id"] for c in stuck]
    # The same definition of "live" the Call Queue display uses, but at 22
    # minutes instead of its 20 — this sweep must stay strictly more
    # conservative than the page, or it resets a row the recruiter is still
    # watching dial. Re-implementing the status list here with no age bound at
    # all meant every conversation the display had already written off as a
    # ghost was precisely the one this refused to touch, so it pinned its
    # candidate at in_progress forever.
    active_ids = await filter_live_call_ids(db, cand_ids, ghost_cutoff_minutes=22)
    reset = 0
    retry_idx = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for cand in stuck:
        cid = cand["id"]
        if cid in active_ids:
            continue
        latest = await db.conversations.find_one(
            {"candidate_id": cid},
            {"_id": 0, "status": 1},
            sort=[("created_at", -1)],
        )
        if not latest:
            # No call was ever placed for this candidate, so `in_progress` here
            # is not a dropped-phone-webhook ghost — it is a text screening in
            # flight (reopen_screening sets in_progress) or a brand-new race.
            # This sweep exists to clean up phone ghosts; leaving a live text
            # screening alone is not its job. Resetting it to "pending" used to
            # yank a candidate mid-conversation out of the flow and, in a
            # chat_first office, re-send their arrival text on the next sweep.
            continue
        if latest.get("status") in ("no_answer", "failed"):
            new_status = "no_answer"
        else:
            new_status = "incomplete_info"
        await db.candidates.update_one(
            {"id": cid},
            {"$set": {"screening_status": new_status, "updated_at": now_iso}},
        )
        reset += 1
        # Schedule the 2h retry so the candidate doesn't sit in limbo.
        # Stagger 60s apart so bulk resets don't create a thundering herd at 9am.
        if new_status in ("no_answer", "incomplete_info"):
            try:
                from .retry import maybe_schedule_incomplete_retry
                await maybe_schedule_incomplete_retry(cand["user_id"], cid, extra_stagger_seconds=retry_idx * 60)
                retry_idx += 1
            except Exception as e:
                logger.warning(f"reconcile retry schedule failed for {cid}: {e}")
    if reset:
        logger.info(f"periodic reconcile reset {reset} stale in_progress candidates (of {len(stuck)} examined)")


async def _auto_sync_stuck_conversations() -> None:
    """Every 60 seconds: find ElevenLabs conversations stuck at in_progress with
    no transcript (post-call webhook was missed) and fetch them directly from
    ElevenLabs so recruiters never have to press Sync manually.

    Only touches conversations older than 90 seconds (gives the real post-call
    webhook time to arrive first) and with an elevenlabs_conversation_id set.
    """
    if state._db is None:
        return
    db = state._db
    from datetime import datetime, timezone, timedelta
    now_utc = datetime.now(timezone.utc)
    cutoff = (now_utc - timedelta(seconds=45)).isoformat()
    # Any conversation older than 30 min with no transcript is treated as
    # no_answer regardless of what EL's API reports — EL sometimes never
    # updates their own status for calls that disconnected immediately.
    hard_cutoff = (now_utc - timedelta(minutes=30)).isoformat()

    stuck = await db.conversations.find(
        {
            "elevenlabs_conversation_id": {"$exists": True, "$ne": None},
            "status": {"$in": ["in_progress", "ringing", "initiated", "queued"]},
            "created_at": {"$lt": cutoff},
            "$or": [
                {"transcript": None},
                {"transcript": {"$size": 0}},
                {"transcript": {"$exists": False}},
            ],
        },
        {"_id": 0, "id": 1, "user_id": 1, "candidate_id": 1, "elevenlabs_conversation_id": 1, "created_at": 1,
         "call_type": 1, "revival_prev_appointment_at": 1},
    ).to_list(20)  # cap per-tick so overnight backlog can't freeze the event loop

    if not stuck:
        return

    logger.info(f"auto-sync: found {len(stuck)} stuck conversations to sync")

    try:
        from voice_service import fetch_elevenlabs_conversation
        from ai_service import summarize_call_transcript
    except Exception as e:
        logger.warning(f"auto-sync: import failed: {e}")
        return

    loop = asyncio.get_event_loop()
    retry_idx = 0

    for conv in stuck:
        try:
            el_id = conv["elevenlabs_conversation_id"]
            # fetch_elevenlabs_conversation uses blocking requests.get — run in
            # executor so it never freezes the asyncio event loop.
            data = await loop.run_in_executor(None, fetch_elevenlabs_conversation, el_id)
            if not data:
                continue
            raw_transcript = data.get("transcript") or []
            metadata = data.get("metadata") or {}
            duration = metadata.get("call_duration_secs") or data.get("duration_seconds")
            termination_reason = (metadata.get("termination_reason") or "").lower()
            el_status = (data.get("status") or "").lower()
            is_very_old = (conv.get("created_at") or "") < hard_cutoff
            # "Has it ended?" must come from ElevenLabs' own verdict on the
            # call, never from the duration. EL reports call_duration_secs for
            # a conversation that is still RUNNING, so `duration is not None`
            # was true 45 seconds into every live call — and the branch below
            # then filed the call as an empty no_answer, permanently, while
            # the candidate was still talking. Because that status is outside
            # this sweep's own filter, nothing ever looked at the row again, so
            # calls were closed with no transcript, including ones several
            # minutes long.
            call_ended = bool(
                termination_reason
                or el_status in ("done", "failed", "error", "completed")
                or is_very_old
            )
            if not raw_transcript:
                if not call_ended:
                    continue  # ElevenLabs still has nothing — genuinely in progress
                # Ended, but with real talk-time behind it: that is ElevenLabs
                # still transcribing, not an empty call. Only the hard cutoff
                # may conclude one of those; a call that never connected has a
                # couple of seconds on it and still closes immediately.
                if (duration or 0) >= 20 and not is_very_old:
                    continue
                if (duration or 0) >= 20:
                    logger.warning(
                        f"auto-sync: {el_id} closed with NO transcript after "
                        f"{duration}s of call — ElevenLabs never produced one"
                    )
                # Call ended with no transcript (didn't connect / hung up immediately)
                await db.conversations.update_one(
                    {"id": conv["id"], "user_id": conv["user_id"]},
                    {"$set": {"transcript": [], "status": "no_answer", "duration_seconds": duration or 0}},
                )
                if (conv.get("call_type") or "") == "no_show_revival":
                    # Revival: record the miss only — never touch screening
                    # fields or schedule screening retries for archived no-shows.
                    await db.candidates.update_one(
                        {"id": conv["candidate_id"], "user_id": conv["user_id"]},
                        {"$set": {"last_call_status": "no_answer", "revival_outcome": "no_answer"}},
                    )
                    logger.info(f"auto-sync: revival conv {conv['elevenlabs_conversation_id']} ended with no transcript → no_answer")
                    continue
                # A recruiter's pause lives in this same screening_status field,
                # so a blind $set erases it and the retry below then books a
                # real call for someone the Paused list promises will not be
                # dialed. retry.py's own paused guard cannot help — by the time
                # it looks, we have already overwritten the pause.
                res = await db.candidates.update_one(
                    {"id": conv["candidate_id"], "user_id": conv["user_id"],
                     "screening_status": {"$ne": "paused"}},
                    {"$set": {"last_call_status": "no_answer", "screening_status": "no_answer"}},
                )
                if not res.matched_count:
                    await db.candidates.update_one(
                        {"id": conv["candidate_id"], "user_id": conv["user_id"]},
                        {"$set": {"last_call_status": "no_answer"}},
                    )
                    logger.info(f"auto-sync: {conv['candidate_id']} is paused — recorded no_answer without re-queueing")
                    continue
                # "We missed you" comms BEFORE the next call is queued —
                # queueing writes screening_status='queued', and this sweep is
                # the path that actually runs for most calls, so skipping it
                # here is what left the message many hours late.
                try:
                    from deps import maybe_fire_screening_retry
                    await maybe_fire_screening_retry(
                        conv["user_id"], conv["candidate_id"], outcome="no_answer")
                except Exception as e:
                    logger.warning(f"auto-sync no-transcript screening retry comms failed for {conv['candidate_id']}: {e}")
                try:
                    from .retry import maybe_schedule_incomplete_retry
                    await maybe_schedule_incomplete_retry(conv["user_id"], conv["candidate_id"], extra_stagger_seconds=retry_idx * 60)
                    retry_idx += 1
                except Exception as e:
                    logger.warning(f"auto-sync no-transcript retry failed for {conv['candidate_id']}: {e}")
                logger.info(f"auto-sync: conv {conv['elevenlabs_conversation_id']} ended with no transcript → no_answer")
                continue
            transcript = [
                {"role": x.get("role", ""), "text": x.get("message", "") or x.get("text", "")}
                for x in raw_transcript
            ]
            termination_reason = (
                metadata.get("termination_reason")
                or data.get("termination_reason")
                or ""
            ).lower()
            voicemail = "voicemail" in termination_reason
            # Phrase-scan fallback + misfire veto — shared with the webhook
            # handler. A voicemail flag on a call where the human produced a
            # real back-and-forth is a voicemail_detection misfire (it fires on
            # silence while get_available_slots loads); honouring it would void
            # the conversation and DND-redial the person who just spoke.
            from call_classification import resolve_voicemail
            voicemail, vm_misfired = resolve_voicemail(voicemail, transcript)
            if vm_misfired:
                logger.warning(f"auto-sync {conv.get('elevenlabs_conversation_id')}: voicemail_detection misfired on a live conversation — flag ignored")

            # Revival calls get fully separate post-call handling — no DND
            # retry, no screening verdict, no retry scheduling. Must branch
            # BEFORE the DND block or a revival voicemail would trigger a
            # screening-style redial against an archived candidate.
            if (conv.get("call_type") or "") == "no_show_revival":
                cand_r = await db.candidates.find_one(
                    {"id": conv["candidate_id"], "user_id": conv["user_id"]}, {"_id": 0}
                ) or {}
                from .revival import handle_revival_post_call
                await handle_revival_post_call(db, conv, cand_r, transcript, duration, voicemail)
                continue

            # Fire DND retry NOW — before the slow LLM summarisation below.
            # This is the primary trigger path since the EL post-call webhook
            # appears not to be reaching the server reliably.
            dnd_res: dict = {}
            if voicemail:
                try:
                    from .retry import schedule_dnd_retry
                    dnd_res = await schedule_dnd_retry(conv["user_id"], conv["candidate_id"]) or {}
                    logger.info(f"auto-sync DND retry for {conv['candidate_id']}: {dnd_res}")
                except Exception as e:
                    logger.warning(f"auto-sync DND retry failed for {conv['candidate_id']}: {e}")

            candidate_spoke = any(t.get("role") in ("user", "human") for t in transcript)
            from call_classification import is_complete_call, screening_status_after_call, DISQ_LABELS
            is_complete = is_complete_call(candidate_spoke, duration, voicemail)
            new_status = "completed" if is_complete else "no_answer"

            cand = await db.candidates.find_one(
                {"id": conv["candidate_id"], "user_id": conv["user_id"]}, {"_id": 0}
            ) or {}
            job = None
            if cand.get("job_id"):
                job = await db.jobs.find_one({"id": cand["job_id"]}, {"_id": 0})

            summary = await summarize_call_transcript(
                transcript, cand.get("first_name", ""), (job or {}).get("title", ""),
                duration_seconds=duration,
            )
            await db.conversations.update_one(
                {"id": conv["id"], "user_id": conv["user_id"]},
                {"$set": {
                    "transcript": transcript,
                    "summary": summary.get("summary", ""),
                    "suitability_score": summary.get("suitability_score"),
                    "duration_seconds": duration,
                    "status": new_status,
                }},
            )
            # Fire the same post-call candidate update the EL webhook would have done.
            from models import now_iso as _now_iso
            is_voicemail = voicemail or "voicemail" in (summary.get("summary") or "").lower()
            cand_update = {"last_call_status": new_status, "last_call_voicemail": is_voicemail, "updated_at": _now_iso()}
            verdict = summary.get("verdict")
            if verdict and is_complete:
                cand_update["verdict"] = verdict
                cand_update["call_summary"] = summary.get("summary", "")
                cand_update["suitability_score"] = summary.get("suitability_score")
            # One ladder for every post-call processor. This sweep used to
            # reject "weak" verdicts the webhook was congratulating, archive
            # them with no reason and send no comms — which message a
            # borderline candidate got depended on which processor won the
            # race. (appointment_pending outreach is deliberately not sent
            # here: the hourly unbooked-screened sweep is the net for that.)
            disq_reason = summary.get("disqualification_reason")
            cand_update["screening_status"] = screening_status_after_call(
                is_complete=is_complete, verdict=verdict, disq_reason=disq_reason,
                has_appointment=bool(cand.get("appointment_at")),
            )
            if cand_update["screening_status"] == "rejected":
                cand_update["disqualification_reason"] = DISQ_LABELS.get(disq_reason, disq_reason)
                cand_update["archived_at"] = _now_iso()
                cand_update["archived_reason"] = "withdrawn" if disq_reason == "withdrawn" else "rejected"
            # Same pause protection as the no-transcript branch — the verdict
            # and summary must land either way, a paused candidate just doesn't
            # get their status (and therefore their retry) put back. The two
            # terminal verdicts are exempt: they END the dial loop rather than
            # restarting it, and leaving one at 'paused' would keep an archived
            # candidate sitting in the Paused list forever.
            cand_filter = {"id": conv["candidate_id"], "user_id": conv["user_id"]}
            if cand_update["screening_status"] not in ("approved", "rejected"):
                cand_filter["screening_status"] = {"$ne": "paused"}
            res = await db.candidates.update_one(cand_filter, {"$set": cand_update})
            if not res.matched_count:
                cand_update.pop("screening_status", None)
                await db.candidates.update_one(
                    {"id": conv["candidate_id"], "user_id": conv["user_id"]},
                    {"$set": cand_update},
                )
                logger.info(f"auto-sync: {conv['candidate_id']} is paused — filed the verdict without re-queueing")
            # A gate-failed candidate must hear the outcome whichever path
            # filed it — this sweep used to thank them on the phone and then
            # go silent forever. Guard: only when WE are the first to file the
            # rejection (the webhook may already have sent it), and never for
            # withdrawals (they asked not to be contacted).
            if (
                cand_update.get("screening_status") == "rejected"
                and disq_reason and disq_reason != "withdrawn"
                and cand.get("screening_status") != "rejected"
            ):
                try:
                    from deps import send_stage_comms
                    refreshed = await db.candidates.find_one(
                        {"id": conv["candidate_id"]}, {"_id": 0}) or cand
                    await send_stage_comms(
                        user_id=conv["user_id"], candidate=refreshed, template_key="rejection")
                except Exception as e:
                    logger.warning(f"auto-sync rejection comms failed for {conv['candidate_id']}: {e}")
            # "We missed you" comms. Held back only while a DND bypass redial
            # is seconds away — texting someone and then immediately ringing
            # them again reads as spam, and if they pick that call up they
            # never needed the message. When the redial is not scheduled (or
            # this IS the redial, which returns "already done for this
            # attempt"), the text goes now.
            if (
                cand_update.get("screening_status") in ("no_answer", "incomplete_info")
                and dnd_res.get("status") != "scheduled"
            ):
                try:
                    from deps import maybe_fire_screening_retry
                    await maybe_fire_screening_retry(
                        conv["user_id"], conv["candidate_id"],
                        outcome=cand_update["screening_status"],
                    )
                except Exception as e:
                    logger.warning(f"auto-sync screening retry comms failed for {conv['candidate_id']}: {e}")
            # Queue retry if needed — stagger so bulk syncs don't create a thundering herd.
            if cand_update.get("screening_status") in ("no_answer", "incomplete_info"):
                try:
                    from .retry import maybe_schedule_incomplete_retry
                    await maybe_schedule_incomplete_retry(conv["user_id"], conv["candidate_id"], extra_stagger_seconds=retry_idx * 60)
                    retry_idx += 1
                except Exception as e:
                    logger.warning(f"auto-sync retry schedule failed for {conv['candidate_id']}: {e}")
            logger.info(f"auto-sync: synced conv {el_id} → {new_status}, verdict={verdict}")
        except Exception as e:
            logger.warning(f"auto-sync: failed for conv {conv.get('id')}: {e}")


async def _auto_archive_stale_no_shows() -> None:
    """Hourly: archive candidates in APPOINTMENT whose appointment is 48+ hours
    past and who never rescheduled. Four cases, each archived under its own
    reason because only the first is genuinely a no-show:

      1. no_show_not_rescheduled — explicitly marked no_show by a recruiter.
         The only group the revival dialer chases.
      2. cancelled_not_rebooked — the candidate told us they couldn't attend and
         never picked a new time. Gets the slot picker, not a call asking why
         they didn't turn up.
      3. attendance_unrecorded — the slot lapsed with nobody recording an
         outcome. We do not know whether they attended, so we ask the recruiter
         rather than phoning the candidate. This used to be filed as a no-show,
         which meant a candidate who attended and impressed us could be rung up
         two days later and asked why they missed their interview.
      4. attended_no_form — attended, but the team chose not to invite them
         onward. A final answer with no follow-up owed in either direction,
         so after the same 48h grace the no-show gets, the card is clutter
         (29 of them were sitting on the boards a median of 19 days).
    """
    if state._db is None:
        return
    db = state._db
    from datetime import datetime, timezone, timedelta
    from models import now_iso as _now_iso

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    # A separate, far longer window for interviews nobody has answered yet.
    #
    # 48 hours sounds generous and isn't: a Friday afternoon session is archived
    # before anyone is back at their desk on Monday, and both the board and CG1's
    # Booked Interviews list hide archived candidates — so the moment this fires,
    # the session disappears from the list the team actually works from and can
    # never be marked. Many unanswered interviews went that way. They are not people nobody got to; they are people nobody could
    # still see.
    #
    # Two weeks is long enough to survive a holiday and short enough that the
    # board doesn't silt up. Nothing else about the candidate changes — this only
    # decides when they stop being visible.
    unrecorded_cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()

    projection = {
        "_id": 0, "id": 1, "user_id": 1, "first_name": 1, "last_name": 1,
        "pipeline_id": 1, "appointment_cancelled_at": 1,
    }

    # Case 1: explicitly marked no_show and the 48h window has elapsed
    explicitly_marked = await db.candidates.find(
        {
            "stage": "APPOINTMENT",
            "attendance_status": "no_show",
            "no_show_at": {"$lt": cutoff},
            "archived_at": None,
        },
        projection,
    ).to_list(500)

    # Cases 2 and 3: appointment_at passed 48h+ ago, attendance never recorded.
    # Whether they told us in advance decides which.
    silently_lapsed = await db.candidates.find(
        {
            "stage": "APPOINTMENT",
            "attendance_status": None,
            # `unrecorded_cutoff`, not `cutoff` — see above. Someone who told us
            # in advance that they couldn't come keeps the 48-hour window,
            # because there is nothing left for a human to answer about them.
            "$or": [
                {"appointment_at": {"$lt": unrecorded_cutoff}},
                {"appointment_at": {"$lt": cutoff},
                 "appointment_cancelled_at": {"$ne": None}},
            ],
            "archived_at": None,
        },
        projection,
    ).to_list(500)

    # Case 4: attended but not progressed. The 48h grace runs from when the
    # outcome was RECORDED, not the slot time — a session marked three days
    # late still leaves the recruiter a correction window before the card
    # disappears. (Rebooking clears attendance_status, so anyone invited back
    # after all simply drops out of this query.)
    attended_done = await db.candidates.find(
        {
            "stage": "APPOINTMENT",
            "attendance_status": "attended_no_form",
            "$or": [
                {"attendance_recorded_at": {"$lt": cutoff}},
                {"attendance_recorded_at": None, "appointment_at": {"$lt": cutoff}},
            ],
            "archived_at": None,
        },
        projection,
    ).to_list(500)

    # Deduplicate by id in case a candidate somehow appears in both
    seen = set()
    stale = []
    for c, reason in (
        [(c, "no_show_not_rescheduled") for c in explicitly_marked]
        + [(c, "cancelled_not_rebooked" if c.get("appointment_cancelled_at") else "attendance_unrecorded")
           for c in silently_lapsed]
        + [(c, "attended_no_form") for c in attended_done]
    ):
        if c["id"] not in seen:
            seen.add(c["id"])
            c["_archive_reason"] = reason
            stale.append(c)

    if not stale:
        return

    logger.info(f"no-show auto-archive: found {len(stale)} stale no-shows to archive")
    now = _now_iso()
    for cand in stale:
        try:
            from .retry import cancel_pending_call_jobs
            cancel_pending_call_jobs(cand["id"])
        except Exception:
            pass
        try:
            from auto_dialer import cancel_appointment_reminders, cancel_pending_retry_calls
            cancel_appointment_reminders(cand["id"])
            cancel_pending_retry_calls(cand["id"])
        except Exception:
            pass
        reason = cand.get("_archive_reason") or "no_show_not_rescheduled"
        await db.candidates.update_one(
            {"id": cand["id"], "user_id": cand["user_id"]},
            {"$set": {
                "archived_at": now,
                "archived_reason": reason,
                "auto_dial": False,
                "updated_at": now,
            }},
        )
        nm = f"{cand.get('first_name', '')} {cand.get('last_name', '')}".strip() or "A candidate"
        if reason == "attendance_unrecorded":
            # We genuinely don't know what happened, so ask the one person who does.
            try:
                from notifications_service import create_notification
                await create_notification(
                    cand["user_id"], "appointment.attendance_unrecorded",
                    f"❓ Did {nm} attend their interview?",
                    body="The slot passed 48h ago with no outcome recorded, so they've been archived. "
                         "Mark them attended to move them on, or no-show to start the revival chase.",
                    link=f"/?candidate={cand['id']}",
                    candidate_id=cand["id"], pipeline_id=cand.get("pipeline_id"),
                )
            except Exception as e:
                logger.warning(f"attendance-unrecorded notification failed for {cand['id']}: {e}")
        elif reason == "cancelled_not_rebooked":
            # They told us they couldn't make it and then went quiet — one last
            # slot picker rather than a call asking why they didn't show up.
            try:
                from routes.attendance import _fire_slot_picker_outreach
                full = await db.candidates.find_one({"id": cand["id"]}, {"_id": 0})
                if full:
                    await _fire_slot_picker_outreach(
                        cand["user_id"], full, template_key="appointment_cancelled_rebook",
                    )
            except Exception as e:
                logger.warning(f"cancelled-not-rebooked slot picker failed for {cand['id']}: {e}")
        logger.info(f"no-show auto-archive: archived {nm} ({cand['id']}) as {reason}")


async def _auto_archive_stale_screening() -> None:
    """Every 6h: archive SCREENING candidates who have been called at least once
    but haven't progressed in 48+ hours — gone cold with no answer."""
    if state._db is None:
        return
    db = state._db
    from datetime import datetime, timezone, timedelta
    from models import now_iso as _now_iso

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    stale = await db.candidates.find(
        {
            "stage": "SCREENING",
            "screening_status": {"$in": ["no_answer", "incomplete_info"]},
            "call_attempts": {"$gte": 1},
            "appointment_at": None,
            "updated_at": {"$lt": cutoff},
            "archived_at": None,
        },
        {"_id": 0, "id": 1, "user_id": 1, "first_name": 1, "last_name": 1},
    ).to_list(500)

    if not stale:
        return

    logger.info(f"screening auto-archive: found {len(stale)} stale candidates to archive")
    now = _now_iso()
    for cand in stale:
        try:
            from .retry import cancel_pending_call_jobs
            cancel_pending_call_jobs(cand["id"])
        except Exception:
            pass
        try:
            from auto_dialer import cancel_pending_retry_calls
            cancel_pending_retry_calls(cand["id"])
        except Exception:
            pass
        await db.candidates.update_one(
            {"id": cand["id"], "user_id": cand["user_id"]},
            {"$set": {
                "archived_at": now,
                "archived_reason": "no_contact_48h",
                "auto_dial": False,
                "updated_at": now,
            }},
        )
        logger.info(f"screening auto-archive: archived {cand.get('first_name')} {cand.get('last_name')} ({cand['id']})")


async def _auto_archive_stale_form() -> None:
    """Every 6h: archive FORM-stage candidates who were sent the form but never
    submitted within 48 hours of being moved to the FORM stage."""
    if state._db is None:
        return
    db = state._db
    from datetime import datetime, timezone, timedelta
    from models import now_iso as _now_iso

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    stale = await db.candidates.find(
        {
            "stage": "FORM",
            "form_submitted_at": None,
            "updated_at": {"$lt": cutoff},
            "archived_at": None,
        },
        {"_id": 0, "id": 1, "user_id": 1, "first_name": 1, "last_name": 1},
    ).to_list(500)

    if not stale:
        return

    logger.info(f"form auto-archive: found {len(stale)} stale form candidates to archive")
    now = _now_iso()
    for cand in stale:
        try:
            from auto_dialer import cancel_form_reminder
            cancel_form_reminder(cand["id"])
        except Exception:
            pass
        await db.candidates.update_one(
            {"id": cand["id"], "user_id": cand["user_id"]},
            {"$set": {
                "archived_at": now,
                "archived_reason": "form_not_submitted_48h",
                "updated_at": now,
            }},
        )
        logger.info(f"form auto-archive: archived {cand.get('first_name')} {cand.get('last_name')} ({cand['id']})")


async def _auto_archive_exhausted_attempts() -> None:
    """Every 6h: archive SCREENING candidates whose pipeline's max retry attempts
    are fully exhausted and who haven't progressed within 24h of the last call."""
    if state._db is None:
        return
    db = state._db
    from datetime import datetime, timezone, timedelta
    from models import now_iso as _now_iso

    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    candidates = await db.candidates.find(
        {
            "stage": "SCREENING",
            "screening_status": {"$in": ["no_answer", "incomplete_info"]},
            "call_attempts": {"$gte": 1},
            "appointment_at": None,
            "updated_at": {"$lt": cutoff_24h},
            "archived_at": None,
        },
        {"_id": 0, "id": 1, "user_id": 1, "pipeline_id": 1, "call_attempts": 1, "first_name": 1, "last_name": 1},
    ).to_list(500)

    if not candidates:
        return

    from .state import settings_for
    settings_cache: dict = {}
    to_archive = []
    for cand in candidates:
        key = (cand["user_id"], cand.get("pipeline_id"))
        if key not in settings_cache:
            s = await settings_for(cand["user_id"], cand.get("pipeline_id"))
            settings_cache[key] = s
        s = settings_cache[key]
        max_attempts = int((s.get("auto_dialer") or {}).get("max_retry_attempts", 3))
        if (cand.get("call_attempts") or 0) >= max_attempts:
            to_archive.append(cand)

    if not to_archive:
        return

    logger.info(f"exhausted-attempts archive: {len(to_archive)} candidates to archive")
    now = _now_iso()
    for cand in to_archive:
        try:
            from .retry import cancel_pending_call_jobs
            cancel_pending_call_jobs(cand["id"])
        except Exception:
            pass
        try:
            from auto_dialer import cancel_pending_retry_calls
            cancel_pending_retry_calls(cand["id"])
        except Exception:
            pass
        await db.candidates.update_one(
            {"id": cand["id"], "user_id": cand["user_id"]},
            {"$set": {
                "archived_at": now,
                "archived_reason": "max_attempts_no_contact",
                "auto_dial": False,
                "updated_at": now,
            }},
        )
        logger.info(f"exhausted-attempts archive: archived {cand.get('first_name')} {cand.get('last_name')} ({cand['id']})")


async def _auto_archive_weak_verdict() -> None:
    """Daily: archive SCREENING candidates whose AI verdict was 'weak' and
    haven't been updated in 7+ days — they were screened and found unfit but
    no recruiter action was taken."""
    if state._db is None:
        return
    db = state._db
    from datetime import datetime, timezone, timedelta
    from models import now_iso as _now_iso

    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    stale = await db.candidates.find(
        {
            "stage": "SCREENING",
            "verdict": "weak",
            "updated_at": {"$lt": cutoff},
            "$or": [{"archived_at": None}, {"archived_at": {"$exists": False}}],
        },
        {"_id": 0, "id": 1, "user_id": 1, "first_name": 1, "last_name": 1},
    ).to_list(500)

    if not stale:
        return

    logger.info(f"weak-verdict auto-archive: found {len(stale)} candidates to archive")
    now = _now_iso()
    for cand in stale:
        try:
            from .retry import cancel_pending_call_jobs
            cancel_pending_call_jobs(cand["id"])
        except Exception:
            pass
        await db.candidates.update_one(
            {"id": cand["id"], "user_id": cand["user_id"]},
            {"$set": {
                "archived_at": now,
                "archived_reason": "weak_verdict_no_action",
                "auto_dial": False,
                "updated_at": now,
            }},
        )
        logger.info(f"weak-verdict auto-archive: archived {cand.get('first_name')} {cand.get('last_name')} ({cand['id']})")


def stop_scheduler() -> None:
    if state._scheduler:
        state._scheduler.shutdown(wait=False)
        state._scheduler = None
