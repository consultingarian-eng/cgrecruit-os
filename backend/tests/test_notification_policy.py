"""The bell rings only when a person is needed.

The recruiter's words: "I don't need notifications for the standard — when
people message, when the AI messages, confirmations, bookings. I need this
for emergencies where a human should be notified, or where the AI really
doesn't know how to handle it." A bell that announces the routine trains the
person to ignore it, and then the one abuse handoff that matters gets missed.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/unused")
os.environ.setdefault("DB_NAME", "test_unused")

from notifications_service import ACTIONABLE_KINDS  # noqa: E402


class TestBellPolicy(unittest.TestCase):
    def test_human_needed_kinds_ring(self):
        for kind in ("sms.replies_paused", "sms.unmatched", "screening.booked_unscreened",
                     "appointment.outcome_due", "call.inbound"):
            self.assertIn(kind, ACTIONABLE_KINDS, kind)

    def test_routine_flow_kinds_do_not_ring(self):
        # All of these are the system doing its job — visible on the board,
        # silent on the bell.
        for kind in ("call.completed_positive", "call.completed_negative",
                     "call.booking_failed", "call.completed_review",
                     "screening.reopened", "sms.reply",
                     "appointment.rescheduled_by_sms", "appointment.reschedule_requested",
                     "appointment.cancelled_by_candidate", "start.rescheduled"):
            self.assertNotIn(kind, ACTIONABLE_KINDS, kind)


if __name__ == "__main__":
    unittest.main()
