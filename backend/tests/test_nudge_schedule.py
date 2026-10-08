"""Chasing a silent candidate, and measuring how fast anyone replies.

The nudge fired once, ever, per candidate and was never re-armed. That was
survivable while the AI phone call was the real screening. With the call reduced
to a ping, this sequence *is* the follow-up — someone who answered two questions
and put their phone down had nothing else coming.

Three attempts at 2h, 6h and 24h. Two properties matter more than the exact
numbers: it must stop (a fourth text to someone who ignored three is why people
report a number as spam), and it must never arrive in the middle of the night.
Unlike a call, a text physically can be sent at 3am, so nothing but this code
stops it.

Pure: no database, no clock of its own — `now` is always passed in.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

import pytz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/unused")
os.environ.setdefault("DB_NAME", "test_unused")

from nudge_schedule import (  # noqa: E402
    GIVE_UP_AFTER_DAYS,
    MAX_NUDGES,
    NUDGE_OFFSETS_HOURS,
    due_at,
    in_quiet_hours,
    next_sendable_time,
    response_delay_seconds,
    should_nudge_now,
    summarise_delays,
)

TZ = "America/New_York"
_et = pytz.timezone(TZ)


def at(hour, minute=0, day=4):
    """A wall-clock time in the recruiter's timezone, as UTC."""
    return _et.localize(datetime(2026, 8, day, hour, minute)).astimezone(timezone.utc)


class TestTheSequence(unittest.TestCase):
    def test_it_is_three_attempts_getting_further_apart(self):
        self.assertEqual(NUDGE_OFFSETS_HOURS, [2, 6, 24])
        self.assertEqual(MAX_NUDGES, 3)
        self.assertEqual(sorted(NUDGE_OFFSETS_HOURS), NUDGE_OFFSETS_HOURS)

    def test_each_one_is_due_at_the_right_distance_from_their_last_message(self):
        last = at(9)
        for i, hours in enumerate(NUDGE_OFFSETS_HOURS):
            self.assertEqual(due_at(last, i, TZ), last + timedelta(hours=hours), i)

    def test_it_stops(self):
        """A fourth text to someone who has ignored three is not persistence."""
        self.assertIsNone(due_at(at(9), MAX_NUDGES, TZ))
        self.assertFalse(should_nudge_now(at(9), MAX_NUDGES, at(9) + timedelta(days=1), TZ))

    def test_nothing_fires_before_it_is_due(self):
        last = at(9)
        self.assertFalse(should_nudge_now(last, 0, last + timedelta(hours=1), TZ))
        self.assertTrue(should_nudge_now(last, 0, last + timedelta(hours=2), TZ))

    def test_a_candidate_who_went_quiet_days_ago_is_left_alone(self):
        last = at(9)
        stale = last + timedelta(days=GIVE_UP_AFTER_DAYS + 1)
        self.assertFalse(should_nudge_now(last, 0, stale, TZ))

    def test_the_give_up_rule_is_not_swallowing_everything(self):
        # Guards against GIVE_UP_AFTER_DAYS being 0, which would make the test
        # above pass while nudging nobody at all.
        last = at(9)
        self.assertTrue(should_nudge_now(last, 0, last + timedelta(hours=3), TZ))


class TestNobodyIsTextedAtThreeInTheMorning(unittest.TestCase):
    def test_the_night_is_quiet(self):
        for hour in (23, 0, 2, 4, 6):
            self.assertTrue(in_quiet_hours(at(hour), TZ), hour)

    def test_the_day_is_not(self):
        for hour in (7, 9, 12, 17, 20, 22):
            self.assertFalse(in_quiet_hours(at(hour), TZ), hour)

    def test_texting_runs_later_than_calling_does(self):
        """The point of the wider window: a text at 9pm is fine, a cold call
        isn't. The calling window closes at 19:00."""
        self.assertFalse(in_quiet_hours(at(21), TZ))

    def test_a_nudge_due_overnight_is_held_not_dropped(self):
        """Dropping it would silently shorten the sequence — the candidate would
        get two chases instead of three and nobody would know."""
        due_in_the_night = at(2)
        held = next_sendable_time(due_in_the_night, TZ)
        self.assertFalse(in_quiet_hours(held, TZ))
        self.assertGreater(held, due_in_the_night)
        self.assertEqual(held.astimezone(_et).hour, 7)

    def test_a_late_evening_message_pushes_to_the_next_morning(self):
        # Last message 21:00, +2h lands at 23:00 → next morning.
        held = due_at(at(21), 0, TZ)
        local = held.astimezone(_et)
        self.assertEqual((local.hour, local.day), (7, 5))

    def test_a_daytime_nudge_is_not_delayed_at_all(self):
        when = at(14)
        self.assertEqual(next_sendable_time(when, TZ), when)

    def test_nothing_is_sent_during_quiet_hours_even_if_overdue(self):
        last = at(18)          # +2h = 20:00, due and sendable
        middle_of_night = at(3, day=5)
        self.assertFalse(should_nudge_now(last, 0, middle_of_night, TZ))
        self.assertTrue(should_nudge_now(last, 0, at(8, day=5), TZ))

    def test_an_unknown_timezone_does_not_crash_or_go_silent(self):
        self.assertIsInstance(in_quiet_hours(at(14), "Not/AZone"), bool)
        self.assertIsInstance(in_quiet_hours(at(14), None), bool)


