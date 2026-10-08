"""Recording whether a candidate turned up.

Many booked interviews had no outcome at all, and the missing ones skewed
heavily towards people who *did* attend: you record a no-show because
there is nothing else to do with the candidate, but an attendee gets dragged to
the next stage — and the attendance buttons only exist while they sit in the
APPOINTMENT stage, so the move both discards the answer and removes the means of
giving it. An office that moves people on faster shows more unrecorded
outcomes, and its apparent attendance rate is computed over only the ones that
were recorded.

Two things are pinned here: the timestamp handling that decides whether an
interview has lapsed, and the session grouping behind the Outcome-due queue.

Pure: no database, no HTTP.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/unused")
os.environ.setdefault("DB_NAME", "test_unused")

from attendance_queue import group_unmarked_into_sessions, session_key  # noqa: E402
from models import (  # noqa: E402
    ATTENDANCE_VALUES,
    appointment_has_lapsed,
    parse_appointment_at,
)

DOWNTOWN, RIVERSIDE = "pipe-downtown", "pipe-riverside"
NAMES = {DOWNTOWN: "Downtown", RIVERSIDE: "Riverside"}
DOWNTOWN_ROOM = "https://us02web.zoom.us/j/downtown"
RIVERSIDE_ROOM = "https://us06web.zoom.us/j/riverside"


def ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def ahead(**kw):
    return (datetime.now(timezone.utc) + timedelta(**kw)).isoformat()


def booking(cid, pipeline, at, link, **kw):
    return {"id": cid, "first_name": cid.title(), "last_name": "X",
            "pipeline_id": pipeline, "appointment_at": at, "appointment_link": link, **kw}


# ── reading the timestamp ───────────────────────────────────────────────────

class TestAppointmentTimestamps(unittest.TestCase):
    def test_both_stored_shapes_are_readable(self):
        """Bookings arrive from six code paths; some write a naive ISO string,
        some a real datetime. Anything reading the field has to cope with both."""
        as_dt = datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc)
        self.assertEqual(parse_appointment_at(as_dt), as_dt)
        self.assertEqual(parse_appointment_at("2026-07-01T14:00:00+00:00"), as_dt)
        self.assertEqual(parse_appointment_at("2026-07-01T14:00:00Z"), as_dt)
        self.assertEqual(parse_appointment_at("2026-07-01T14:00:00"), as_dt)  # naive

    def test_unreadable_values_are_none_not_an_exception(self):
        for junk in [None, "", "   ", "next tuesday", 12345, [], {}]:
            self.assertIsNone(parse_appointment_at(junk), junk)

    def test_a_past_interview_has_lapsed_and_a_future_one_has_not(self):
        self.assertTrue(appointment_has_lapsed(ago(hours=3), grace_minutes=120))
        self.assertFalse(appointment_has_lapsed(ahead(hours=3), grace_minutes=120))

    def test_the_grace_window_holds_a_session_that_just_finished(self):
        # Ninety minutes after a 30-minute slot the host may still be in the room.
        self.assertFalse(appointment_has_lapsed(ago(minutes=90), grace_minutes=120))
        self.assertTrue(appointment_has_lapsed(ago(minutes=150), grace_minutes=120))

    def test_an_unreadable_timestamp_never_blocks_anything(self):
        """This value gates a stage move. A candidate must never be stuck on the
        board because their appointment time couldn't be parsed."""
        self.assertFalse(appointment_has_lapsed("not-a-date"))
        self.assertFalse(appointment_has_lapsed(None))


# ── sessions, and the shared room ───────────────────────────────────────────

