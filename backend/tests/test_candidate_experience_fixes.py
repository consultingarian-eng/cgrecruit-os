"""Regression tests for the 2026-08-08 candidate-experience fixes.

Pure-function tests (no HTTP server, no live Mongo writes) in the mock-driver
style — runnable with:
    DB_NAME=cgrecruit_local_test backend/.venv/bin/python -m pytest backend/tests/test_candidate_experience_fixes.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class ClassifierTests(unittest.TestCase):
    """'Can't wait!' from a booked candidate must never read as a decline."""

    def _classify(self, body):
        from training_sms import _classify
        return _classify(body)

    def test_cant_wait_is_yes(self):
        self.assertEqual(self._classify("Can't wait!"), "yes")

    def test_cant_wait_to_see_you_is_yes(self):
        self.assertEqual(self._classify("Can't wait to see you!"), "yes")

    def test_no_worries_see_you_then_is_yes(self):
        self.assertEqual(self._classify("No worries, see you then!"), "yes")

    def test_nope_no_questions_see_you_is_yes(self):
        self.assertEqual(self._classify("Nope, no questions - see you at 2"), "yes")

    def test_bare_no_is_still_no(self):
        self.assertEqual(self._classify("No"), "no")
        self.assertEqual(self._classify("Nope"), "no")

    def test_explicit_declines_still_no(self):
        # NO_PHRASES outrank everything, including positive phrases later in
        # the message.
        self.assertEqual(self._classify("I can't make it"), "no")
        self.assertEqual(self._classify("Sorry, have to cancel — maybe see you another time"), "no")
        self.assertEqual(self._classify("won't be able to attend"), "no")

    def test_bare_cant_is_unknown_not_no(self):
        # A lone "can't" carries no attendance meaning — the model should ask.
        self.assertEqual(self._classify("can't"), "unknown")

    def test_plain_yes_still_yes(self):
        self.assertEqual(self._classify("Yes"), "yes")
        self.assertEqual(self._classify("ok"), "yes")

    def test_matches_no_phrase(self):
        from training_sms import _matches_no_phrase
        self.assertTrue(_matches_no_phrase("I can't make it Thursday"))
        self.assertTrue(_matches_no_phrase("need to cancel"))
        self.assertFalse(_matches_no_phrase("No"))
        self.assertFalse(_matches_no_phrase("Can't wait!"))


class PhoneNormalizationTests(unittest.TestCase):
    """Naturally-typed US numbers must reach the dial APIs as E.164."""

    def _n(self, phone):
        from deps import normalize_phone_e164
        return normalize_phone_e164(phone)

    def test_parenthesized_us_number(self):
        self.assertEqual(self._n("(617) 555-0134"), "+16175550134")

    def test_dashed_us_number(self):
        self.assertEqual(self._n("617-555-0134"), "+16175550134")

    def test_eleven_digit_with_leading_one(self):
        self.assertEqual(self._n("1 617 555 0134"), "+16175550134")

    def test_already_e164_unchanged(self):
        self.assertEqual(self._n("+16175550134"), "+16175550134")

    def test_international_kept(self):
        self.assertEqual(self._n("+44 20 7946 0958"), "+442079460958")

    def test_unparseable_returned_verbatim(self):
        # Intake tolerance: never destroy what the candidate typed.
        self.assertEqual(self._n("12345"), "12345")
        self.assertEqual(self._n(""), "")


class CandidateWindowDaysTests(unittest.TestCase):
    """Every candidate door shares one booking-window computation."""

    PIPELINE = {"availability_rules": [
        {"weekday": d, "start": "09:00", "capacity": 10} for d in range(0, 5)
    ]}

    def test_honours_booking_days_offered(self):
        from availability_service import candidate_window_days
        days = candidate_window_days(self.PIPELINE, {"booking_days_offered": 4}, "America/New_York")
        # effective_window_days may widen for dead days, but never below base
        # and never past base + MAX_WINDOW_EXTENSION_DAYS.
        self.assertGreaterEqual(days, 4)
        self.assertLessEqual(days, 11)

    def test_fallback_when_unconfigured(self):
        from availability_service import candidate_window_days
        self.assertEqual(candidate_window_days(self.PIPELINE, {}, "America/New_York", fallback_days=14), 14)

    def test_no_sixty_day_horizon(self):
        from availability_service import candidate_window_days
        days = candidate_window_days(self.PIPELINE, {"booking_days_offered": 4}, "America/New_York")
        self.assertLess(days, 60)


class SlotOrderingTests(unittest.TestCase):
    """Within a day, the soonest slot must be offered first regardless of the
    order the recruiter added the rules."""

    def test_slots_sorted_chronologically(self):
        from datetime import datetime, timezone
        from availability_service import compute_available_slots
        pipeline = {
            "appointment_duration_minutes": 30,
            # 1:00 PM rule added BEFORE the 9:15 AM one — insertion order used
            # to leak straight into the offer order.
            "availability_rules": [
                {"weekday": d, "start": "13:00", "capacity": 10} for d in range(0, 7)
            ] + [
                {"weekday": d, "start": "09:15", "capacity": 10} for d in range(0, 7)
            ],
        }
        slots = compute_available_slots(
            pipeline, [], days_ahead=3, tz_name="America/New_York",
            now=datetime(2026, 8, 3, 5, 0, tzinfo=timezone.utc),  # a Monday, pre-window
        )
        self.assertGreaterEqual(len(slots), 4)
        isos = [s["datetime"] for s in slots]
        self.assertEqual(isos, sorted(isos))


if __name__ == "__main__":
    unittest.main()
