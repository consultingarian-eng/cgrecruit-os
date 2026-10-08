"""Availability computation: turn pipeline rules + blackouts + booked appointments into open slots.

Slots are authored in the *recruiter's* timezone (Settings → Region & Language → Timezone),
generated in that zone, then returned as UTC ISO strings for storage. Spoken/email labels
are formatted in the recruiter zone so candidates and the AI agent never hear "UTC".

Each generated sub-slot has a capacity (rule.capacity, default 50). Multiple candidates
can book the same slot until capacity is reached.
"""
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Tuple
from collections import Counter

# Minimum notice before a slot may be offered. Same-day booking is allowed —
# this is what stops a candidate being handed a time an hour after they applied.
DEFAULT_MIN_LEAD_MINUTES = 60

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover - fallback only
    ZoneInfo = None  # type: ignore


def _parse_hhmm(s: str):
    parts = (s or "09:00").split(":")
    return int(parts[0]), int(parts[1] if len(parts) > 1 else "0")


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _resolve_tz(tz_name: Optional[str]):
    if not tz_name or ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return timezone.utc


def _index_rules_by_weekday(rules: List[Dict[str, Any]]) -> Dict[int, List[Dict[str, Any]]]:
    """Group availability rules by weekday so we can look up a day's slots in O(1)."""
    by_day: Dict[int, List[Dict[str, Any]]] = {}
    for r in rules:
        by_day.setdefault(int(r.get("weekday", 0)), []).append(r)
    return by_day


# A booking window may never stretch further than this past its configured size,
# however many dead days it runs into. Without a ceiling, a pipeline whose rules
# were cleared would extend forever looking for a day that runs interviews.
MAX_WINDOW_EXTENSION_DAYS = 7


def effective_window_days(
    pipeline: Dict[str, Any],
    base_days: int,
    now_local: datetime,
) -> int:
    """Widen a booking window by one day for every day inside it that runs no
    interviews, so a weekend cannot eat the window.

    A flat "three days ahead" is three chances mid-week and one on a Friday:
    an office that runs Tue–Sat has Sunday and Monday dead, and a candidate screened
    on Friday would see Saturday and nothing else before the window shut. Each
    dead day inside the window pushes the far edge out by one, which keeps the
    *number of real opportunities* roughly constant rather than the calendar
    span — the thing that actually decides whether someone can book.
    """
    by_day = _index_rules_by_weekday(pipeline.get("availability_rules") or [])
    if not by_day:
        return base_days
    limit = max(0, int(base_days))
    ceiling = limit + MAX_WINDOW_EXTENSION_DAYS
    d = 0
    while d <= limit and limit < ceiling:
        if not by_day.get((now_local + timedelta(days=d)).weekday()):
            limit += 1
        d += 1
    return limit


def candidate_window_days(
    pipeline: Dict[str, Any],
    appt_settings: Dict[str, Any],
    tz_name: str,
    fallback_days: int = 14,
) -> int:
    """The slot horizon for a CANDIDATE-facing offer.

    `appointments.booking_days_offered` widened by `effective_window_days`, or
    `fallback_days` when no window is configured. Every candidate door — the
    voice tool, the web retry chat, the SMS screener — must pass its horizon
    through here: a window only the voice door honours is not a window, and the
    chat doors were quietly booking week-3+ slots, which attend far less often
    than same/next-day ones.
    """
    from datetime import datetime as _dt, timezone as _tz
    try:
        from zoneinfo import ZoneInfo as _ZI
        now_local = _dt.now(_ZI(tz_name))
    except Exception:
        now_local = _dt.now(_tz.utc)
    base = int((appt_settings or {}).get("booking_days_offered") or 0)
    if base <= 0:
        return max(1, int(fallback_days))
    return effective_window_days(pipeline, base, now_local)


def _count_booked_per_slot(booked_isos: List[str]) -> Counter:
    """Tally how many candidates already hold each exact instant — keyed on
    UTC ISO with microseconds zeroed so booking timestamps coalesce cleanly."""
    counts: Counter = Counter()
    for iso in booked_isos or []:
        d = _parse_iso(iso)
        if d:
            key = d.astimezone(timezone.utc).replace(microsecond=0).isoformat()
            counts[key] += 1
    return counts


def _parse_blackouts(blackouts: List[Dict[str, Any]]) -> List[Tuple[datetime, datetime]]:
    """Materialise blackout windows as (start_utc, end_utc) tuples for fast checks."""
    out: List[Tuple[datetime, datetime]] = []
    for b in blackouts:
        s = _parse_iso(b.get("start"))
        e = _parse_iso(b.get("end"))
        if s and e:
            out.append((s.astimezone(timezone.utc), e.astimezone(timezone.utc)))
    return out


