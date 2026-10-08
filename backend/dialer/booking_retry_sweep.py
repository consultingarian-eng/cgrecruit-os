"""Booking-retry sweep — texts a candidate their booking page once the booking
window has rolled far enough forward to actually contain a slot.

The window (`appointments.booking_days_offered`) deliberately hides slots that
are further out than a few days, because a booking many days out is easier
to forget. That leaves a gap the old code filled
by offering a distant slot anyway: someone who wants an interview, on a day when
everything near is full. Rather than book them badly or hand them to a human,
`/public/send-booking-link` parks them with a `booking_retry_at`, and this sweep
picks them up.

A parked candidate is only sent the link when there is something to book. If the
window is still empty at the retry time the date rolls forward again instead of
burning the attempt, so nobody is sent a page with an empty grid — which is the
one outcome guaranteed to stop them coming back.
"""
import logging
import company_profile
from datetime import timedelta
from typing import Any, Dict

from . import state
from .state import now_utc, settings_for

logger = logging.getLogger("auto_dialer")


async def booking_retry_sweep() -> Dict[str, Any]:
    """Send the booking page to everyone whose parked retry is due and whose
    window now holds a slot; roll the rest forward."""
    if state._db is None:
        return {"checked": 0, "sent": 0, "rolled": 0}
    db = state._db

    due = await db.candidates.find(
        {
            "booking_retry_at": {"$ne": None, "$lte": now_utc().isoformat()},
            "booking_retry_sent_at": None,
            # Booked, closed or archived in the meantime — the retry is moot.
            "appointment_at": None,
            "archived_at": None,
            "stage": {"$nin": ["CLOSE", "TRAINING"]},
        },
        {"_id": 0},
    ).to_list(500)

    sent = rolled = dropped = 0
    for cand in due:
        try:
            settings = await settings_for(cand["user_id"], cand.get("pipeline_id"))
            prefs = (settings.get("booking_preferences") or {})
            wait_days = max(1, int(prefs.get("booking_retry_days") or 2))
            max_attempts = max(1, int(prefs.get("booking_retry_max_attempts") or 3))
            attempts = int(cand.get("booking_retry_attempts") or 0)

            pipe = await db.pipelines.find_one({"id": cand.get("pipeline_id")}, {"_id": 0})
            if not pipe:
                continue

            from routes.public import window_has_slots
            if not await window_has_slots(pipe, settings):
                if attempts + 1 >= max_attempts:
                    # Stop rolling silently forever — clear the park so the
                    # candidate shows up as ordinary unbooked work for a human.
                    await db.candidates.update_one(
                        {"id": cand["id"]},
                        {"$set": {"booking_retry_at": None,
                                  "booking_retry_attempts": attempts + 1}},
                    )
                    dropped += 1
                    await _alert_park_dropped(
                        cand, pipe,
                        f"The booking window stayed empty through {attempts + 1} retries, "
                        f"so the promised automatic text was never sent. Add availability, "
                        f"then send them the booking link manually.",
                    )
                    logger.info(
                        f"booking retry: giving up on {cand['id']} after "
                        f"{attempts + 1} rolls — window still empty")
                    continue
                await db.candidates.update_one(
                    {"id": cand["id"]},
                    {"$set": {
                        "booking_retry_at": (now_utc() + timedelta(days=wait_days)).isoformat(),
                        "booking_retry_attempts": attempts + 1,
                    }},
                )
                rolled += 1
                continue

            res = await _send_link(db, cand, pipe, settings)
            if res.get("status") == "sent":
                await db.candidates.update_one(
                    {"id": cand["id"]},
                    {"$set": {"booking_retry_sent_at": now_utc().isoformat(),
                              "booking_retry_at": None,
                              "booking_retry_attempts": attempts + 1}},
                )
                sent += 1
            else:
                # Opted out, no sender, Twilio down — don't spin on it.
                await db.candidates.update_one(
                    {"id": cand["id"]},
                    {"$set": {"booking_retry_at": None,
                              "booking_retry_attempts": attempts + 1}},
                )
                dropped += 1
                await _alert_park_dropped(
                    cand, pipe,
                    f"Sending their booking link failed ({(res or {}).get('reason') or (res or {}).get('status') or 'send error'}). "
                    f"They were promised the page would arrive automatically — send it manually or call them.",
                )
                logger.warning(f"booking retry send failed for {cand['id']}: {res}")
        except Exception as e:
            logger.warning(f"booking retry sweep failed for {cand.get('id')}: {e}")

    if due:
        logger.info(f"booking retry sweep: {len(due)} due, {sent} sent, "
                    f"{rolled} rolled forward, {dropped} dropped")
    return {"checked": len(due), "sent": sent, "rolled": rolled, "dropped": dropped}


async def _alert_park_dropped(cand: Dict[str, Any], pipe: Dict[str, Any], body: str) -> None:
    """Ring the bell when a parked candidate is dropped from the retry loop.

    These candidates cleared screening, wanted to book, and were told out loud
    that a text would arrive automatically. Dropping them with only a log line
    was a silent loss of the funnel's warmest unbooked people — nobody on the
    team was ever told the promise had been broken."""
    try:
        from notifications_service import create_notification
        nm = f"{cand.get('first_name', '')} {cand.get('last_name', '')}".strip() or "A candidate"
        await create_notification(
            cand.get("user_id", ""), "booking.park_dropped",
            f"⏳ {nm} was promised a booking text that never sent",
            body=body,
            link=f"/?candidate={cand.get('id')}",
            candidate_id=cand.get("id"), pipeline_id=cand.get("pipeline_id"),
        )
    except Exception as e:
        logger.warning(f"park-dropped notification failed for {cand.get('id')}: {e}")


async def _send_link(db, cand: Dict[str, Any], pipe: Dict[str, Any],
                     settings: Dict[str, Any]) -> Dict[str, Any]:
    import os
    from sms_service import send_direct_sms

    base_url = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
    token = cand.get("public_token")
    if not (base_url and token):
        return {"status": "skipped", "reason": "no portal link available"}
    link = f"{base_url}/applicant/{token}"
    company = (settings.get("recruiter_profile") or {}).get("company_name") or company_profile.company_name()
    first = cand.get("first_name") or "there"
    body = (f"Hi {first}, it's {company} — some new interview times have just opened up. "
            f"Grab whichever suits you here: {link}")
    return await send_direct_sms(
        db, settings, cand.get("phone") or "", body,
        pipeline=pipe, template_key="booking_link_retry",
        user_id=cand.get("user_id", ""), candidate_id=cand.get("id", ""),
    )


def schedule_booking_retry_sweep() -> None:
    """Hourly — the window rolls by the day, so hourly is as fine-grained as the
    thing it is watching ever changes."""
    if state._scheduler is None:
        return
    try:
        state._scheduler.add_job(
            booking_retry_sweep,
            "interval",
            hours=1,
            id="booking-retry-sweep",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info("booking retry sweep scheduled (hourly)")
    except Exception as e:
        logger.warning(f"could not schedule booking retry sweep: {e}")
