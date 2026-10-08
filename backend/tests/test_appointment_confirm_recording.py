"""Regression tests for confirmations that were never recorded.

Several candidates texted "Y" to the appointment confirm chaser on the same
day; `appointment_sms_confirmed` was stamped on only one of them.

`_last_outbound_was_ai` decides whether a bare "yes" is confirming the existing
slot or accepting one the assistant just offered mid-conversation. It read only
`training_sms_messages`, but template sends — the confirm chaser, the 1h/10m
reminders — are written only to `communications`; `_log_message` is never called
for them. So it reported the assistant as "last to speak" however long ago it
had spoken, every "Y" was routed to the LLM as a mid-conversation reply, and the
branch that stamps the confirmation never ran.

The two logs also disagree on timezone: `training_sms_messages.timestamp` is ET,
`communications.created_at` is UTC. Comparing them as strings is wrong by the
offset, which is wider than the whole chaser-to-interview window.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import training_sms  # noqa: E402
from training_sms import _last_outbound_was_ai, _parse_ts  # noqa: E402


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, field, direction):
        self._docs = sorted(self._docs, key=lambda d: d.get(field) or "",
                            reverse=(direction < 0))
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, n):
        return self._docs[:n]


class _Coll:
    def __init__(self, docs):
        self.docs = list(docs)

    def find(self, *a, **kw):
        return _Cursor(list(self.docs))


class _DB:
    def __init__(self, sms=(), comms=()):
        self.training_sms_messages = _Coll(sms)
        self.communications = _Coll(comms)


def ai(ts):
    return {"was_llm_reply": True, "timestamp": ts, "direction": "out"}


def tpl(ts):
    return {"created_at": ts, "type": "sms", "status": "sent"}


class TestParseTs(unittest.TestCase):
    def test_handles_both_log_formats(self):
        et = _parse_ts("2026-08-03T19:24:00.000000-04:00")
        utc = _parse_ts("2026-08-03T23:00:00.000000+00:00")
        self.assertIsNotNone(et)
        self.assertIsNotNone(utc)
        # 19:24 ET is 23:24 UTC — later than 23:00 UTC despite sorting earlier
        # as a string ("2026-08-03T19" < "2026-08-03T23").
        self.assertGreater(et, utc)

    def test_naive_timestamp_is_treated_as_utc(self):
        self.assertIsNotNone(_parse_ts("2026-08-03T19:24:00"))

    def test_garbage_returns_none(self):
        self.assertIsNone(_parse_ts("not a date"))
        self.assertIsNone(_parse_ts(""))
        self.assertIsNone(_parse_ts(None))


class TestLastOutboundWasAi(unittest.IsolatedAsyncioTestCase):
    async def test_chaser_after_the_ai_is_not_mid_conversation(self):
        """The exact 2026-08-03 shape: assistant screened them in the morning,
        the chaser went out at 19:00, then they replied 'Y'."""
        db = _DB(sms=[ai("2026-08-03T09:19:37.000000-04:00")],
                 comms=[tpl("2026-08-03T23:00:03.000000+00:00")])  # 19:00 ET
        self.assertFalse(await _last_outbound_was_ai(db, "c1"),
                         "a 'Y' after the chaser must be recorded as a confirmation")

    async def test_ai_speaking_last_is_still_mid_conversation(self):
        db = _DB(sms=[ai("2026-08-03T19:30:00.000000-04:00")],       # 23:30 UTC
                 comms=[tpl("2026-08-03T23:00:03.000000+00:00")])    # 23:00 UTC
        self.assertTrue(await _last_outbound_was_ai(db, "c1"),
                        "the assistant offered a slot most recently — 'yes' means that slot")

    async def test_timezone_offset_is_not_string_compared(self):
        """The trap. AI at 19:24 ET (=23:24 UTC) is LATER than a template at
        23:00 UTC, but sorts EARLIER as a string."""
        db = _DB(sms=[ai("2026-08-03T19:24:00.000000-04:00")],
                 comms=[tpl("2026-08-03T23:00:00.000000+00:00")])
        self.assertTrue(await _last_outbound_was_ai(db, "c1"))

    async def test_no_ai_message_at_all(self):
        db = _DB(sms=[{"was_llm_reply": False, "timestamp": "2026-08-03T19:00:00-04:00"}],
                 comms=[tpl("2026-08-03T23:00:00+00:00")])
        self.assertFalse(await _last_outbound_was_ai(db, "c1"))

    async def test_no_templates_ever_sent_keeps_the_ai_answer(self):
        db = _DB(sms=[ai("2026-08-03T19:00:00.000000-04:00")], comms=[])
        self.assertTrue(await _last_outbound_was_ai(db, "c1"))

    async def test_undateable_ai_row_stays_conservative(self):
        # Can't order it — keep the old behaviour rather than risk stamping a
        # confirmation onto a slot the candidate was actually declining.
        db = _DB(sms=[ai("")], comms=[tpl("2026-08-03T23:00:00+00:00")])
        self.assertTrue(await _last_outbound_was_ai(db, "c1"))

    async def test_db_failure_does_not_raise(self):
        class _Broken:
            def find(self, *a, **kw):
                raise RuntimeError("mongo down")
        db = _DB()
        db.training_sms_messages = _Broken()
        self.assertFalse(await _last_outbound_was_ai(db, "c1"))


class TestChaserIsStillNotDoubleSent(unittest.TestCase):
    """The chaser is skipped once `appointment_sms_confirmed` is set. Now that
    the flag actually gets set, that guard starts doing its job — confirm it
    reads the flag and nothing else."""

    def test_guard_reads_the_flag(self):
        src = Path(__file__).resolve().parents[1] / "dialer" / "reminders.py"
        text = src.read_text()
        self.assertIn('template_key == "appointment_confirm_chaser" and cand.get("appointment_sms_confirmed")',
                      text)


if __name__ == "__main__":
    unittest.main()
