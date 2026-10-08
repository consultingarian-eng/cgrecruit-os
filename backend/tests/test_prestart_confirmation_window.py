"""Pre-start confirmation window — the case behind it.

Two starters booked for the same Monday texted in with ordinary questions days
early (starting pay, "will someone be with me?"). The stage goal chases a YES
unconditionally, so the assistant bolted "can you confirm with a YES so we
have you locked in?" onto its answers — and the reply stamped
sms_confirmation_status "confirmed" days before the day-of process (the
10 AM confirmation SMS) had begun. Attendance is only assessed on the
morning of the start day, never earlier.

These tests pin the gate: before the confirmation SMS has gone out (or the
start-morning window opened), no prompt on any channel may chase a YES, and
an inbound YES routes to the model as conversation instead of stamping the
roster confirmed.

    DB_NAME=cgrecruit_local_test backend/venv/bin/python -m pytest backend/tests/test_prestart_confirmation_window.py
"""
import inspect
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import email_replies  # noqa: E402
import training_sms  # noqa: E402
from training_sms import (  # noqa: E402
    ET,
    STAGE_GOAL_TRAINING_PRE_WINDOW,
    STAGE_GOALS,
    _confirm_process_open,
    _llm_system_prompt,
)


def _iso(dt: datetime) -> str:
    """Naive local ISO — the shape training_start_at actually has."""
    return dt.replace(tzinfo=None).isoformat()


def _cand(start_delta: timedelta, **over) -> dict:
    base = {
        "id": "c1",
        "first_name": "Zinnia",
        "stage": "TRAINING",
        "training_start_at": _iso(datetime.now(ET) + start_delta),
    }
    base.update(over)
    return base


class ConfirmProcessOpenTests(unittest.TestCase):
    def test_days_before_start_is_closed(self):
        self.assertFalse(_confirm_process_open(_cand(timedelta(days=3))))

    def test_confirmation_sms_sent_opens_it(self):
        cand = _cand(timedelta(days=3), sms_confirmation_sent_at="2026-09-14T10:00:00-04:00")
        self.assertTrue(_confirm_process_open(cand))

    def test_status_sent_alone_opens_it(self):
        self.assertTrue(_confirm_process_open(_cand(timedelta(days=3), sms_confirmation_status="sent")))

    def test_start_morning_window_opens_it_without_a_send(self):
        # 10:05 AM on a 1 PM start day: the chaser may be minutes from firing
        # (or have failed) — a YES here is day-of intent either way.
        self.assertTrue(_confirm_process_open(_cand(timedelta(hours=2))))

    def test_shortly_after_start_is_still_open(self):
        self.assertTrue(_confirm_process_open(_cand(timedelta(hours=-1))))

    def test_reschedule_recloses_it(self):
        # The reschedule flow unsets sms_confirmation_sent_at and sets status
        # "rescheduled" so the chaser re-fires before the NEW date — until it
        # does, an early YES for that date must not confirm either.
        cand = _cand(timedelta(days=6), sms_confirmation_status="rescheduled")
        self.assertFalse(_confirm_process_open(cand))

    def test_missing_everything_is_closed(self):
        self.assertFalse(_confirm_process_open({}))
        self.assertFalse(_confirm_process_open(None))


class SmsPromptTests(unittest.TestCase):
    def test_days_early_swaps_to_the_pre_window_goal(self):
        prompt = _llm_system_prompt(_cand(timedelta(days=3)), {}, "follow_up", {})
        self.assertIn(STAGE_GOAL_TRAINING_PRE_WINDOW, prompt)
        self.assertNotIn("PRIMARY goal is to get them to confirm attendance", prompt)
        self.assertNotIn("keep nudging toward YES", prompt)

    def test_unknown_intent_stops_steering_to_confirmation(self):
        prompt = _llm_system_prompt(_cand(timedelta(days=3)), {}, "unknown", {})
        self.assertNotIn("bring them back to confirming", prompt)

    def test_pre_window_beats_recruiter_override(self):
        # An override is authored for the day-of case — it carries the same
        # chase-a-YES assumption as the default goal.
        enrichment = {"settings": {"ai_stage_prompts": {"TRAINING": {"sms": "Chase that YES hard."}}}}
        prompt = _llm_system_prompt(_cand(timedelta(days=3)), {}, "follow_up", enrichment)
        self.assertNotIn("Chase that YES hard.", prompt)
        self.assertIn(STAGE_GOAL_TRAINING_PRE_WINDOW, prompt)

    def test_start_morning_keeps_the_original_goal(self):
        prompt = _llm_system_prompt(_cand(timedelta(hours=2)), {}, "follow_up", {})
        self.assertIn(STAGE_GOALS["TRAINING"], prompt)
        self.assertNotIn(STAGE_GOAL_TRAINING_PRE_WINDOW, prompt)

    def test_after_confirmation_sms_keeps_the_original_goal(self):
        cand = _cand(timedelta(days=3), sms_confirmation_status="sent")
        prompt = _llm_system_prompt(cand, {}, "follow_up", {})
        self.assertIn(STAGE_GOALS["TRAINING"], prompt)
        self.assertIn("keep nudging toward YES", prompt)

    def test_stale_start_still_wins_over_pre_window(self):
        prompt = _llm_system_prompt(_cand(timedelta(days=-58)), {}, "follow_up", {})
        self.assertNotIn(STAGE_GOAL_TRAINING_PRE_WINDOW, prompt)
        self.assertIn("ALREADY PASSED", prompt)


class YesBranchGateTests(unittest.TestCase):
    """The inbound YES handler must consult the window before stamping."""

    def _training_yes_segment(self) -> str:
        src = inspect.getsource(training_sms.handle_inbound)
        start = src.index('elif classification == "yes"')
        end = src.index('elif classification == "no"')
        return src[start:end]

    def test_confirmed_stamp_is_gated_on_the_window(self):
        seg = self._training_yes_segment()
        self.assertIn("_confirm_process_open(cand)", seg)
        # The gate must come BEFORE the stamp, as its guard.
        self.assertLess(
            seg.index("_confirm_process_open(cand)"),
            seg.index('update["sms_confirmation_status"] = "confirmed"'),
        )

    def test_early_yes_goes_to_the_model_not_a_canned_confirm(self):
        seg = self._training_yes_segment()
        gate = seg[seg.index("_confirm_process_open(cand)"):]
        gate = gate[:gate.index('update["sms_confirmation_status"] = "confirmed"')]
        self.assertIn('"follow_up"', gate)


class EmailPromptTests(unittest.TestCase):
    def test_days_early_swaps_the_email_goal(self):
        prompt = email_replies._build_system_prompt(_cand(timedelta(days=3)), {}, {})
        self.assertIn(email_replies.STAGE_GOAL_TRAINING_PRE_WINDOW, prompt)
        self.assertNotIn("Confirm attendance,", prompt)

    def test_start_morning_keeps_the_email_goal(self):
        prompt = email_replies._build_system_prompt(_cand(timedelta(hours=2)), {}, {})
        self.assertIn(email_replies.STAGE_GOALS["TRAINING"], prompt)

    def test_stale_still_wins_over_pre_window_on_email(self):
        prompt = email_replies._build_system_prompt(_cand(timedelta(days=-58)), {}, {})
        self.assertIn(email_replies.STAGE_GOAL_TRAINING_STALE_UNCONFIRMED, prompt)
        self.assertNotIn(email_replies.STAGE_GOAL_TRAINING_PRE_WINDOW, prompt)


if __name__ == "__main__":
    unittest.main()
