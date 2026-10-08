"""The "we missed you" text must go out when the call is missed, not a day later.

For candidates whose FIRST call went unanswered, the message usually arrived
many hours after the miss, rarely inside 5 minutes, and for some never at all. The
copy, the template and the trigger all existed; the send lost a race.

`maybe_fire_screening_retry` decided eligibility by RE-READING
`candidate.screening_status`. Two other writers overwrite that field within
milliseconds of a failed call — `schedule_dnd_retry` (25s bypass redial) and
`maybe_schedule_incomplete_retry` (next attempt) both write 'queued'. So by the
time the sender looked, the answer was 'queued', which is not in the eligible
list, and it skipped. It then only fired once the whole call sequence was
exhausted. Separately, the auto-sync sweep — the path that actually runs in
production, because the ElevenLabs post-call webhook is unreliable — never
called the sender at all.

Fix: callers pass the outcome they just observed, and the sweep calls the sender.

Pure: DB, comms and the EL HTTP client are all mocked.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/unused")
os.environ.setdefault("DB_NAME", "test_unused")

import deps  # noqa: E402
from dialer import state  # noqa: E402
from dialer.scheduler import _auto_sync_stuck_conversations  # noqa: E402


def _cand(**over):
    base = {
        "id": "c1", "user_id": "u1", "first_name": "Rowan", "phone": "+15550001111",
        "email": "j@example.com", "appointment_at": None, "screening_status": "queued",
        "screening_attempts": 0, "screening_retry_sent_at": None, "pipeline_id": "p1",
    }
    base.update(over)
    return base


class SenderGateTests(unittest.IsolatedAsyncioTestCase):
    """deps.maybe_fire_screening_retry — who gets the text and who does not."""

    def _db_with(self, cand):
        db = MagicMock()
        db.candidates.find_one = AsyncMock(return_value=cand)
        db.candidates.update_one = AsyncMock()
        db.jobs.find_one = AsyncMock(return_value=None)
        return db

    async def _fire(self, cand, **kw):
        db = self._db_with(cand)
        email = AsyncMock(return_value={"status": "sent"})
        sms = AsyncMock(return_value={"status": "sent"})
        with patch("deps.db", db), \
             patch("deps.send_stage_email", email), \
             patch("deps.resolve_settings", AsyncMock(return_value={})), \
             patch("deps.get_or_create_settings", AsyncMock(return_value={})), \
             patch("sms_service.send_candidate_sms", sms), \
             patch("email_service.get_template_for_key",
                   MagicMock(return_value={"sms_body": "we missed you [Retry Link]"})), \
             patch("email_service.build_template_vars", MagicMock(return_value={})), \
             patch("email_service.render_template", MagicMock(return_value="we missed you")):
            res = await deps.maybe_fire_screening_retry("u1", "c1", **kw)
        return res, email, sms

    async def test_the_regression_fires_while_the_stored_status_already_says_queued(self):
        """The whole bug in one test: the redial scheduler has already written
        'queued', but the caller watched the call go unanswered. It must send."""
        res, email, sms = await self._fire(_cand(screening_status="queued"), outcome="no_answer")
        self.assertEqual(res["status"], "sent")
        self.assertEqual(email.await_count, 1)
        self.assertEqual(sms.await_count, 1)

    async def test_without_an_outcome_a_queued_candidate_is_still_skipped(self):
        """Old callers that pass no outcome keep the old, conservative reading."""
        res, email, _ = await self._fire(_cand(screening_status="queued"))
        self.assertEqual(res["status"], "skipped")
        self.assertEqual(email.await_count, 0)

    async def test_reading_the_status_still_works_when_no_outcome_is_passed(self):
        res, email, _ = await self._fire(_cand(screening_status="no_answer"))
        self.assertEqual(res["status"], "sent")
        self.assertEqual(email.await_count, 1)

    async def test_a_paused_candidate_is_never_texted_even_with_an_outcome(self):
        """A recruiter's pause outranks a call that was already ringing."""
        res, email, _ = await self._fire(_cand(screening_status="paused"), outcome="no_answer")
        self.assertEqual(res["status"], "skipped")
        self.assertEqual(email.await_count, 0)

    async def test_an_approved_or_rejected_candidate_is_never_texted(self):
        for status in ("approved", "rejected"):
            with self.subTest(status=status):
                res, email, _ = await self._fire(
                    _cand(screening_status=status), outcome="no_answer")
                self.assertEqual(res["status"], "skipped")
                self.assertEqual(email.await_count, 0)

    async def test_a_booked_candidate_is_never_told_they_were_missed(self):
        res, email, _ = await self._fire(
            _cand(appointment_at="2026-09-20T13:00:00+00:00"), outcome="no_answer")
        self.assertEqual(res["status"], "skipped")
        self.assertEqual(email.await_count, 0)

    async def test_it_still_only_ever_sends_once(self):
        res, email, _ = await self._fire(
            _cand(screening_retry_sent_at="2026-09-01T10:00:00+00:00"), outcome="no_answer")
        self.assertEqual(res["status"], "skipped")
        self.assertEqual(email.await_count, 0)

    async def test_the_three_attempt_cap_still_holds(self):
        res, email, _ = await self._fire(
            _cand(screening_attempts=3), outcome="no_answer")
        self.assertEqual(res["status"], "skipped")
        self.assertEqual(email.await_count, 0)


