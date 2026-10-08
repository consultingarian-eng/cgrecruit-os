"""Tests for the abandoned-retry-chat nudge sweep (dialer/retry_chat_nudge_sweep.py).

A candidate who starts the /retry/{token} text screening but goes quiet
mid-conversation should get nudged once, 2h-3d after their last message,
and never nudged twice. `retry_chat_last_at` is cleared to None by
`routes/retry.py: retry_chat_turn` whenever the chat legitimately concludes
([END] — booked or disqualified), so a finished conversation should never
be picked up by the sweep's range query.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dialer import state  # noqa: E402
from dialer.retry_chat_nudge_sweep import retry_chat_nudge_sweep  # noqa: E402

# The package __init__ re-exports the sweep FUNCTION under the module's own
# name, so `import dialer.retry_chat_nudge_sweep as m` binds the function —
# go through sys.modules for the real module object to patch.
sweep_mod = sys.modules["dialer.retry_chat_nudge_sweep"]

# Frozen mid-afternoon "now". The sweep holds nudges during quiet hours
# (22:30–07:00 candidate-local); with a real clock these tests failed every
# evening — the sweep was right and the assertion was wrong. Time-of-day is
# nudge_schedule's own tested concern, not this file's.
_FROZEN_NOW = datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)


def _cand(id_, hours_ago, nudged=False):
    return {
        "id": id_, "user_id": "user-1", "pipeline_id": "pipe-1",
        "phone": "+15551234567", "public_token": f"tok-{id_}",
        "screening_status": "no_answer",
        "appointment_at": None,
        "retry_chat_last_at": (state.now_utc() - timedelta(hours=hours_ago)).isoformat(),
        "retry_chat_nudge_sent_at": (state.now_utc().isoformat() if nudged else None),
    }


class RetryChatNudgeSweepTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = MagicMock()
        self.db.jobs.find_one = AsyncMock(return_value=None)
        self.db.candidates.update_one = AsyncMock()
        # No inbound SMS by default — a web-chat starter, who gets the link
        # template. Individual tests override for the texted-then-stalled case.
        self.db.training_sms_messages.count_documents = AsyncMock(return_value=0)
        state._db = self.db
        self.settings = {
            "recruiter_profile": {"company_name": "Example Co"},
            "screen_call_agent": {"agent_name": "Olivia"},
            "region_language": {"timezone": "UTC"},
        }
        self.tpl = {"enabled": True, "sms_enabled": True, "sms_body": "Finish your chat: [Retry Link]"}
        self.patches = [
            patch.object(sweep_mod, "now_utc", new=lambda: _FROZEN_NOW),
            patch.object(state, "now_utc", new=lambda: _FROZEN_NOW),
            patch("dialer.retry_chat_nudge_sweep.settings_for", new=AsyncMock(return_value=self.settings)),
            patch("deps.send_stage_email", new=AsyncMock(return_value={"status": "sent"})),
            patch("sms_service.send_candidate_sms", new=AsyncMock(return_value={"status": "sent"})),
            patch("email_service.get_template_for_key", return_value=self.tpl),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        state._db = None

    def _set_candidates(self, docs):
        # Only docs matching the sweep's Mongo-style filter are "found" — the
        # sweep issues a single find(); reproduce its filtering logic here
        # so the fake driver behaves like real Mongo for these predicates.
        cutoff_lower = (state.now_utc() - timedelta(days=3)).isoformat()
        cutoff_upper = (state.now_utc() - timedelta(hours=2)).isoformat()

        def matches(d):
            last = d.get("retry_chat_last_at")
            if not last or not (cutoff_lower <= last <= cutoff_upper):
                return False
            if d.get("retry_chat_nudge_sent_at") is not None:
                return False
            if d.get("appointment_at") is not None:
                return False
            if d.get("screening_status") in ("approved", "rejected"):
                return False
            return True

        matched = [d for d in docs if matches(d)]
        cursor = MagicMock()
        cursor.to_list = AsyncMock(return_value=matched)
        self.db.candidates.find = MagicMock(return_value=cursor)

    async def test_nudges_stale_unfinished_chat(self):
        self._set_candidates([_cand("c1", hours_ago=10)])
        result = await retry_chat_nudge_sweep()
        self.assertEqual(result["notified"], 1)
        self.db.candidates.update_one.assert_awaited_once()
        args, kwargs = self.db.candidates.update_one.call_args
        self.assertEqual(args[0], {"id": "c1"})
        self.assertIsNotNone(args[1]["$set"]["retry_chat_nudge_sent_at"])

    async def test_a_texted_candidate_is_nudged_in_thread_without_a_link(self):
        """Someone screening BY TEXT gets 'just reply here' in the same thread —
        a link that bounces them to the web chat mid-conversation restarts the
        feel of the screening (and did, on the first overnight abandon). The
        link template stays for web-chat starters, where the page IS the
        thread."""
        import sms_service
        self.db.training_sms_messages.count_documents = AsyncMock(return_value=3)
        self._set_candidates([_cand("c1", hours_ago=10)])
        result = await retry_chat_nudge_sweep()
        self.assertEqual(result["notified"], 1)
        body = sms_service.send_candidate_sms.call_args.args[3]
        self.assertIn("reply here", body)
        self.assertNotIn("http", body)

    async def test_a_web_chat_candidate_still_gets_the_link(self):
        import sms_service
        self._set_candidates([_cand("c1", hours_ago=10)])
        await retry_chat_nudge_sweep()
        body = sms_service.send_candidate_sms.call_args.args[3]
        self.assertIn("tok-c1", body)

    async def test_skips_too_recent_chat(self):
        self._set_candidates([_cand("c1", hours_ago=1)])  # inside the 2h grace period
        result = await retry_chat_nudge_sweep()
        self.assertEqual(result["notified"], 0)

    async def test_skips_too_old_chat(self):
        self._set_candidates([_cand("c1", hours_ago=100)])  # past the 3-day outer bound
        result = await retry_chat_nudge_sweep()
        self.assertEqual(result["notified"], 0)

    async def test_skips_already_nudged(self):
        self._set_candidates([_cand("c1", hours_ago=10, nudged=True)])
        result = await retry_chat_nudge_sweep()
        self.assertEqual(result["notified"], 0)

    async def test_second_sweep_run_does_not_renudge(self):
        cand = _cand("c1", hours_ago=10)
        self._set_candidates([cand])
        await retry_chat_nudge_sweep()
        # Simulate the persisted guard field being set, then sweep again.
        cand["retry_chat_nudge_sent_at"] = state.now_utc().isoformat()
        self._set_candidates([cand])
        result = await retry_chat_nudge_sweep()
        self.assertEqual(result["notified"], 0)


if __name__ == "__main__":
    unittest.main()
