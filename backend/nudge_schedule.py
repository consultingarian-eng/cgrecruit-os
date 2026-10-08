"""When to chase a candidate who has gone quiet mid-screening.

There was one nudge, fired once ever per candidate and never re-armed. That was
survivable while the AI phone call was the real screening and the chat was a
sideshow. With the call reduced to a ping, this sequence *is* the follow-up —
if it fires once and gives up, a candidate who answered two questions and got
distracted is simply lost.

Three attempts, at 2, 6 and 24 hours after they last said anything. The spacing
is deliberate: the first catches someone who put their phone down mid-thought,
the second catches the other end of a working day, the third catches tomorrow.
After that, stop — a fourth text to someone who has ignored three is not
persistence, it is the reason people report a number as spam.

Quiet hours matter more here than anywhere else in the system, because unlike a
phone call a text can technically be sent at 3am. It shouldn't be. A nudge due
during the night is held until the morning rather than dropped, since the point
is to arrive when it will actually be read.

**Quiet hours govern US STARTING a conversation, never us continuing one.** If a
candidate texts at 10:38pm the assistant answers, finishes their screening, books
the slot and sends the confirmation and the calendar invite — at 10:38pm. Someone
who is awake and engaging is the best moment this funnel ever gets, and going
silent on them until morning to observe our own politeness rule would be
self-defeating. Nothing in the inbound reply path or in `send_candidate_sms`
consults this module, and `test_nudge_schedule` asserts that it stays that way.

Pure functions — no database, no clock of its own. Everything takes `now`.
"""
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytz
from app_tz import app_zone, default_tz_name  # APP_TIMEZONE (noqa: F401)

# Hours after the candidate's last message. Index = how many nudges already sent.
NUDGE_OFFSETS_HOURS: List[int] = [2, 6, 24]
MAX_NUDGES = len(NUDGE_OFFSETS_HOURS)

# Wider than the 09:00-19:00 calling window — a text is not an interruption in
# the way a ringing phone is — but not around the clock either.
QUIET_START = time(22, 30)   # nothing new after this
QUIET_END = time(7, 0)       # ...until this

# Past this, they aren't coming back to a half-finished screening, and the
# nudge would read as a message from a company they'd forgotten applying to.
GIVE_UP_AFTER_DAYS = 3


def _tz(tz_name: Optional[str]):
    try:
        return pytz.timezone(tz_name or default_tz_name())
    except Exception:
        return pytz.timezone(default_tz_name())


def in_quiet_hours(when: datetime, tz_name: Optional[str] = None) -> bool:
    """Is this a time of day we should not be texting a stranger?"""
    local = when.astimezone(_tz(tz_name)).time()
    # The window wraps midnight, so it's "after the start OR before the end".
    return local >= QUIET_START or local < QUIET_END


def next_sendable_time(when: datetime, tz_name: Optional[str] = None) -> datetime:
    """`when`, or the start of the next window if `when` falls inside quiet hours.

    Held rather than dropped: a nudge that came due at 2am is still worth
    sending at 7am, and skipping it would silently shorten the sequence.
    """
    if not in_quiet_hours(when, tz_name):
        return when
    tz = _tz(tz_name)
    local = when.astimezone(tz)
    target = local.replace(hour=QUIET_END.hour, minute=QUIET_END.minute, second=0, microsecond=0)
    if local.time() >= QUIET_START:
        target += timedelta(days=1)
    return tz.normalize(target).astimezone(timezone.utc)


def due_at(last_activity: datetime, nudges_sent: int, tz_name: Optional[str] = None) -> Optional[datetime]:
    """When nudge number `nudges_sent + 1` should go out. None once we're done."""
    if nudges_sent >= MAX_NUDGES:
        return None
    return next_sendable_time(
        last_activity + timedelta(hours=NUDGE_OFFSETS_HOURS[nudges_sent]), tz_name
    )


def should_nudge_now(
    last_activity: datetime,
    nudges_sent: int,
    now: datetime,
    tz_name: Optional[str] = None,
) -> bool:
    """The whole decision, in one place so the sweep has no timing logic of its own."""
    if nudges_sent >= MAX_NUDGES:
        return False
    if now - last_activity > timedelta(days=GIVE_UP_AFTER_DAYS):
        return False
    if in_quiet_hours(now, tz_name):
        return False
    due = due_at(last_activity, nudges_sent, tz_name)
    return bool(due and now >= due)


def describe(nudges_sent: int) -> str:
    """Human label for logs and for the candidate record."""
    if nudges_sent >= MAX_NUDGES:
        return "sequence complete"
    return f"nudge {nudges_sent + 1} of {MAX_NUDGES} (+{NUDGE_OFFSETS_HOURS[nudges_sent]}h)"


# ── speed to contact ────────────────────────────────────────────────────────

def response_delay_seconds(sent_at: Any, replied_at: Any) -> Optional[float]:
    """Seconds between reaching out and hearing back. None if either is unusable.

    The measurement the whole chat-first change is being judged on: candidates
    are added the instant they apply, so this is effectively apply-to-engagement.
    """
    from models import parse_appointment_at  # tolerant of both stored shapes

    a, b = parse_appointment_at(sent_at), parse_appointment_at(replied_at)
    if not a or not b:
        return None
    delta = (b - a).total_seconds()
    # A reply timestamped before the outreach means clock skew or a mis-stamp,
    # not a prescient candidate. Better no number than a negative one.
    return delta if delta >= 0 else None


def summarise_delays(delays: List[float]) -> Dict[str, Any]:
    """Median, mean and a reply-rate-friendly count.

    Median leads because this distribution has a long tail — a handful of people
    reply four days later and drag a mean somewhere unrecognisable.
    """
    clean = sorted(d for d in delays if d is not None and d >= 0)
    if not clean:
        return {"replies": 0, "median_seconds": None, "mean_seconds": None,
                "under_5_min": 0, "under_1_hour": 0}
    n = len(clean)
    median = clean[n // 2] if n % 2 else (clean[n // 2 - 1] + clean[n // 2]) / 2
    return {
        "replies": n,
        "median_seconds": round(median),
        "mean_seconds": round(sum(clean) / n),
        "under_5_min": sum(1 for d in clean if d <= 300),
        "under_1_hour": sum(1 for d in clean if d <= 3600),
    }