class TestAnswerIsNotTheSameAsInitiate(unittest.TestCase):
    """Quiet hours must govern us STARTING a conversation, never continuing one.

    A candidate who texts at 10:38pm is the best moment this funnel ever gets.
    The assistant has to answer, finish the screening, book the slot and send the
    confirmation — then, at 10:38pm. Going quiet on someone who is awake and
    engaging, to observe our own politeness rule, would be self-defeating.

    These read the source because the property is an absence: the guarantee is
    that no reply path consults this module, and an absence is exactly what
    quietly reappears when somebody adds a well-meaning global check later.
    """

    BACKEND = os.path.join(os.path.dirname(__file__), "..")

    def _source(self, *parts):
        with open(os.path.join(self.BACKEND, *parts), encoding="utf-8") as fh:
            return fh.read()

    def test_the_inbound_reply_path_has_no_quiet_hours_check(self):
        src = self._source("training_sms.py")
        handler = src[src.index("async def handle_inbound"):]
        for forbidden in ("in_quiet_hours", "next_sendable_time", "QUIET_START", "QUIET_END"):
            self.assertNotIn(forbidden, handler, forbidden)

    # The symbols that would actually gate a send. Matching the bare word
    # "quiet" instead catches ordinary prose — screening_start's docstring says
    # a caller may have "their own reason to stay quiet" — and a test that fails
    # on a comment teaches people to weaken it.
    GATE_SYMBOLS = ("in_quiet_hours", "next_sendable_time", "QUIET_START", "QUIET_END",
                    "should_nudge_now", "quiet_hours")

    def assert_ungated(self, source, where):
        for symbol in self.GATE_SYMBOLS:
            self.assertNotIn(symbol, source, f"{symbol} in {where}")

    def test_the_web_chat_reply_path_has_no_quiet_hours_check(self):
        self.assert_ungated(self._source("routes", "retry.py"), "routes/retry.py")

    def test_sending_a_message_to_a_candidate_is_never_time_gated(self):
        """send_candidate_sms carries the booking confirmation, the approval and
        every reply. A window here would silently swallow a 10:38pm booking."""
        self.assert_ungated(self._source("sms_service.py"), "sms_service.py")

    def test_booking_and_confirming_do_not_consult_the_clock(self):
        for module in ("screening_outcome.py", "screening_start.py"):
            self.assert_ungated(self._source(module), module)

    def test_the_gate_symbols_are_real(self):
        """Guards the four tests above against being vacuous: if these names were
        renamed, every assert_ungated would pass by matching nothing."""
        import nudge_schedule

        for symbol in ("in_quiet_hours", "next_sendable_time", "QUIET_START",
                       "QUIET_END", "should_nudge_now"):
            self.assertTrue(hasattr(nudge_schedule, symbol), symbol)

    def test_only_the_nudge_sweep_uses_this_module(self):
        """The architectural guarantee, stated as a test: exactly one caller
        decides when NOT to message someone, and it is the one that starts
        conversations rather than continues them."""
        import subprocess

        out = subprocess.run(
            ["grep", "-rln", "should_nudge_now\\|in_quiet_hours\\|next_sendable_time",
             "--include=*.py", "."],
            cwd=self.BACKEND, capture_output=True, text=True,
        ).stdout.split()
        callers = sorted(f for f in out
                         if "test" not in f and "nudge_schedule.py" not in f)
        self.assertEqual(callers, ["./dialer/retry_chat_nudge_sweep.py"], callers)


class TestSpeedToContact(unittest.TestCase):
    def test_it_measures_the_gap_between_reaching_out_and_hearing_back(self):
        out = "2026-08-04T13:00:00+00:00"
        back = "2026-08-04T13:04:30+00:00"
        self.assertEqual(response_delay_seconds(out, back), 270)

    def test_both_stored_timestamp_shapes_work(self):
        out = datetime(2026, 8, 4, 13, 0, tzinfo=timezone.utc)
        self.assertEqual(response_delay_seconds(out, "2026-08-04T13:01:00+00:00"), 60)

    def test_a_missing_end_is_no_measurement_rather_than_zero(self):
        """Someone who never replied must not be counted as replying instantly —
        that would make the headline number better the worse things got."""
        self.assertIsNone(response_delay_seconds("2026-08-04T13:00:00+00:00", None))
        self.assertIsNone(response_delay_seconds(None, "2026-08-04T13:00:00+00:00"))

    def test_a_reply_stamped_before_the_outreach_is_discarded(self):
        # Clock skew, not a prescient candidate. Better no number than a negative.
        self.assertIsNone(response_delay_seconds("2026-08-04T13:00:00+00:00",
                                                 "2026-08-04T12:00:00+00:00"))

    def test_the_median_leads_because_the_tail_is_long(self):
        """Four people: three inside ten minutes, one the next day. The mean says
        six hours, which describes nobody."""
        s = summarise_delays([30, 120, 600, 86400])
        self.assertEqual(s["median_seconds"], 360)
        self.assertGreater(s["mean_seconds"], 20000)

    def test_it_counts_the_fast_replies_separately(self):
        s = summarise_delays([30, 120, 600, 86400])
        self.assertEqual(s["under_5_min"], 2)
        self.assertEqual(s["under_1_hour"], 3)

    def test_no_replies_yet_reports_nothing_rather_than_zero(self):
        s = summarise_delays([])
        self.assertEqual(s["replies"], 0)
        self.assertIsNone(s["median_seconds"])


if __name__ == "__main__":
    unittest.main()
