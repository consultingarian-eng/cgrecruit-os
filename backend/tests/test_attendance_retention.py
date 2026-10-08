"""Not losing an interview outcome once somebody has given one.

The team marks every candidate after every session — through CGRecruit or
through CG1's Booked Interviews screen. Both write correctly. Yet 245 past
interviews had no outcome, which looked like a process problem and wasn't. Only
13 were sitting unanswered and visible. The rest had been recorded, or could
have been, and the system lost them three ways:

  * 107 were advanced past the interview stage, which wrote no outcome and
    removed the only control that could — fixed by the /move gate.
  * 57 were marked a no-show and then rescheduled, and the reschedule cleared
    the field. Every single unanswered record carrying a `no_show_at` stamp also
    carries `previous_appointment_at`, so the reschedule explains all of them.
  * 125 were archived 48 hours after the slot. Both the board and CG1's list
    hide archived candidates, so a Friday session was invisible by Monday.

This file covers the second and third. Pure: no database, no scheduler.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/unused")
os.environ.setdefault("DB_NAME", "test_unused")

from screening_outcome import preserve_attendance  # noqa: E402


class TestReschedulingKeepsTheOldAnswer(unittest.TestCase):
    def test_a_recorded_no_show_is_kept_when_they_rebook(self):
        """The exact 57. Marked no_show, rescheduled, and the answer vanished —
        hiding a real no-show from every report, and inflating the "nobody
        records outcomes" figure with cases where somebody did."""
        cand = {
            "attendance_status": "no_show",
            "appointment_at": "2026-07-01T13:00:00+00:00",
            "attendance_recorded_at": "2026-07-01T15:00:00+00:00",
            "attendance_recorded_by": "user-1",
        }
        push = preserve_attendance(cand)
        entry = push["attendance_history"]
        self.assertEqual(entry["status"], "no_show")
        self.assertEqual(entry["appointment_at"], "2026-07-01T13:00:00+00:00")
        self.assertEqual(entry["recorded_by"], "user-1")
        self.assertTrue(entry["cleared_at"])

    def test_every_outcome_type_is_kept_not_just_no_shows(self):
        for status in ("attended_form", "attended_no_form", "no_show"):
            push = preserve_attendance({"attendance_status": status})
            self.assertEqual(push["attendance_history"]["status"], status, status)

    def test_nothing_is_written_when_there_was_nothing_to_keep(self):
        """A candidate who was never marked must not gain an empty history entry
        — that would make "has been rescheduled" look like "was once answered"."""
        self.assertEqual(preserve_attendance({}), {})
        self.assertEqual(preserve_attendance({"attendance_status": None}), {})
        self.assertEqual(preserve_attendance({"attendance_status": ""}), {})

    def test_it_is_shaped_for_a_push_not_a_set(self):
        """History accumulates. A candidate can no-show twice, and overwriting
        would leave the same hole one reschedule further along."""
        push = preserve_attendance({"attendance_status": "no_show"})
        self.assertEqual(list(push), ["attendance_history"])
        self.assertIsInstance(push["attendance_history"], dict)

    def test_the_reschedule_paths_actually_call_it(self):
        """The helper existing changes nothing on its own."""
        for path in (("routes", "attendance.py"), ("training_sms.py",)):
            full = os.path.join(os.path.dirname(__file__), "..", *path)
            with open(full, encoding="utf-8") as fh:
                self.assertIn("preserve_attendance", fh.read(), path)


