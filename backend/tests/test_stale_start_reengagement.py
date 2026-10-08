"""Stale-start re-engagement.

The case behind it: a starter was booked to start and never confirmed. The lapsed sweep archived
him; two months later he texted "👀", unarchive_on_contact put him back in
TRAINING with the old date still on the record, and the SMS agent — whose
stage goal unconditionally chases a YES, reading a history whose lines carry
no dates — replied "are you still good to come in today at 1:00 PM?".

These tests pin the gate: once training_start_at is more than 3 hours gone,
no surface may chase attendance for it — the goal flips to re-engagement
(or concierge, if they had confirmed), "next Monday" is never a past date,
and every history line the model reads is stamped with when it was sent.

    DB_NAME=cgrecruit_local_test backend/venv/bin/python -m pytest backend/tests/test_stale_start_reengagement.py
"""
import sys
import unittest
from unittest import mock
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import email_replies  # noqa: E402
import training_sms  # noqa: E402
from training_sms import (  # noqa: E402
    ET,
    STAGE_GOAL_TRAINING_STALE_CONFIRMED,
    STAGE_GOAL_TRAINING_STALE_UNCONFIRMED,
    STAGE_GOALS,
    _format_history,
    _llm_ctx_block,
    _llm_system_prompt,
    _next_monday_iso,
    _skip_no_cohort,
    _start_is_stale,
)

NOW = datetime(2026, 8, 26, 7, 8, tzinfo=ET)


def _iso(dt: datetime) -> str:
    """Naive local ISO — the shape training_start_at actually has."""
    return dt.replace(tzinfo=None).isoformat()


def _cand(start_delta: timedelta, **over) -> dict:
    base = {
        "id": "c1",
        "first_name": "Nico",
        "stage": "TRAINING",
        "training_start_at": _iso(datetime.now(ET) + start_delta),
        "sms_confirmation_status": "sent",
    }
    base.update(over)
    return base


class StartIsStaleTests(unittest.TestCase):
    def test_future_start_is_not_stale(self):
        cand = {"training_start_at": "2026-08-31T13:00:00"}
        self.assertFalse(_start_is_stale(cand, NOW))

    def test_two_hours_after_start_is_still_the_watch_window(self):
        cand = {"training_start_at": "2026-08-26T05:30:00"}
        self.assertFalse(_start_is_stale(cand, NOW))

    def test_four_hours_after_start_is_stale(self):
        cand = {"training_start_at": "2026-08-26T03:00:00"}
        self.assertTrue(_start_is_stale(cand, NOW))

    def test_two_months_after_start(self):
        cand = {"training_start_at": "2026-06-29T13:00:00"}
        self.assertTrue(_start_is_stale(cand, NOW))

    def test_no_date_is_not_stale(self):
        self.assertFalse(_start_is_stale({"training_start_at": None}, NOW))
        self.assertFalse(_start_is_stale({"training_start_at": ""}, NOW))
        self.assertFalse(_start_is_stale({}, NOW))
        self.assertFalse(_start_is_stale(None, NOW))


class NextMondayTests(unittest.TestCase):
    def test_stale_training_date_never_yields_a_past_monday(self):
        # June 29 used to compute July 6 — months gone by the time a lapsed
        # no-show texts back, and the YES flow would have booked it.
        out = _next_monday_iso("2026-06-29T13:00:00")
        d = datetime.strptime(out, "%Y-%m-%d").date()
        today = datetime.now(ET).date()
        self.assertGreater(d, today)
        self.assertEqual(d.weekday(), 0)

    def test_future_training_date_keeps_monday_after_it(self):
        base = datetime.now(ET) + timedelta(days=10)
        out = _next_monday_iso(_iso(base))
        d = datetime.strptime(out, "%Y-%m-%d").date()
        self.assertGreater(d, base.date())
        self.assertEqual(d.weekday(), 0)

    def test_none_computes_from_now(self):
        out = _next_monday_iso(None)
        d = datetime.strptime(out, "%Y-%m-%d").date()
        self.assertGreater(d, datetime.now(ET).date())
        self.assertEqual(d.weekday(), 0)


class NoCohortWeekTests(unittest.TestCase):
    # A cancelled week must never be offered: the next real cohort is.
    def test_cancelled_monday_skips_to_next(self):
        with mock.patch("training_sms.NO_COHORT_MONDAYS", frozenset({"2099-01-05"})):
            self.assertEqual(_skip_no_cohort("2099-01-05"), "2099-01-12")

    def test_normal_monday_unchanged(self):
        with mock.patch("training_sms.NO_COHORT_MONDAYS", frozenset({"2099-01-05"})):
            self.assertEqual(_skip_no_cohort("2099-01-12"), "2099-01-12")

    def test_consecutive_cancelled_weeks(self):
        with mock.patch("training_sms.NO_COHORT_MONDAYS", frozenset({"2099-01-05", "2099-01-12"})):
            self.assertEqual(_skip_no_cohort("2099-01-05"), "2099-01-19")

    def test_next_monday_never_offers_a_cancelled_week(self):
        with mock.patch("training_sms.NO_COHORT_MONDAYS", frozenset({"2099-01-12"})):
            self.assertEqual(_next_monday_iso("2099-01-05T13:00:00"), "2099-01-19")


