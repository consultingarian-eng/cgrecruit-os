"""Per-slot interviewer names.

Four different people run interviews, changing week to week — one generic
name per pipeline misnames three of them on the portal, the confirmations
and the calendar invite. The override rides the same rails as the per-slot
Zoom link: an availability rule carries the name, and every booking path
resolves it by weekday + exact HH:MM in the recruiter timezone, falling
back to pipeline.appointment_recruiter. Pure: no database, no network.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/unused")
os.environ.setdefault("DB_NAME", "test_unused")

from availability_service import resolve_slot_recruiter  # noqa: E402

# Wednesday 2026-07-29 at 14:00 ET.
WED_2PM_ET = "2026-07-29T14:00:00"
PIPELINE = {
    "appointment_recruiter": "Hiring Team",
    "availability_rules": [
        {"weekday": 2, "start": "09:15", "appointment_recruiter_override": ""},
        {"weekday": 2, "start": "14:00", "appointment_recruiter_override": "Emery K."},
        {"weekday": 5, "start": "13:00", "appointment_recruiter_override": "Kai D."},
    ],
}


class TestResolveSlotRecruiter(unittest.TestCase):
    def test_the_slot_with_a_name_returns_it(self):
        self.assertEqual(resolve_slot_recruiter(PIPELINE, WED_2PM_ET), "Emery K.")

    def test_a_slot_without_a_name_returns_none_for_the_caller_to_fall_back(self):
        self.assertIsNone(resolve_slot_recruiter(PIPELINE, "2026-07-29T09:15:00"))

    def test_a_time_matching_no_rule_returns_none(self):
        # 14:00 exists on Wednesday, not Thursday — weekday must match, not
        # just the clock time, or Thursday bookings would borrow Wednesday's
        # interviewer.
        self.assertIsNone(resolve_slot_recruiter(PIPELINE, "2026-07-30T14:00:00"))

    def test_utc_slot_isos_resolve_in_the_recruiter_timezone(self):
        # The booking paths store UTC; the rules are recruiter-local. Wednesday
        # 14:00 ET is 18:00 UTC — it must still match Emery's 14:00 rule.
        self.assertEqual(
            resolve_slot_recruiter(PIPELINE, "2026-07-29T18:00:00+00:00", "America/New_York"),
            "Emery K.",
        )

    def test_garbage_input_is_a_quiet_none(self):
        self.assertIsNone(resolve_slot_recruiter(PIPELINE, None))
        self.assertIsNone(resolve_slot_recruiter(PIPELINE, "not-a-date"))
        self.assertIsNone(resolve_slot_recruiter({}, WED_2PM_ET))


if __name__ == "__main__":
    unittest.main()
