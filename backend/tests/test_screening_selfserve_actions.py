"""Tests for making the inbound SMS/email AI aware of the moment-of-apply
self-serve options (chat now / instant callback / pick a time) and letting it
act on them directly, mirroring how APPOINTMENT-stage replies can already
[BOOK:]/[CANCEL] an interview.

Covers:
  - training_sms._extract_markers picks up [REQUEST_CALLBACK] / [SCHEDULE_CALL:]
  - training_sms._handle_reply_markers dispatches to the right action for
    SCREENING/APPLICANT stage, and falls back to an apology on failure
  - email_replies._take_action handles request_callback / schedule_call the
    same way, via the ACTION: convention already used for cancel/reschedule
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import training_sms  # noqa: E402
import email_replies  # noqa: E402


class ExtractMarkersTests(unittest.TestCase):
    def test_request_callback_marker(self):
        cleaned, proposed, book, cancelled, callback, sched, _withdrawn = training_sms._extract_markers(
            "On it — expect a call shortly! [REQUEST_CALLBACK]"
        )
        self.assertEqual(cleaned, "On it — expect a call shortly!")
        self.assertTrue(callback)
        self.assertIsNone(sched)

    def test_schedule_call_marker(self):
        cleaned, proposed, book, cancelled, callback, sched, _withdrawn = training_sms._extract_markers(
            "Sounds good, tomorrow morning it is! [SCHEDULE_CALL: 2026-03-04T13:00:00+00:00]"
        )
        self.assertEqual(cleaned, "Sounds good, tomorrow morning it is!")
        self.assertFalse(callback)
        self.assertEqual(sched, "2026-03-04T13:00:00+00:00")

    def test_no_markers_present(self):
        cleaned, proposed, book, cancelled, callback, sched, _withdrawn = training_sms._extract_markers("Just a normal reply.")
        self.assertFalse(callback)
        self.assertIsNone(sched)
        self.assertEqual(cleaned, "Just a normal reply.")


class TrainingSmsHandleReplyMarkersTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # The SCREENING/APPLICANT branch reads the office's settings to decide
        # whether text is the screening channel. Keep that off the database:
        # empty settings = the default (voice-first) mode.
        patcher = patch("deps.resolve_settings", new=AsyncMock(return_value={}))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cand = {
            "id": "cand-1", "user_id": "user-1", "phone": "+15551234567",
            "screening_status": "no_answer",
        }

    async def test_callback_requested_success(self):
        with patch("training_sms._apply_sms_callback_request", new=AsyncMock(return_value=True)) as mocked:
            prose, proposed = await training_sms._handle_reply_markers(
                None, self.cand, {}, "SCREENING", "Sure! [REQUEST_CALLBACK]", "call me now"
            )
        mocked.assert_awaited_once()
        self.assertEqual(prose, "Sure!")

    async def test_callback_requested_failure_swaps_prose(self):
        with patch("training_sms._apply_sms_callback_request", new=AsyncMock(return_value=False)):
            prose, proposed = await training_sms._handle_reply_markers(
                None, self.cand, {}, "APPLICANT", "Sure! [REQUEST_CALLBACK]", "call me now"
            )
        self.assertIn("Sorry", prose)

    async def test_schedule_call_success(self):
        iso = "2026-03-04T13:00:00+00:00"
        with patch("training_sms._apply_sms_schedule_call", new=AsyncMock(return_value=True)) as mocked:
            prose, proposed = await training_sms._handle_reply_markers(
                None, self.cand, {}, "SCREENING", f"Great! [SCHEDULE_CALL: {iso}]", "call me tomorrow morning"
            )
        mocked.assert_awaited_once_with(None, self.cand, iso)
        self.assertEqual(prose, "Great!")

    async def test_schedule_call_failure_swaps_prose(self):
        # Well-formed enough to match SCHEDULE_CALL_RE's charset, but a bogus
        # instant — exercises the "extracted but rejected downstream" path
        # rather than "never extracted at all".
        with patch("training_sms._apply_sms_schedule_call", new=AsyncMock(return_value=False)):
            prose, proposed = await training_sms._handle_reply_markers(
                None, self.cand, {}, "SCREENING",
                "Great! [SCHEDULE_CALL: 9999-99-99T99:99:99+00:00]", "call me then",
            )
        self.assertIn("Sorry", prose)

    async def test_markers_ignored_outside_screening_applicant(self):
        # FORM/CLOSE/TRAINING stage candidates should never trigger these actions
        # even if the LLM hallucinates a marker.
        with patch("training_sms._apply_sms_callback_request", new=AsyncMock()) as mocked:
            prose, proposed = await training_sms._handle_reply_markers(
                None, self.cand, {}, "FORM", "Sure! [REQUEST_CALLBACK]", "call me now"
            )
        mocked.assert_not_called()


class ApplySmsActionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_request_rejects_completed_screening(self):
        cand = {"id": "c1", "user_id": "u1", "phone": "+15551234567", "screening_status": "approved"}
        ok = await training_sms._apply_sms_callback_request(None, cand)
        self.assertFalse(ok)

    async def test_callback_request_places_call(self):
        cand = {"id": "c1", "user_id": "u1", "phone": "+15551234567", "screening_status": "no_answer"}
        with patch("dialer.place_call.place_call_now", new=AsyncMock(return_value={"status": "initiated"})) as mocked:
            ok = await training_sms._apply_sms_callback_request(None, cand)
        self.assertTrue(ok)
        # candidate_initiated=True is required: a callback the candidate asked for
        # by text must bypass the call window, or we'd silently queue it for later.
        mocked.assert_awaited_once_with("u1", "c1", candidate_initiated=True)

    async def test_schedule_call_rejects_too_soon(self):
        cand = {"id": "c1", "user_id": "u1", "phone": "+15551234567", "screening_status": "no_answer"}
        iso = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        ok = await training_sms._apply_sms_schedule_call(None, cand, iso)
        self.assertFalse(ok)

    async def test_schedule_call_reschedules(self):
        cand = {"id": "c1", "user_id": "u1", "phone": "+15551234567", "screening_status": "no_answer"}
        iso = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
        with patch("dialer.queue.schedule_call_with_window", new=AsyncMock(return_value={"status": "scheduled"})) as mocked:
            ok = await training_sms._apply_sms_schedule_call(None, cand, iso)
        self.assertTrue(ok)
        _, kwargs = mocked.call_args
        self.assertTrue(kwargs["force"])


class EmailRepliesTakeActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_callback_success(self):
        cand = {"id": "c1", "user_id": "u1", "phone": "+15551234567", "screening_status": "no_answer"}
        with patch("dialer.place_call.place_call_now", new=AsyncMock(return_value={"status": "initiated"})):
            ok = await email_replies._take_action(None, cand, "request_callback", None)
        self.assertTrue(ok)

    async def test_request_callback_no_phone_fails(self):
        cand = {"id": "c1", "user_id": "u1", "phone": "", "screening_status": "no_answer"}
        ok = await email_replies._take_action(None, cand, "request_callback", None)
        self.assertFalse(ok)

    async def test_schedule_call_missing_proposed_date_fails(self):
        cand = {"id": "c1", "user_id": "u1", "phone": "+15551234567", "screening_status": "no_answer"}
        ok = await email_replies._take_action(None, cand, "schedule_call", None)
        self.assertFalse(ok)

    async def test_schedule_call_success(self):
        cand = {"id": "c1", "user_id": "u1", "phone": "+15551234567", "screening_status": "no_answer"}
        iso = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
        with patch("dialer.queue.schedule_call_with_window", new=AsyncMock(return_value={"status": "scheduled"})) as mocked:
            ok = await email_replies._take_action(None, cand, "schedule_call", iso)
        self.assertTrue(ok)
        mocked.assert_awaited_once()

    async def test_schedule_call_too_far_out_fails(self):
        cand = {"id": "c1", "user_id": "u1", "phone": "+15551234567", "screening_status": "no_answer"}
        iso = (datetime.now(timezone.utc) + timedelta(days=40)).isoformat()
        ok = await email_replies._take_action(None, cand, "schedule_call", iso)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