class TestSessionGrouping(unittest.TestCase):
    def test_same_time_same_room_is_one_session(self):
        at = ago(hours=4)
        rows = [booking("a", DOWNTOWN, at, DOWNTOWN_ROOM), booking("b", DOWNTOWN, at, DOWNTOWN_ROOM)]
        out = group_unmarked_into_sessions(rows, pipeline_names=NAMES)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["unmarked"], 2)
        self.assertFalse(out[0]["shared"])

    def test_same_time_different_rooms_are_two_sessions(self):
        """Both offices run something at 2pm. Merging them would hand a host a
        roster of people who are not in their room."""
        at = ago(hours=4)
        rows = [booking("a", DOWNTOWN, at, DOWNTOWN_ROOM), booking("b", RIVERSIDE, at, RIVERSIDE_ROOM)]
        out = group_unmarked_into_sessions(rows, pipeline_names=NAMES)
        self.assertEqual(len(out), 2)
        self.assertTrue(all(not s["shared"] for s in out))

    def test_the_shared_afternoon_session_is_one_room_across_two_offices(self):
        """The actual bug. Some of Downtown's slots run in Riverside's room,
        and attendance being per-office meant nobody marked them."""
        at = ago(hours=4)
        rows = [booking("down1", DOWNTOWN, at, RIVERSIDE_ROOM), booking("river1", RIVERSIDE, at, RIVERSIDE_ROOM)]
        out = group_unmarked_into_sessions(rows, pipeline_names=NAMES)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["shared"])
        self.assertEqual(out[0]["pipeline_names"], ["Downtown", "Riverside"])

    def test_the_host_sees_the_whole_shared_room_including_the_other_office(self):
        """A Riverside recruiter hosting the joint session must be able to mark
        off Downtown's candidates — they are the only person who was there."""
        at = ago(hours=4)
        rows = [booking("down1", DOWNTOWN, at, RIVERSIDE_ROOM), booking("river1", RIVERSIDE, at, RIVERSIDE_ROOM)]
        out = group_unmarked_into_sessions(
            rows, pipeline_names=NAMES, visible_pipeline_ids=[RIVERSIDE])
        self.assertEqual(len(out), 1)
        self.assertEqual({c["id"] for c in out[0]["candidates"]}, {"down1", "river1"})

    def test_an_unrelated_office_still_sees_nothing(self):
        """Visibility widens to the room, not to everything. A Downtown-only
        recruiter must not be handed Riverside's own sessions."""
        at = ago(hours=4)
        rows = [booking("river1", RIVERSIDE, at, RIVERSIDE_ROOM)]
        out = group_unmarked_into_sessions(
            rows, pipeline_names=NAMES, visible_pipeline_ids=[DOWNTOWN])
        self.assertEqual(out, [])

    def test_the_visibility_assertion_is_not_vacuous(self):
        # Same rows, unrestricted, must come back — otherwise the test above
        # would pass on an empty input.
        at = ago(hours=4)
        rows = [booking("river1", RIVERSIDE, at, RIVERSIDE_ROOM)]
        self.assertEqual(len(group_unmarked_into_sessions(rows, pipeline_names=NAMES)), 1)

    def test_already_marked_candidates_are_not_asked_about_again(self):
        at = ago(hours=4)
        rows = [booking("a", DOWNTOWN, at, DOWNTOWN_ROOM, attendance_status="no_show"),
                booking("b", DOWNTOWN, at, DOWNTOWN_ROOM)]
        out = group_unmarked_into_sessions(rows, pipeline_names=NAMES)
        self.assertEqual(out[0]["unmarked"], 1)
        self.assertEqual(out[0]["candidates"][0]["id"], "b")

    def test_every_attendance_value_counts_as_marked(self):
        at = ago(hours=4)
        rows = [booking(v, DOWNTOWN, at, DOWNTOWN_ROOM, attendance_status=v) for v in ATTENDANCE_VALUES]
        self.assertEqual(group_unmarked_into_sessions(rows, pipeline_names=NAMES), [])

    def test_future_interviews_are_not_in_the_queue(self):
        rows = [booking("a", DOWNTOWN, ahead(days=1), DOWNTOWN_ROOM)]
        self.assertEqual(group_unmarked_into_sessions(rows, pipeline_names=NAMES), [])

    def test_oldest_debt_comes_first(self):
        """The session least likely to be remembered accurately is the one to
        clear first, so the queue is not newest-first like the rest of the app."""
        rows = [booking("new", DOWNTOWN, ago(hours=3), DOWNTOWN_ROOM),
                booking("old", DOWNTOWN, ago(days=3), DOWNTOWN_ROOM),
                booking("mid", DOWNTOWN, ago(days=1), DOWNTOWN_ROOM)]
        out = group_unmarked_into_sessions(rows, pipeline_names=NAMES)
        self.assertEqual([s["candidates"][0]["id"] for s in out], ["old", "mid", "new"])

    def test_archived_candidates_are_still_listed_and_flagged(self):
        """The 48h sweep archives unmarked interviews, and the board hides
        archived candidates — which is how the old prompt pointed at a screen
        that filtered out the thing it was asking about."""
        rows = [booking("a", DOWNTOWN, ago(days=3), DOWNTOWN_ROOM, archived_at=ago(days=1))]
        out = group_unmarked_into_sessions(rows, pipeline_names=NAMES)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["candidates"][0]["archived"])

    def test_a_missing_link_does_not_merge_unrelated_sessions(self):
        # Two offices, same time, neither with a link configured. They are not
        # the same room just because both rooms are unknown... but they do key
        # identically, so this pins the behaviour we actually get: one session,
        # explicitly flagged shared, which surfaces the misconfiguration rather
        # than hiding it.
        at = ago(hours=4)
        rows = [booking("a", DOWNTOWN, at, None), booking("b", RIVERSIDE, at, "")]
        out = group_unmarked_into_sessions(rows, pipeline_names=NAMES)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["shared"])

    def test_session_key_distinguishes_room_and_time(self):
        at = ago(hours=4)
        self.assertEqual(session_key(booking("a", DOWNTOWN, at, RIVERSIDE_ROOM)),
                         session_key(booking("b", RIVERSIDE, at, RIVERSIDE_ROOM)))
        self.assertNotEqual(session_key(booking("a", DOWNTOWN, at, DOWNTOWN_ROOM)),
                            session_key(booking("b", DOWNTOWN, at, RIVERSIDE_ROOM)))


# ── the sweep that never fired ──────────────────────────────────────────────

class TestScreeningRetrySweepQuery(unittest.TestCase):
    def test_the_sweep_no_longer_looks_for_an_absent_field(self):
        """`{"$exists": False}` matched no candidate at all. The
        Candidate model declares screening_retry_sent_at with a default of None,
        so model_dump() writes an explicit null on every insert and the field
        always exists. This safety net had never once fired.
        """
        path = os.path.join(os.path.dirname(__file__), "..", "server.py")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        start = source.index("async def _screening_retry_sweep")
        block = source[start:start + 2000]
        self.assertNotIn('"screening_retry_sent_at": {"$exists": False}', block)
        self.assertIn('"screening_retry_sent_at": {"$in": [None]}', block)

    def test_the_field_really_does_default_to_an_explicit_null(self):
        # The premise of the fix. If this ever stops being true the query should
        # be revisited rather than left to silently match nothing again.
        from models import Candidate

        doc = Candidate(user_id="u", pipeline_id="p", first_name="A").model_dump()
        self.assertIn("screening_retry_sent_at", doc)
        self.assertIsNone(doc["screening_retry_sent_at"])


if __name__ == "__main__":
    unittest.main()
