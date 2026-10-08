"""Tests for the last net under the ElevenLabs sync.

Three things copy a screening out of ElevenLabs: the post-call webhook, the
auto-sync sweep, and — when neither managed it — this recovery pass. The first
two both used to conclude "no transcript" the instant EL hadn't produced one
yet, and the auto-sync sweep finds work by STATUS, so anything they concluded
early dropped out of its view for good. 102 real screenings went that way,
several of them five minutes long.

The recovery sweep is therefore blind to status, and writes to the conversation
ONLY: re-running screening outcomes on a day-old call would fire rejection
emails, slot-picker links and retry dials at people whose cases have moved on.
"""
import os
import sys
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/unused")
os.environ.setdefault("DB_NAME", "test_unused")

from dialer import state  # noqa: E402
from dialer.transcript_recovery import (  # noqa: E402
    LOOKBACK_HOURS,
    MAX_ATTEMPTS,
    SETTLE_MINUTES,
    late_transcript_recovery_sweep,
)


def _turns(n):
    return [{"role": "agent" if i % 2 == 0 else "user", "message": f"t{i}"} for i in range(n)]


class LateTranscriptRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = MagicMock()
        self.db.conversations.update_one = AsyncMock()
        self.db.candidates.update_one = AsyncMock()
        self.db.candidates.find_one = AsyncMock(return_value={"first_name": "Galen", "job_id": "j1"})
        self.db.jobs.find_one = AsyncMock(return_value={"title": "Sales Rep"})
        state._db = self.db
        self.addCleanup(setattr, state, "_db", None)
        self.score = AsyncMock(return_value={"summary": "Manager at Verizon, meets all gates.",
                                             "suitability_score": 78, "verdict": "good"})
        patch("ai_service.summarize_call_transcript", self.score).start()
        self.addCleanup(patch.stopall)

    def _queue(self, rows):
        self.db.conversations.find.return_value.to_list = AsyncMock(return_value=rows)
        return self.db.conversations.find

    async def _run(self, payload, row=None):
        row = row or {"id": "v1", "elevenlabs_conversation_id": "conv_x",
                      "duration_seconds": 0, "status": "no_answer", "candidate_id": "c1"}
        self._queue([row])
        with patch("voice_service.fetch_elevenlabs_conversation", return_value=payload):
            return await late_transcript_recovery_sweep()

    def _written(self):
        """The $set from the recovery write (the $inc attempt counter is separate)."""
        for call in self.db.conversations.update_one.await_args_list:
            if "$set" in call.args[1]:
                return call.args[1]["$set"]
        return None

    async def test_a_screening_filed_as_an_empty_no_answer_is_restored(self):
        """Esme's shape exactly: 0 seconds and nothing stored locally, a
        19-turn conversation waiting at ElevenLabs."""
        res = await self._run({"transcript": _turns(19), "metadata": {"call_duration_secs": 212}})
        self.assertEqual(res["recovered"], 1)
        w = self._written()
        self.assertEqual(len(w["transcript"]), 19)
        self.assertEqual(w["duration_seconds"], 212)
        self.assertEqual(w["status"], "completed")

    async def test_the_call_is_rescored_so_the_card_does_not_contradict_itself(self):
        """Galen's card: 30 turns and a correct candidate score of 78, under
        a conversation header still reading "Suitability 0/100" — the zero the
        premature conclusion wrote over an empty transcript."""
        await self._run({"transcript": _turns(30), "metadata": {"call_duration_secs": 195}})
        w = self._written()
        self.assertEqual(w["suitability_score"], 78)
        self.assertTrue(w["summary"])

    async def test_a_failed_rescore_never_costs_us_the_transcript(self):
        """A missing score is cosmetic; a missing transcript is not."""
        self.score.side_effect = RuntimeError("LLM had a bad night")
        res = await self._run({"transcript": _turns(19), "metadata": {"call_duration_secs": 212}})
        self.assertEqual(res["recovered"], 1)
        self.assertEqual(len(self._written()["transcript"]), 19)

    async def test_it_never_touches_the_candidate(self):
        """The reason this is transcript-only: re-running outcomes on an old
        call would email and re-dial people whose cases have moved on."""
        await self._run({"transcript": _turns(19), "metadata": {"call_duration_secs": 212}})
        self.db.candidates.update_one.assert_not_awaited()

    async def test_a_call_that_genuinely_rang_out_is_asked_about_and_dropped(self):
        """Most dials never connect. Each is polled a bounded number of
        times and then left alone forever."""
        res = await self._run({"transcript": []})
        self.assertEqual(res["recovered"], 0)
        self.assertEqual(self.db.conversations.update_one.await_args.args[1],
                         {"$inc": {"transcript_recovery_attempts": 1}})

    async def test_it_looks_past_status_which_is_what_hid_these_calls(self):
        """The auto-sync sweep's filter is a status list; this query must not
        have one, or terminal-status rows stay invisible exactly as before."""
        self._queue([])
        with patch("voice_service.fetch_elevenlabs_conversation"):
            await late_transcript_recovery_sweep()
        q = self.db.conversations.find.call_args.args[0]
        self.assertNotIn("status", q)
        self.assertLess(q["transcript_recovery_attempts"]["$lt"], MAX_ATTEMPTS + 1)
        # Bounded window: settled enough that the auto-sync sweep has had its
        # go, recent enough that we aren't re-polling the whole account.
        self.assertIn("$gte", q["created_at"])
        self.assertIn("$lt", q["created_at"])

    async def test_the_window_matches_the_sweep_it_backs_up(self):
        self.assertGreaterEqual(SETTLE_MINUTES, 20)   # auto-sync concludes at 30 min
        self.assertGreaterEqual(LOOKBACK_HOURS, 24)


class WebhookDefersInsteadOfConcludingTests(unittest.IsolatedAsyncioTestCase):
    """The post-call webhook carried the same defect as the sweep: an empty
    transcript on a call with real talk-time was written as a no_answer, which
    put the row outside the auto-sync sweep's filter — Cosmo's five-minute
    screening went invisible for days that way. It now writes nothing
    and lets the sweep keep polling."""

    def test_the_webhook_defers_a_transcript_that_is_not_ready(self):
        path = os.path.join(os.path.dirname(__file__), "..", "routes", "webhooks.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        start = src.index("async def _process_screening_post_call")
        block = src[start:start + 6000]
        self.assertIn('if not transcript and (duration or 0) >= 20 and not voicemail_by_platform:', block)
        self.assertIn('"deferred": "transcript_not_ready"', block)
        # The deferral must come BEFORE anything that writes or sends.
        self.assertLess(block.index("transcript_not_ready"), block.index("db.conversations.update_one"))


if __name__ == "__main__":
    unittest.main()
