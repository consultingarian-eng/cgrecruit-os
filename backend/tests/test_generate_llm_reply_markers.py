"""Regression test for a real production bug: `_generate_llm_reply` had its
own private unpack of `_extract_markers()`'s return tuple (line ~916) that
the earlier SCREENING self-serve work missed when the tuple grew from 4
values to 6 (added callback_requested, schedule_call_iso). It crashed every
inbound SMS that reached the free-text LLM path with "too many values to
unpack (expected 4)" — caught internally, so requests still returned 200,
but candidates silently got the generic fallback reply instead of an AI one.

This test drives the real function (mocking only the Anthropic client) so a
future tuple-shape change gets caught here instead of in production logs.
"""
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import training_sms  # noqa: E402


def _fake_anthropic_client(reply_text: str):
    content_block = MagicMock()
    content_block.type = "text"
    content_block.text = reply_text
    response = MagicMock()
    response.content = [content_block]
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=response)
    # The code calls client.with_options(timeout=...).messages.create(...).
    client.with_options.return_value = client
    return client


class GenerateLlmReplyMarkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cand = {"id": "c1", "user_id": "u1", "pipeline_id": "p1", "stage": "SCREENING"}
        self.pipeline = {"name": "Downtown"}
        patcher = patch.dict(os.environ, {"EMERGENT_LLM_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)
        fetch_patcher = patch("training_sms._fetch_llm_context", new=AsyncMock(return_value={}))
        fetch_patcher.start()
        self.addCleanup(fetch_patcher.stop)

    async def test_plain_reply_no_markers(self):
        client = _fake_anthropic_client("Sure thing, happy to help!")
        with patch("anthropic.AsyncAnthropic", return_value=client):
            result = await training_sms._generate_llm_reply(
                None, self.cand, self.pipeline, [], "hi", "unknown"
            )
        self.assertEqual(result, "Sure thing, happy to help!")

    async def test_all_marker_types_survive_the_round_trip(self):
        raw = (
            "On it! [REQUEST_CALLBACK] [SCHEDULE_CALL: 2026-03-04T13:00:00+00:00] "
            "[BOOK: 2026-03-05T14:00:00+00:00] [CANCEL] [PROPOSED_DATE: 2026-03-09]"
        )
        client = _fake_anthropic_client(raw)
        with patch("anthropic.AsyncAnthropic", return_value=client):
            result = await training_sms._generate_llm_reply(
                None, self.cand, self.pipeline, [], "call me now", "unknown"
            )
        self.assertIsNotNone(result)
        self.assertIn("[REQUEST_CALLBACK]", result)
        self.assertIn("[SCHEDULE_CALL: 2026-03-04T13:00:00+00:00]", result)
        self.assertIn("[BOOK: 2026-03-05T14:00:00+00:00]", result)
        self.assertIn("[CANCEL]", result)
        self.assertIn("[PROPOSED_DATE: 2026-03-09]", result)

    async def test_long_reply_truncates_prose_but_keeps_markers(self):
        long_prose = "x" * 400
        raw = f"{long_prose} [REQUEST_CALLBACK]"
        client = _fake_anthropic_client(raw)
        with patch("anthropic.AsyncAnthropic", return_value=client):
            result = await training_sms._generate_llm_reply(
                None, self.cand, self.pipeline, [], "call me now", "unknown"
            )
        self.assertIn("[REQUEST_CALLBACK]", result)
        self.assertLess(len(result), len(raw))


if __name__ == "__main__":
    unittest.main()
