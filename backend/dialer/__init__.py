"""Auto-dialer package — split out of the monolithic auto_dialer.py.

Public API mirrored on the legacy `auto_dialer` module via re-export so every
existing `from auto_dialer import X` import keeps working.

Responsibility map:
- `state`       — module-level singletons (_scheduler, _db, now_utc, settings_for)
- `window`      — call-window math (parse_hhmm, next_in_window)
- `scheduler`   — APScheduler lifecycle (start_scheduler, stop_scheduler)
- `place_call`  — outbound dial w/ dual concurrency cap (place_call_now)
- `queue`       — sched_call, schedule_call_with_window, batch_dial_pipeline
- `pre_call_sms`— warmup SMS sched + send
- `follow_up`   — post-call sync, transcript+summary, retry decision
- `retry`       — maybe_schedule_incomplete_retry, cancel_pending_retry_calls
- `reminders`   — appointment reminders (built-in + custom)
- `auto_promote`— delayed APPOINTMENT promotion on screening success
- `rebook_sweep`— hourly re-notify of rebook-watchlist candidates
- `retry_chat_nudge_sweep` — hourly nudge for abandoned retry-chat screenings
- `transcript_recovery` — status-blind re-poll for screenings EL never handed over
"""
from .auto_promote import (
    AUTO_PROMOTE_DELAY_MINUTES,
    cancel_auto_promote,
    fire_auto_promote,
    schedule_auto_promote,
)
from .follow_up import followup_after_call, sched_call_followup
from .place_call import place_call_now
from .pre_call_sms import sched_pre_call_sms, send_pre_call_sms
from .queue import (
    batch_dial_pipeline,
    sched_call,
    schedule_call_with_window,
)
from .booking_retry_sweep import (
    booking_retry_sweep,
    schedule_booking_retry_sweep,
)
from .training_archive_sweep import (
    schedule_training_lapsed_sweep,
    training_lapsed_sweep,
    unarchive_on_contact,
)
from .rebook_sweep import (
    rebook_watchlist_sweep,
    schedule_rebook_watchlist_sweep,
    schedule_unbooked_screened_sweep,
    unbooked_screened_sweep,
)
from .retry_chat_nudge_sweep import (
    retry_chat_nudge_sweep,
    schedule_retry_chat_nudge_sweep,
)
from .gate_check_sweep import (
    gate_check_sweep,
    schedule_gate_check_sweep,
)
from .session_confirm_sweep import (
    session_confirm_sweep,
    schedule_session_confirm_sweep,
)
from .transcript_recovery import (
    late_transcript_recovery_sweep,
    schedule_late_transcript_recovery,
)
from .reminders import (
    REMINDER_KINDS,
    cancel_appointment_reminders,
    cancel_form_reminder,
    schedule_appointment_reminders,
    schedule_form_reminder,
    send_appointment_reminder,
    send_form_reminder,
)
from .retry import (
    cancel_pending_call_jobs,
    cancel_pending_retry_calls,
    maybe_schedule_incomplete_retry,
    schedule_dnd_retry,
)
from .scheduler import start_scheduler, stop_scheduler
from .state import now_utc, settings_for
from .window import next_in_window, parse_hhmm

__all__ = [
    # State + helpers
    "now_utc", "settings_for",
    # Window math
    "parse_hhmm", "next_in_window",
    # Scheduler lifecycle
    "start_scheduler", "stop_scheduler",
    # Call placement + queueing
    "place_call_now", "sched_call", "schedule_call_with_window", "batch_dial_pipeline",
    # Pre-call SMS
    "sched_pre_call_sms", "send_pre_call_sms",
    # Follow-up
    "sched_call_followup", "followup_after_call",
    # Retry policy
    "maybe_schedule_incomplete_retry", "schedule_dnd_retry",
    "cancel_pending_retry_calls", "cancel_pending_call_jobs",
    # Reminders
    "REMINDER_KINDS", "send_appointment_reminder",
    "cancel_appointment_reminders", "schedule_appointment_reminders",
    "send_form_reminder", "cancel_form_reminder", "schedule_form_reminder",
    # Auto-promote
    "AUTO_PROMOTE_DELAY_MINUTES", "schedule_auto_promote", "cancel_auto_promote", "fire_auto_promote",
    # Rebook sweep
    "rebook_watchlist_sweep", "schedule_rebook_watchlist_sweep",
    "unbooked_screened_sweep", "schedule_unbooked_screened_sweep",
    # Booking-window retry sweep
    "booking_retry_sweep", "schedule_booking_retry_sweep",
    # Training-lapsed archive sweep (4 days past the training date)
    "training_lapsed_sweep", "schedule_training_lapsed_sweep", "unarchive_on_contact",
    # Abandoned retry-chat nudge sweep
    "retry_chat_nudge_sweep", "schedule_retry_chat_nudge_sweep",
    # Gate-check sweep (booked but never screened)
    "gate_check_sweep", "schedule_gate_check_sweep",
    # Late transcript recovery (screenings ElevenLabs never handed over)
    "late_transcript_recovery_sweep", "schedule_late_transcript_recovery",
    # Session-unconfirmed alarm
    "session_confirm_sweep", "schedule_session_confirm_sweep",
]