class TestTheArchiveStopsHidingUnansweredSessions(unittest.TestCase):
    """Read from source: the property is which cutoff a query uses, and there is
    no way to exercise the sweep here without a live scheduler and database."""

    def _sweep_source(self):
        path = os.path.join(os.path.dirname(__file__), "..", "dialer", "scheduler.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        start = src.index("async def _auto_archive_stale_no_shows")
        return src[start:src.index("async def", start + 10)]

    def test_unanswered_interviews_get_a_fortnight_not_two_days(self):
        src = self._sweep_source()
        self.assertIn("unrecorded_cutoff", src)
        self.assertIn("timedelta(days=14)", src)

    def test_the_lapsed_query_uses_the_longer_window(self):
        """A Friday session archived before Monday can never be marked, because
        both the board and CG1's list hide archived candidates."""
        src = self._sweep_source()
        lapsed = src[src.index("silently_lapsed"):src.index("Deduplicate")]
        self.assertIn("unrecorded_cutoff", lapsed)

    def test_someone_who_cancelled_in_advance_still_goes_after_48_hours(self):
        """Nothing is waiting to be answered about them, so holding them on the
        board for a fortnight would just be clutter."""
        src = self._sweep_source()
        lapsed = src[src.index("silently_lapsed"):src.index("Deduplicate")]
        self.assertIn("appointment_cancelled_at", lapsed)
        self.assertIn("cutoff", lapsed)

    def test_an_explicit_no_show_is_untouched_by_this_change(self):
        """Already answered — archiving it promptly is correct, and it is what
        hands the revival dialler its queue."""
        src = self._sweep_source()
        marked = src[src.index("explicitly_marked"):src.index("silently_lapsed")]
        self.assertIn('"attendance_status": "no_show"', marked)
        self.assertNotIn("unrecorded_cutoff", marked)

    def test_the_two_windows_are_actually_different(self):
        # Guards against someone "simplifying" them back into one value, which
        # would silently restore the behaviour this whole change removes.
        src = self._sweep_source()
        self.assertIn("timedelta(hours=48)", src)
        self.assertIn("timedelta(days=14)", src)


class TestAttendedNotProgressedLeavesTheBoard(unittest.TestCase):
    """attended_no_form is a final answer: they came, and the team chose not to
    invite them onward. Nothing ever removed them from the APPOINTMENT column —
    29 sat on the boards a median of 19 days, padding the count the team reads
    as "bookings"."""

    def _sweep_source(self):
        path = os.path.join(os.path.dirname(__file__), "..", "dialer", "scheduler.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        start = src.index("async def _auto_archive_stale_no_shows")
        return src[start:src.index("async def", start + 10)]

    def _case_four(self):
        src = self._sweep_source()
        return src[src.index("attended_done"):src.index("# Deduplicate")]

    def test_attended_no_form_is_swept(self):
        self.assertIn('"attendance_status": "attended_no_form"', self._case_four())

    def test_it_is_archived_under_the_reason_recruiters_already_use(self):
        """Manual archives of these candidates already say attended_no_form —
        the sweep must file them identically or the archive splits in two."""
        self.assertIn('"attended_no_form") for c in attended_done', self._sweep_source())

    def test_grace_runs_from_the_recording_not_the_slot(self):
        """An outcome marked three days after the session must still get its
        48 hours on the board — archiving on slot time would remove it the
        moment it was recorded."""
        block = self._case_four()
        self.assertIn('"attendance_recorded_at": {"$lt": cutoff}', block)

    def test_it_uses_the_short_window_not_the_fortnight(self):
        """The fortnight exists for UNANSWERED sessions. This one is answered."""
        self.assertNotIn("unrecorded_cutoff", self._case_four())

    def test_attended_form_is_never_swept(self):
        """attended_form auto-advances to FORM; one still sitting in
        APPOINTMENT means that advance failed, and archiving it would bury the
        evidence along with the candidate."""
        self.assertNotIn('"attended_form"', self._sweep_source())


class TestTheWindowIsWideEnoughToMatter(unittest.TestCase):
    def test_a_friday_session_survives_the_weekend(self):
        """The concrete failure: session Friday 2pm, nobody in until Monday 9am.
        At 48 hours it is archived on Sunday afternoon and gone."""
        session = datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc)   # a Friday
        monday = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)
        self.assertLess(session + timedelta(hours=48), monday)       # old: already archived
        self.assertGreater(session + timedelta(days=14), monday)     # new: still there

    def test_it_still_expires_eventually(self):
        session = datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc)
        self.assertLess(session + timedelta(days=14),
                        datetime(2026, 9, 7, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
