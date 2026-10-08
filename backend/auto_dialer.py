"""Backwards-compat facade — see /app/backend/dialer/ for the actual modules.

Every name the rest of the codebase imports from `auto_dialer` is re-exported
from the new sub-package. Private names (leading underscore) are aliased to
their renamed public counterparts in `dialer/`. Module-level singletons
(`_scheduler`, `_db`) are proxied via PEP 562 `__getattr__` so reads always
see the latest live value.
"""
from typing import Any

from dialer import (  # noqa: F401 — re-exports
    AUTO_PROMOTE_DELAY_MINUTES,
    REMINDER_KINDS,
    batch_dial_pipeline,
    cancel_appointment_reminders,
    cancel_auto_promote,
    cancel_form_reminder,
    cancel_pending_call_jobs,
    cancel_pending_retry_calls,
    fire_auto_promote,
    followup_after_call,
    gate_check_sweep,
    maybe_schedule_incomplete_retry,
    next_in_window,
    now_utc,
    parse_hhmm,
    place_call_now,
    booking_retry_sweep,
    schedule_booking_retry_sweep,
    schedule_training_lapsed_sweep,
    training_lapsed_sweep,
    unarchive_on_contact,
    rebook_watchlist_sweep,
    retry_chat_nudge_sweep,
    sched_call,
    sched_call_followup,
    sched_pre_call_sms,
    schedule_appointment_reminders,
    schedule_auto_promote,
    schedule_call_with_window,
    schedule_dnd_retry,
    schedule_form_reminder,
    schedule_gate_check_sweep,
    schedule_late_transcript_recovery,
    late_transcript_recovery_sweep,
    schedule_rebook_watchlist_sweep,
    schedule_session_confirm_sweep,
    session_confirm_sweep,
    schedule_retry_chat_nudge_sweep,
    schedule_unbooked_screened_sweep,
    unbooked_screened_sweep,
    send_appointment_reminder,
    send_form_reminder,
    send_pre_call_sms,
    settings_for,
    start_scheduler,
    stop_scheduler,
)
from dialer import state as _state  # noqa: F401

# Legacy private aliases — these underscore names were imported by name in
# tests / older callers. Public counterparts live in `dialer.*`.
_place_call_now = place_call_now
_followup_after_call = followup_after_call
_send_pre_call_sms = send_pre_call_sms
_send_appointment_reminder = send_appointment_reminder
_fire_auto_promote = fire_auto_promote
_rebook_watchlist_sweep = rebook_watchlist_sweep
_settings_for = settings_for
_REMINDER_KINDS = REMINDER_KINDS


def __getattr__(name: str) -> Any:
    """PEP 562 — lazily proxy `_scheduler` / `_db` so callers (incl. tests)
    that do `from auto_dialer import _scheduler` always see the latest binding,
    even after `start_scheduler()` mutates it."""
    if name in ("_scheduler", "_db"):
        return getattr(_state, name)
    raise AttributeError(f"module 'auto_dialer' has no attribute {name!r}")
