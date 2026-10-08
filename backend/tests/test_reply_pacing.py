"""Reply pacing: the delay must scale with length, and never stall the thread.

The point of the delay is believability — a 400-character screening recap that
arrives in under a second reads as "nobody considered this". But the failure
mode on the other side is worse: a candidate mid-screening left waiting half a
minute per question drops out. So these tests pin both edges — short stays
snappy, long stays bounded — and that already-spent LLM time is credited
rather than stacked. Pure: no database, no network, no sleeping.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/unused")
os.environ.setdefault("DB_NAME", "test_unused")

from reply_pacing import considered_delay_seconds, remaining_delay_seconds  # noqa: E402

SHORT = "Perfect! And do you have full working rights in the US?"
LONG = (
    "Amazing — that competitive edge is exactly what does well in this role! "
    "Alright, let's get you booked in. I have Tuesday at 5:30 PM or Wednesday "
    "at 9:00 AM — which works best for you? Both sessions run on Zoom with our "
    "hiring manager and take about thirty minutes, and you'll get the link, a "
    "calendar invite and a reminder beforehand so you have everything you need."
)


class TestConsideredDelay(unittest.TestCase):
    def test_short_replies_stay_quick(self):
        # A one-line acknowledgement should land in a few seconds — pacing is
        # about believability, not making six questions take ten minutes.
        self.assertLess(considered_delay_seconds(SHORT), 6.0)

    def test_long_replies_take_visibly_longer(self):
        # The user-stated target: roughly 10-15s for the big paragraphs.
        d = considered_delay_seconds(LONG)
        self.assertGreaterEqual(d, 10.0)
        self.assertLessEqual(d, 15.0)

    def test_longer_reply_never_arrives_faster_than_a_shorter_one(self):
        self.assertGreater(considered_delay_seconds(LONG), considered_delay_seconds(SHORT))

    def test_the_cap_holds_for_absurd_lengths(self):
        # A runaway model reply must not park the conversation for a minute.
        self.assertLessEqual(considered_delay_seconds("x" * 5000, "y" * 5000), 15.0)

    def test_even_an_empty_reply_is_not_instant(self):
        self.assertGreaterEqual(considered_delay_seconds(""), 2.0)

    def test_what_the_candidate_wrote_buys_reading_time(self):
        # Same reply, longer inbound → slightly longer pause. This is the
        # "it looked like my answer wasn't even read" complaint, directly.
        self.assertGreater(
            considered_delay_seconds(SHORT, "I live in Somerville so the commute is easy " * 3),
            considered_delay_seconds(SHORT, "Yes"),
        )


class TestPacedSendIsNotGarbageCollected(unittest.TestCase):
    """asyncio holds scheduled tasks only weakly: a bare create_task with no
    saved reference can be destroyed mid-sleep, and here that is a screening
    reply that silently never sends — in the channel with no other copy of
    the message. The handler must keep a strong reference until done."""

    def test_the_task_reference_is_kept_until_it_finishes(self):
        path = os.path.join(os.path.dirname(__file__), "..", "training_sms.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("_PACED_REPLY_TASKS.add(task)", src)
        self.assertIn("task.add_done_callback(_PACED_REPLY_TASKS.discard)", src)


class TestRemainingDelay(unittest.TestCase):
    def test_llm_time_counts_as_thinking_time(self):
        target = considered_delay_seconds(LONG)
        self.assertAlmostEqual(remaining_delay_seconds(LONG, "", 4.0), target - 4.0, places=6)

    def test_a_slow_llm_call_never_produces_a_negative_sleep(self):
        self.assertEqual(remaining_delay_seconds(SHORT, "", 60.0), 0.0)

    def test_negative_elapsed_is_treated_as_zero(self):
        # Clock weirdness (NTP step, monotonic misuse) must not extend the wait.
        self.assertAlmostEqual(
            remaining_delay_seconds(SHORT, "", -5.0),
            considered_delay_seconds(SHORT), places=6,
        )


if __name__ == "__main__":
    unittest.main()
