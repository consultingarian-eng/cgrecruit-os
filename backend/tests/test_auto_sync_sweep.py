"""Tests for the conversation auto-sync sweep (dialer/scheduler.py).

The sweep exists because the ElevenLabs post-call webhook is unreliable: every
60 seconds it finds call rows with no transcript and fetches them from EL
directly. It was also, for the same reason, the thing destroying them.

`call_ended` was inferred from `duration is not None` — but EL reports
call_duration_secs for a conversation that is still RUNNING. So 45 seconds into
a live 5-minute screening the sweep concluded "ended, no transcript", wrote
transcript=[] / status=no_answer, marked the candidate no_answer and queued a
retry call at someone who was mid-sentence. Because "no_answer" is outside the
sweep's own filter, the row was never looked at again and the screening was
gone, including calls several minutes long. Downstream, ensure_screening_outcome then told the recruiter these
booked candidates had "no screening conversation on any channel".

Pure: DB and the EL HTTP client are mocked.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/unused")
os.environ.setdefault("DB_NAME", "test_unused")

from dialer import state  # noqa: E402
from dialer.scheduler import _auto_sync_stuck_conversations  # noqa: E402


def _conv(minutes_ago=5):
    return {
        "id": "v1", "user_id": "u1", "candidate_id": "c1",
        "elevenlabs_conversation_id": "conv_x",
        "created_at": (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(),
        "call_type": "screening",
    }


class AutoSyncSweepTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = MagicMock()
        self.db.conversations.update_one = AsyncMock()
        self.db.candidates.update_one = AsyncMock()
        self.db.candidates.find_one = AsyncMock(return_value={
            "id": "c1", "user_id": "u1", "first_name": "Rowan", "appointment_at": None})
        self.db.jobs.find_one = AsyncMock(return_value={"title": "Sales Rep"})
        state._db = self.db
        self.addCleanup(setattr, state, "_db", None)
        self.retry = AsyncMock()
        patch("dialer.retry.maybe_schedule_incomplete_retry", self.retry).start()
        self.addCleanup(patch.stopall)

    def _queue(self, convs):
        self.db.conversations.find.return_value.to_list = AsyncMock(return_value=convs)

    async def _run(self, el_payload, convs=None):
        self._queue(convs if convs is not None else [_conv()])
        with patch("voice_service.fetch_elevenlabs_conversation", return_value=el_payload):
            await _auto_sync_stuck_conversations()

    def _conv_written(self):
        if not self.db.conversations.update_one.await_args_list:
            return None
        return self.db.conversations.update_one.await_args_list[0].args[1]["$set"]

    async def test_a_live_call_is_left_alone_however_long_it_has_been_running(self):
        """The incident: EL reports the duration of a call in progress. That is
        not permission to close the record and phone the person again."""
        await self._run({"status": "in-progress", "metadata": {"call_duration_secs": 45}})
        self.assertIsNone(self._conv_written())
        self.retry.assert_not_awaited()

    async def test_an_ended_call_still_being_transcribed_is_left_alone(self):
        """EL marks a conversation done before its transcript is ready. A
        five-minute call with nothing in it yet is a transcript that hasn't
        landed, not a call nobody answered."""
        await self._run({"status": "done", "metadata": {"call_duration_secs": 283,
                                                        "termination_reason": "end_call_tool"}})
        self.assertIsNone(self._conv_written())
        self.retry.assert_not_awaited()

    async def test_a_call_that_never_connected_still_closes_immediately(self):
        """The many calls that ring out must not now wait half an hour for their
        retry — they end in a couple of seconds with nothing to transcribe."""
        await self._run({"status": "done", "metadata": {"call_duration_secs": 3,
                                                        "termination_reason": "no_answer"}})
        written = self._conv_written()
        self.assertEqual(written["status"], "no_answer")
        self.assertEqual(written["transcript"], [])
        self.retry.assert_awaited_once()

    async def test_a_transcript_that_never_arrives_concludes_at_the_hard_cutoff(self):
        """Waiting is bounded: after 30 minutes the row is closed anyway, so a
        conversation EL simply lost cannot pin the candidate in limbo."""
        await self._run({"status": "done", "metadata": {"call_duration_secs": 305}},
                        convs=[_conv(minutes_ago=45)])
        self.assertEqual(self._conv_written()["status"], "no_answer")

    async def test_a_transcript_is_stored_whenever_one_exists(self):
        """The happy path has to keep working: transcript present, sweep saves
        it regardless of what the status field says."""
        turns = [{"role": "agent" if i % 2 == 0 else "user", "message": f"t{i}"} for i in range(12)]
        with patch("ai_service.summarize_call_transcript",
                   AsyncMock(return_value={"summary": "ok", "suitability_score": 70,
                                           "verdict": "good", "disqualification_reason": None})):
            await self._run({"status": "done", "transcript": turns,
                             "metadata": {"call_duration_secs": 198}})
        written = self._conv_written()
        self.assertEqual(len(written["transcript"]), 12)
        self.assertEqual(written["status"], "completed")


if __name__ == "__main__":
    unittest.main()