def _conv(minutes_ago=5):
    return {
        "id": "v1", "user_id": "u1", "candidate_id": "c1",
        "elevenlabs_conversation_id": "conv_x",
        "created_at": (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(),
        "call_type": "screening",
    }


class SweepFiresCommsTests(unittest.IsolatedAsyncioTestCase):
    """The auto-sync sweep is the live path; it must actually send the message."""

    async def asyncSetUp(self):
        self.db = MagicMock()
        self.db.conversations.update_one = AsyncMock()
        self.db.candidates.update_one = AsyncMock(
            return_value=MagicMock(matched_count=1))
        self.db.candidates.find_one = AsyncMock(return_value=_cand())
        self.db.jobs.find_one = AsyncMock(return_value={"title": "Sales Rep"})
        state._db = self.db
        self.addCleanup(setattr, state, "_db", None)

        self.comms = AsyncMock(return_value={"status": "sent"})
        self.sched = AsyncMock(return_value={"status": "scheduled"})
        self.dnd = AsyncMock(return_value={"status": "skipped"})
        patch("deps.maybe_fire_screening_retry", self.comms).start()
        patch("dialer.retry.maybe_schedule_incomplete_retry", self.sched).start()
        patch("dialer.retry.schedule_dnd_retry", self.dnd).start()
        self.addCleanup(patch.stopall)

    async def _run(self, payload):
        self.db.conversations.find.return_value.to_list = AsyncMock(return_value=[_conv()])
        with patch("voice_service.fetch_elevenlabs_conversation", return_value=payload):
            await _auto_sync_stuck_conversations()

    async def test_a_call_that_never_connected_gets_the_text_immediately(self):
        await self._run({
            "status": "done", "transcript": [],
            "metadata": {"call_duration_secs": 3, "termination_reason": "no-answer"},
        })
        self.assertEqual(self.comms.await_count, 1)
        self.assertEqual(self.comms.await_args.kwargs.get("outcome"), "no_answer")

    async def test_the_text_goes_out_before_the_next_call_is_queued(self):
        """Ordering matters: queueing writes 'queued' over the status, which is
        what used to swallow the send."""
        order = []
        self.comms.side_effect = lambda *a, **k: order.append("comms") or {"status": "sent"}
        self.sched.side_effect = lambda *a, **k: order.append("queue") or {"status": "scheduled"}
        await self._run({
            "status": "done", "transcript": [],
            "metadata": {"call_duration_secs": 3, "termination_reason": "no-answer"},
        })
        self.assertEqual(order, ["comms", "queue"])

    async def test_no_text_while_a_dnd_bypass_redial_is_seconds_away(self):
        """Texting and then immediately re-ringing reads as spam; whoever picks
        that redial up never needed the message."""
        self.dnd.return_value = {"status": "scheduled"}
        await self._run({
            "status": "done",
            "transcript": [{"role": "agent", "message": "Hi, this is Olivia"}],
            "metadata": {"call_duration_secs": 12, "termination_reason": "voicemail"},
        })
        self.assertEqual(self.comms.await_count, 0)

    async def test_the_text_goes_out_once_the_redial_has_also_failed(self):
        """On the redial itself schedule_dnd_retry reports it is already done
        for this attempt, so the message is no longer held back."""
        self.dnd.return_value = {"status": "skipped",
                                 "reason": "DND retry already done for this attempt"}
        await self._run({
            "status": "done",
            "transcript": [{"role": "agent", "message": "Hi, this is Olivia"}],
            "metadata": {"call_duration_secs": 12, "termination_reason": "voicemail"},
        })
        self.assertEqual(self.comms.await_count, 1)


if __name__ == "__main__":
    unittest.main()