def _generate_day_slots(
    day,
    rule: Dict[str, Any],
    tz,
    now_utc: datetime,
    duration_minutes: int,
    default_capacity: int,
    blackout_ranges: List[Tuple[datetime, datetime]],
    booked_counts: Counter,
    earliest_utc: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Walk one rule's window for one day, yielding slot dicts that aren't
    in the past, too soon, blacked-out, or fully booked."""
    sh, sm = _parse_hhmm(rule.get("start", "09:00"))
    slot_minutes = int(rule.get("slot_minutes") or duration_minutes or 30)
    capacity = int(rule.get("capacity") or default_capacity)
    cur_local = datetime(day.year, day.month, day.day, sh, sm, tzinfo=tz)
    # Each rule is one individual slot — end is always start + duration, regardless
    # of what may be stored in older DB documents (eliminates need to re-save after deploy).
    end_local = cur_local + timedelta(minutes=slot_minutes)
    cur_utc = cur_local.astimezone(timezone.utc)
    if cur_utc <= now_utc:
        return []
    # Same-day interviews are fine, ambushes are not: at 9:00 we don't offer
    # 9:15, but 2:00 PM the same afternoon is fair game.
    if earliest_utc is not None and cur_utc < earliest_utc:
        return []
    if any(bs <= cur_utc < be for (bs, be) in blackout_ranges):
        return []
    key = cur_utc.replace(microsecond=0).isoformat()
    remaining = capacity - booked_counts[key]
    if remaining <= 0:
        return []
    return [{
        "datetime": cur_utc.isoformat(),
        "label": cur_local.strftime("%a %b %-d at %-I:%M %p"),
        "duration_minutes": slot_minutes,
        "capacity": capacity,
        "capacity_remaining": remaining,
        # Per-slot meeting link (empty = pipeline default). Surfaced so slot
        # pickers/previews can show which link a booking will carry.
        "appointment_link_override": (rule.get("appointment_link_override") or "").strip(),
        "appointment_recruiter_override": (rule.get("appointment_recruiter_override") or "").strip(),
    }]


def compute_available_slots(
    pipeline: Dict[str, Any],
    booked_isos: List[str],
    days_ahead: int = 7,
    now: Optional[datetime] = None,
    max_slots: int = 60,
    tz_name: Optional[str] = None,
    default_capacity: int = 50,
    start_day: int = 0,
    min_lead_minutes: int = DEFAULT_MIN_LEAD_MINUTES,
) -> List[Dict[str, Any]]:
    """Return list of {datetime, label, capacity_remaining, duration_minutes} slots
    available within the next `days_ahead` days, expressed in the recruiter timezone.

    start_day=0 includes today; `min_lead_minutes` is what keeps that sane. The
    funnel dials within minutes of an application, so refusing every same-day
    slot threw away the hottest candidates — the real constraint is notice, not
    the calendar date."""
    duration = int(pipeline.get("appointment_duration_minutes") or 30)
    tz = _resolve_tz(tz_name)
    now = now or datetime.now(timezone.utc)
    now_local = now.astimezone(tz)

    by_day = _index_rules_by_weekday(pipeline.get("availability_rules") or [])
    booked_counts = _count_booked_per_slot(booked_isos)
    blackout_ranges = _parse_blackouts(pipeline.get("availability_blackouts") or [])

    earliest = now + timedelta(minutes=max(0, int(min_lead_minutes or 0)))

    out: List[Dict[str, Any]] = []
    for d_off in range(start_day, days_ahead + 1):
        day = (now_local + timedelta(days=d_off)).date()
        rules_for_day = by_day.get(day.weekday())
        if not rules_for_day:
            continue
        for rule in rules_for_day:
            out.extend(_generate_day_slots(
                day, rule, tz, now, duration, default_capacity,
                blackout_ranges, booked_counts, earliest_utc=earliest,
            ))
    # Chronological, not rule-insertion, order. Rules within a day sat in the
    # order the recruiter added them, so a 9:15 session created after a 1:00 PM
    # one listed second — and everything downstream slices positionally ("offer
    # the FIRST two", grids show the next six), quietly biasing offers away
    # from the soonest time on the day.
    out.sort(key=lambda s: s.get("datetime") or "")
    return out[:max_slots]


def slot_capacity_remaining(
    pipeline: Dict[str, Any],
    booked_isos: List[str],
    slot_iso: str,
    default_capacity: int = 50,
    tz_name: Optional[str] = None,
) -> int:
    """Return how many seats are left at a specific slot instant. -1 if the slot is
    not produced by any rule (i.e. invalid weekday or start time)."""
    target = _parse_iso(slot_iso)
    if not target:
        return -1
    target_utc = target.astimezone(timezone.utc).replace(microsecond=0)
    booked_counts: Counter = Counter()
    for iso in booked_isos or []:
        d = _parse_iso(iso)
        if d:
            booked_counts[d.astimezone(timezone.utc).replace(microsecond=0).isoformat()] += 1
    rules = pipeline.get("availability_rules") or []
    tz = _resolve_tz(tz_name)
    target_local = target_utc.astimezone(tz)
    wd = target_local.weekday()
    # Match by weekday AND exact HH:MM in the recruiter timezone — prevents accepting
    # hallucinated times (e.g. 12:00 PM) when only 09:15 AM is configured.
    matching_caps = []
    for r in rules:
        if int(r.get("weekday", 0)) != wd:
            continue
        rh, rm = _parse_hhmm(r.get("start", "09:00"))
        if target_local.hour == rh and target_local.minute == rm:
            matching_caps.append(int(r.get("capacity") or default_capacity))
    if not matching_caps:
        return -1
    capacity = max(matching_caps)
    return capacity - booked_counts.get(target_utc.isoformat(), 0)


def resolve_slot_link(
    pipeline: Dict[str, Any],
    slot_iso: Optional[str],
    tz_name: Optional[str] = None,
) -> Optional[str]:
    """Return the per-slot `appointment_link_override` of the availability rule
    that produces `slot_iso`, or None when that slot has no override (callers
    fall back to pipeline.appointment_link). Matching mirrors
    `slot_capacity_remaining`: same weekday AND exact HH:MM in the recruiter
    timezone. A naive `slot_iso` is treated as already being recruiter-local
    (the convention for recruiter-entered appointment times)."""
    target = _parse_iso(slot_iso)
    if not target:
        return None
    tz = _resolve_tz(tz_name)
    target_local = target.replace(tzinfo=tz) if target.tzinfo is None else target.astimezone(tz)
    wd = target_local.weekday()
    for r in pipeline.get("availability_rules") or []:
        if int(r.get("weekday", 0)) != wd:
            continue
        rh, rm = _parse_hhmm(r.get("start", "09:00"))
        if target_local.hour == rh and target_local.minute == rm:
            link = (r.get("appointment_link_override") or "").strip()
            if link:
                return link
    return None


def resolve_slot_recruiter(
    pipeline: Dict[str, Any],
    slot_iso: Optional[str],
    tz_name: Optional[str] = None,
) -> Optional[str]:
    """Return the per-slot `appointment_recruiter_override` of the availability
    rule that produces `slot_iso`, or None when that slot has no override
    (callers fall back to pipeline.appointment_recruiter). Same matching as
    resolve_slot_link: weekday + exact HH:MM in the recruiter timezone."""
    target = _parse_iso(slot_iso)
    if not target:
        return None
    tz = _resolve_tz(tz_name)
    target_local = target.replace(tzinfo=tz) if target.tzinfo is None else target.astimezone(tz)
    wd = target_local.weekday()
    for r in pipeline.get("availability_rules") or []:
        if int(r.get("weekday", 0)) != wd:
            continue
        rh, rm = _parse_hhmm(r.get("start", "09:00"))
        if target_local.hour == rh and target_local.minute == rm:
            name = (r.get("appointment_recruiter_override") or "").strip()
            if name:
                return name
    return None


def migrate_rules_to_single_slots(pipeline: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Expand any old wide-window rules (start→end every slot_minutes) into individual
    single-slot rules (start = slot time, end = start + duration). Returns the new rule
    list, or the original if nothing needed expanding. Safe to call on already-migrated data."""
    duration = int(pipeline.get("appointment_duration_minutes") or 30)
    rules = pipeline.get("availability_rules") or []
    expanded = []
    changed = False
    for rule in rules:
        sh, sm = _parse_hhmm(rule.get("start", "09:00"))
        eh, em = _parse_hhmm(rule.get("end", "09:00"))
        slot_min = int(rule.get("slot_minutes") or duration or 30)
        start_total = sh * 60 + sm
        end_total = eh * 60 + em
        capacity = rule.get("capacity", 50)
        weekday = rule.get("weekday", 0)
        # Already a single-slot rule — end equals start + slot_min (within 1 min tolerance).
        if abs(end_total - start_total - slot_min) <= 1:
            expanded.append(rule)
            continue
        # Wide window — expand into individual slots.
        changed = True
        cur = start_total
        while cur < end_total:
            slot_end = cur + slot_min
            hh = cur // 60 % 24
            mm = cur % 60
            eh2 = slot_end // 60 % 24
            em2 = slot_end % 60
            expanded.append({
                "weekday": weekday,
                "start": f"{hh:02d}:{mm:02d}",
                "end": f"{eh2:02d}:{em2:02d}",
                "slot_minutes": slot_min,
                "capacity": capacity,
            })
            cur += slot_min
    return expanded if changed else rules
