"""Where the text-chase clear belongs: on a CONNECTED call, never at dial time.

History, because this policy has now flipped once in each direction:

  * v1 — retry_chat_last_at was only cleared by the chat finishing ([END]), so
    a candidate genuinely reached by phone kept getting "you never finished
    your chat" nudges for days after real contact. Fix: clear it in
    place_call_now, before dialing.
  * v2 (this test) — that fix cleared at DIAL time, before anyone answered.
    Under chat-first the 120-minute fallback call rings out unanswered for
    most people, and the clear silently killed the 2h/6h/24h text chase for
    candidates who were mid-text-screening an hour earlier (a candidate answered two
    gates, the fallback rang out, and no nudge ever came).

So: place_call_now must NOT touch retry_chat_last_at, and follow_up's
_build_candidate_update clears it only when the call actually connected
(is_complete) — an unanswered ring is not contact.

Run directly (never via pytest collection):
    MONGO_URL=mongodb://127.0.0.1:1/x DB_NAME=x .venv/bin/python tests/test_place_call_clears_retry_chat.py
"""
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dialer import state  # noqa: E402
import dialer.place_call as place_call  # noqa: E402
from dialer.follow_up import _build_candidate_update  # noqa: E402


class PlaceCallLeavesChaseAloneTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = MagicMock()
        self.cand = {
            "id": "cand-1", "user_id": "user-1", "pipeline_id": "pipe-1",
            "phone": "+15551234567", "retry_chat_last_at": "2026-07-27T16:44:04+00:00",
        }
        self.db.candidates.find_one = AsyncMock(return_value=dict(self.cand))
        self.db.candidates.update_one = AsyncMock()
        self.db.pipelines.find_one = AsyncMock(return_value={"id": "pipe-1", "calling_mode": "managed"})
        state._db = self.db

        self.patches = [
            patch.object(place_call, "should_skip_call", return_value=(False, None)),
            patch.object(place_call, "settings_for", new=AsyncMock(return_value={})),
            patch.object(place_call, "_enforce_concurrency_cap", new=AsyncMock(return_value=None)),
            patch.object(place_call, "_enforce_slot_availability", new=AsyncMock(return_value=None)),
            patch.object(place_call, "resolve_agent_config", return_value={"agent_name": "Olivia", "agent_id": "a1"}),
            patch.object(place_call, "build_dynamic_variables", return_value={}),
            patch.object(place_call, "_dial_via_managed", new=AsyncMock(return_value={"status": "initiated"})),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        state._db = None

    async def test_dialing_does_not_clear_retry_chat_last_at(self):
        """An attempt is not contact — the chase must survive an unanswered ring."""
        await place_call.place_call_now("user-1", "cand-1")
        clear_calls = [
            c for c in self.db.candidates.update_one.call_args_list
            if c.args[1].get("$set", {}).get("retry_chat_last_at", "unset") is None
        ]
        self.assertFalse(clear_calls, "place_call_now must not clear retry_chat_last_at at dial time")


class FollowUpClearsChaseOnRealContactTests(unittest.TestCase):
    def _cand(self):
        return {"id": "cand-1", "verdict": None, "call_summary": None,
                "smart_score": None, "retry_chat_last_at": "2026-07-27T16:44:04+00:00"}

    def test_connected_call_clears_the_chase(self):
        update = _build_candidate_update(self._cand(), "completed", "completed",
                                         {"verdict": "strong"}, is_complete=True)
        self.assertIn("retry_chat_last_at", update)
        self.assertIsNone(update["retry_chat_last_at"])

    def test_unanswered_call_leaves_the_chase_armed(self):
        update = _build_candidate_update(self._cand(), "no_answer", "no_answer",
                                         {}, is_complete=False)
        self.assertNotIn("retry_chat_last_at", update)


if __name__ == "__main__":
    unittest.main()