class SmsSystemPromptTests(unittest.TestCase):
    def test_stale_unconfirmed_gets_reengagement_goal(self):
        prompt = _llm_system_prompt(_cand(timedelta(days=-58)), {}, "follow_up", {})
        self.assertIn(STAGE_GOAL_TRAINING_STALE_UNCONFIRMED, prompt)
        self.assertNotIn("PRIMARY goal is to get them to confirm attendance", prompt)
        self.assertNotIn("keep nudging toward YES", prompt)

    def test_stale_confirmed_gets_concierge_goal(self):
        cand = _cand(timedelta(days=-58), sms_confirmation_status="confirmed")
        prompt = _llm_system_prompt(cand, {}, "follow_up", {})
        self.assertIn(STAGE_GOAL_TRAINING_STALE_CONFIRMED, prompt)

    def test_stale_beats_recruiter_override(self):
        # An override is authored for the upcoming-start case — it carries the
        # same confirm-for-today assumption as the default goal.
        enrichment = {"settings": {"ai_stage_prompts": {"TRAINING": {"sms": "Chase that YES hard."}}}}
        prompt = _llm_system_prompt(_cand(timedelta(days=-58)), {}, "follow_up", enrichment)
        self.assertNotIn("Chase that YES hard.", prompt)
        self.assertIn(STAGE_GOAL_TRAINING_STALE_UNCONFIRMED, prompt)

    def test_stale_replaces_the_no_ladder(self):
        # STEP 1 of the decline ladder says "nudge to still attend today".
        prompt = _llm_system_prompt(_cand(timedelta(days=-58)), {}, "no", {})
        self.assertNotIn("nudge to still attend today", prompt)
        self.assertIn("start date is in the past", prompt)

    def test_upcoming_start_keeps_the_original_goal(self):
        prompt = _llm_system_prompt(_cand(timedelta(days=3)), {}, "follow_up", {})
        self.assertIn(STAGE_GOALS["TRAINING"], prompt)
        self.assertNotIn(STAGE_GOAL_TRAINING_STALE_UNCONFIRMED, prompt)


class SmsContextBlockTests(unittest.TestCase):
    def test_stale_start_date_is_marked_passed(self):
        ctx = _llm_ctx_block(_cand(timedelta(days=-58)), {}, {})
        self.assertIn("THIS DATE HAS ALREADY PASSED", ctx)

    def test_upcoming_start_date_is_not_marked(self):
        ctx = _llm_ctx_block(_cand(timedelta(days=3)), {}, {})
        self.assertNotIn("ALREADY PASSED", ctx)

    def test_next_monday_in_stale_context_is_future(self):
        ctx = _llm_ctx_block(_cand(timedelta(days=-58)), {}, {})
        line = next(l for l in ctx.splitlines() if l.startswith("NEXT MONDAY"))
        d = datetime.strptime(line.rsplit(": ", 1)[1], "%Y-%m-%d").date()
        self.assertGreater(d, datetime.now(ET).date())


class FormatHistoryTests(unittest.TestCase):
    def test_lines_carry_send_dates(self):
        history = [
            {"direction": "out", "body": "see you today at 1:00 PM",
             "timestamp": "2026-06-29T10:02:00-04:00"},
            {"direction": "in", "body": "👀",
             "timestamp": "2026-08-26T07:08:00-04:00"},
        ]
        out = _format_history(history)
        self.assertIn("stamped with when it was sent", out)
        self.assertIn("Jun 29", out)
        self.assertIn("10:02 AM] BOT: see you today at 1:00 PM", out)
        self.assertIn("Aug 26", out)

    def test_missing_timestamp_degrades_to_bare_line(self):
        out = _format_history([{"direction": "in", "body": "hey"}])
        self.assertIn("\nSTARTER: hey", out)

    def test_empty_history_is_empty(self):
        self.assertEqual(_format_history([]), "")


class EmailPromptTests(unittest.TestCase):
    def test_stale_unconfirmed_swaps_the_email_goal(self):
        cand = _cand(timedelta(days=-58))
        prompt = email_replies._build_system_prompt(cand, {}, {})
        self.assertIn(email_replies.STAGE_GOAL_TRAINING_STALE_UNCONFIRMED, prompt)
        self.assertNotIn("Confirm attendance", prompt)

    def test_stale_confirmed_swaps_to_concierge(self):
        cand = _cand(timedelta(days=-58), sms_confirmation_status="confirmed")
        prompt = email_replies._build_system_prompt(cand, {}, {})
        self.assertIn(email_replies.STAGE_GOAL_TRAINING_STALE_CONFIRMED, prompt)

    def test_upcoming_start_keeps_the_email_goal(self):
        prompt = email_replies._build_system_prompt(_cand(timedelta(days=3)), {}, {})
        self.assertIn(email_replies.STAGE_GOALS["TRAINING"], prompt)

    def test_stale_context_marks_the_date_and_offers_a_future_monday(self):
        ctx = email_replies._build_context_block(_cand(timedelta(days=-58)), {}, {})
        self.assertIn("THIS DATE HAS ALREADY PASSED", ctx)
        line = next(l for l in ctx.splitlines() if l.startswith("NEXT MONDAY"))
        d = datetime.strptime(line.rsplit(": ", 1)[1], "%Y-%m-%d").date()
        self.assertGreater(d, datetime.now(ET).date())


if __name__ == "__main__":
    unittest.main()
