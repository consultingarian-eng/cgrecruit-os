"""A text screening has to conclude something.

Until chat became the front door it concluded nothing. `[END]` was emitted both
when a candidate booked and when they failed a hard gate, and downstream the two
were identical: no verdict, no score, no summary, no rejection, no notification,
no status change. A 17-year-old could finish the chat, be told "thanks, all the
best", and then keep receiving automated calls — because nothing had marked them
rejected and the dialler only stands down on approved/rejected.

The tests below are mostly about that asymmetry. Passing is easy to get right by
accident; failing is not. So the rejection path is asserted in detail — status,
reason label, the dialler standing down, the rejection notice going out, and the
one case where it must NOT go out.

Pure: the database and the LLM are both mocked. No network, no scheduler.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/unused")
os.environ.setdefault("DB_NAME", "test_unused")

from ai_service import MIN_TEXT_REPLIES_FOR_VERDICT, summarize_text_screening  # noqa: E402
from models import local_slot_label  # noqa: E402
from screening_outcome import (  # noqa: E402
    DISQ_LABELS,
    conclude_text_screening,
    ensure_screening_outcome,
    transcript_from_chat_log,
)

CAND = {"id": "c1", "user_id": "u1", "pipeline_id": "p1",
        "first_name": "Kai", "last_name": "D", "chat_log": []}


def verdict(**kw):
    base = {"summary": "Answered everything.", "suitability_score": 72,
            "key_points": ["a"], "verdict": "good", "disqualification_reason": None}
    base.update(kw)
    return base


class OutcomeCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = MagicMock()
        self.db.candidates.update_one = AsyncMock()
        self.db.candidates.find_one = AsyncMock(return_value=dict(CAND))
        self.db.jobs.find_one = AsyncMock(return_value={"title": "Direct Sales Rep"})
        self.notify = AsyncMock()
        self.comms = AsyncMock()
        self.cancel = MagicMock()
        self.slot_picker = AsyncMock(return_value={})
        for p in [
            patch("notifications_service.create_notification", self.notify),
            patch("deps.send_stage_comms", self.comms),
            patch("auto_dialer.cancel_pending_retry_calls", self.cancel),
            patch("routes.attendance._fire_slot_picker_outreach", self.slot_picker),
        ]:
            p.start()
        self.addCleanup(patch.stopall)

    async def run_with(self, summary, **kw):
        with patch("ai_service.summarize_text_screening", AsyncMock(return_value=summary)):
            return await conclude_text_screening(self.db, dict(CAND), channel="retry", **kw)

    def written(self):
        """The $set from the outcome write (the first update_one call)."""
        return self.db.candidates.update_one.await_args_list[0].args[1]["$set"]


class TestHardGateFailure(OutcomeCase):
    async def test_a_failed_gate_actually_rejects(self):
        await self.run_with(verdict(verdict="weak", disqualification_reason="age"))
        w = self.written()
        self.assertEqual(w["screening_status"], "rejected")
        self.assertEqual(w["disqualification_reason"], DISQ_LABELS["age"])

    async def test_a_rejected_candidate_stops_being_phoned(self):
        """The consequence that mattered most: before this they stayed in the
        dialler queue and kept getting called after being told no."""
        await self.run_with(verdict(verdict="weak", disqualification_reason="work_authorization"))
        self.assertIs(self.written()["auto_dial"], False)
        self.cancel.assert_called_once_with("c1")

    async def test_the_rejection_notice_goes_out(self):
        await self.run_with(verdict(verdict="weak", disqualification_reason="commute"))
        self.comms.assert_awaited_once()
        self.assertEqual(self.comms.await_args.kwargs["template_key"], "rejection")

    async def test_someone_who_withdrew_is_not_sent_a_rejection(self):
        """"withdrawn" means they asked not to be contacted. Sending them a
        rejection notice is the one thing they explicitly said no to."""
        await self.run_with(verdict(verdict="weak", disqualification_reason="withdrawn"))
        self.assertEqual(self.written()["screening_status"], "rejected")
        self.comms.assert_not_awaited()

    async def test_every_reason_maps_to_a_readable_label(self):
        for reason in DISQ_LABELS:
            self.setUp()
            await self.run_with(verdict(verdict="weak", disqualification_reason=reason))
            label = self.written()["disqualification_reason"]
            self.assertEqual(label, DISQ_LABELS[reason])
            self.assertNotEqual(label, reason, "the raw enum should not reach a recruiter")


class TestPassingPaths(OutcomeCase):
    async def test_booked_is_approved(self):
        await self.run_with(verdict(), booked=True)
        self.assertEqual(self.written()["screening_status"], "approved")

    async def test_screened_but_unbooked_gets_the_slot_picker(self):
        await self.run_with(verdict(), booked=False)
        self.assertEqual(self.written()["screening_status"], "appointment_pending")
        self.slot_picker.assert_awaited_once()

    async def test_the_slot_picker_is_not_sent_twice(self):
        self.db.candidates.find_one = AsyncMock(
            return_value=dict(CAND, slot_picker_sent_at="2026-07-01T00:00:00Z"))
        await self.run_with(verdict(), booked=False)
        self.slot_picker.assert_not_awaited()

    async def test_the_verdict_lands_where_the_ui_already_reads_it(self):
        """Deliberately the same fields the voice path writes, so the verdict
        badge, the drawer card and the Kanban tooltip work unchanged."""
        await self.run_with(verdict(verdict="strong", suitability_score=91), booked=True)
        w = self.written()
        self.assertEqual(w["verdict"], "strong")
        self.assertEqual(w["suitability_score"], 91)
        self.assertTrue(w["call_summary"])
        self.assertEqual(w["screened_channel"], "retry")


class TestIncomplete(OutcomeCase):
    async def test_walking_away_is_not_scored_as_a_rejection(self):
        """Someone who stops replying has not failed screening — they have not
        had one. Marking them weak would quietly bury a live candidate."""
        await self.run_with(verdict(verdict="incomplete", suitability_score=0))
        w = self.written()
        self.assertEqual(w["screening_status"], "incomplete_info")
        self.assertNotIn("verdict", w)
        self.assertNotIn("suitability_score", w)
        self.comms.assert_not_awaited()

    async def test_a_scoring_failure_still_leaves_a_visible_state(self):
        with patch("ai_service.summarize_text_screening", AsyncMock(side_effect=RuntimeError("llm down"))):
            res = await conclude_text_screening(self.db, dict(CAND), channel="retry", booked=False)
        # Falls back to needs-a-human, never to a silent pass or fail.
        self.assertEqual(self.written()["screening_status"], "appointment_pending")
        self.assertIsNotNone(res)


class TestTheRecruiterFindsOut(OutcomeCase):
    async def test_every_conclusion_notifies_somebody(self):
        """The entire chat path created zero notifications. A recruiter had no
        way of learning a screening had happened without refreshing the board."""
        for summary in [verdict(), verdict(verdict="weak", disqualification_reason="age"),
                        verdict(verdict="incomplete")]:
            self.setUp()
            await self.run_with(summary)
            self.notify.assert_awaited_once()

    async def test_the_alert_says_which_channel_screened_them(self):
        await self.run_with(verdict(verdict="weak", disqualification_reason="age"))
        title = self.notify.await_args.args[2]
        self.assertIn("chat", title)
        self.assertIn("Kai", title)

    async def test_sms_screening_is_labelled_as_text_not_chat(self):
        with patch("ai_service.summarize_text_screening", AsyncMock(return_value=verdict())):
            await conclude_text_screening(self.db, dict(CAND), channel="sms", booked=True)
        self.assertIn("text", self.notify.await_args.args[2])


class TestTranscriptShaping(unittest.TestCase):
    def test_both_channels_are_included(self):
        log = [{"role": "agent", "channel": "retry", "text": "q"},
               {"role": "applicant", "channel": "sms", "text": "a"}]
        self.assertEqual(len(transcript_from_chat_log(log)), 2)

    def test_unrelated_chat_log_entries_are_excluded(self):
        """chat_log also carries system notes ("Resume received via email…")
        written with role=agent. Feeding those to the summariser would make an
        untouched candidate look like a conversation."""
        log = [{"role": "agent", "channel": "system", "text": "Resume received via email"},
               {"role": "applicant", "channel": "retry", "text": "hi"}]
        out = transcript_from_chat_log(log)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["role"], "applicant")

    def test_candidate_role_spellings_are_normalised(self):
        for spelling in ("applicant", "candidate", "user", "human", "APPLICANT"):
            out = transcript_from_chat_log([{"role": spelling, "channel": "retry", "text": "x"}])
            self.assertEqual(out[0]["role"], "applicant", spelling)


class TestShortConversationsAreNotScored(unittest.IsolatedAsyncioTestCase):
    async def test_a_barely_started_chat_is_incomplete_without_asking_the_llm(self):
        """Cheap and, more importantly, honest: with two replies the four hard
        gates cannot have been covered, so any verdict would be invention."""
        called = AsyncMock()
        with patch("ai_service._llm_chat", called):
            res = await summarize_text_screening(
                [{"role": "applicant", "text": "hi"}, {"role": "applicant", "text": "ok"}],
                "Kai", "Sales")
        self.assertEqual(res["verdict"], "incomplete")
        called.assert_not_awaited()

    async def test_short_answers_are_not_mistaken_for_disengagement(self):
        """The call-based guard is a word count, and "yes/yes/yes/Downtown" is a
        complete text screening in about six words. Using that guard here would
        mark good candidates as never screened."""
        replies = [{"role": "applicant", "text": "yes"} for _ in range(MIN_TEXT_REPLIES_FOR_VERDICT + 1)]
        with patch("ai_service._llm_chat", AsyncMock(return_value='{"verdict":"good","suitability_score":70}')):
            res = await summarize_text_screening(replies, "Kai", "Sales")
        self.assertEqual(res["verdict"], "good")

    async def test_an_empty_conversation_is_incomplete(self):
        self.assertEqual((await summarize_text_screening([], "M", "R"))["verdict"], "incomplete")


class TestOutcomeFollowsEveryBookingDoor(unittest.TestCase):
    """Bookings arrive through many doors — chat, SMS, portal grid, reschedule
    page, revival call, a recruiter's hand — but only the conversation paths
    wrote a verdict. The go-live audit found five booked candidates with full
    screenings and empty cards and one booked while still marked no_answer.
    ensure_screening_outcome runs after every door; these pin the guards and
    that every non-conversation door actually calls it."""

    def _src(self, *parts):
        path = os.path.join(os.path.dirname(__file__), "..", *parts)
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_the_hook_never_overwrites_an_existing_verdict(self):
        src = self._src("screening_outcome.py")
        start = src.index("async def ensure_screening_outcome")
        block = src[start:start + 2500]
        self.assertIn('if not cand.get("verdict"):', block)
        self.assertIn('sets["screening_status"] = "approved"', block)

    def test_every_link_and_manual_door_calls_the_hook(self):
        n = 0
        for f in (("routes", "public.py"), ("routes", "attendance.py"), ("server.py",)):
            n += self._src(*f).count("ensure_screening_outcome_later(")
        # portal book, book-by-phone, reschedule book, /move, recruiter
        # reschedule, manual add — plus the definition itself is not counted here.
        self.assertGreaterEqual(n, 6)

    def test_thin_material_notifies_instead_of_guessing(self):
        src = self._src("screening_outcome.py")
        self.assertIn("booked_unscreened", src)
        self.assertIn('sets["screening_material"] = "none"', src)


class TestNeverScreenedIsAVerifiedClaim(unittest.IsolatedAsyncioTestCase):
    """"Booked but never screened" is an accusation, and it was being made on
    the absence of a Mongo document rather than the absence of a screening.

    Rowan took a 36-turn screening call, booked a slot on it, and was flagged
    fifteen minutes later: his conversation row was still the stub the dialler
    creates, whose duration is 0 until the post-call step — the same step that
    writes the transcript — has run, so the "is a transcript still coming?"
    guard could never fire for the one case it existed for. Cosmo's long call
    was filed as a transcript-less no_answer, invisible to the auto-sync sweep,
    and he was flagged repeatedly over several days and texted the four gates
    he had answered out loud on the call.
    """

    def setUp(self):
        self.notify = AsyncMock()
        patch("notifications_service.create_notification", self.notify).start()
        self.addCleanup(patch.stopall)

    def _db(self, cand, convs):
        db = MagicMock()
        db.candidates.find_one = AsyncMock(return_value=cand)
        db.candidates.update_one = AsyncMock()
        db.conversations.update_one = AsyncMock()
        db.jobs.find_one = AsyncMock(return_value={"title": "Sales Rep"})
        db.conversations.find.return_value.sort.return_value.to_list = AsyncMock(return_value=convs)
        db.training_sms_messages.find.return_value.sort.return_value.to_list = AsyncMock(return_value=[])
        return db

    @staticmethod
    def _cand(**kw):
        c = {"id": "c1", "user_id": "u1", "pipeline_id": "p1", "first_name": "Rowan",
             "last_name": "M", "chat_log": [], "appointment_at": "2026-08-04T13:00:00+00:00"}
        c.update(kw)
        return c

    @staticmethod
    def _spoken(n=6):
        return [{"role": "agent" if i % 2 == 0 else "user", "text": f"turn {i}"} for i in range(n)]

    async def test_a_call_whose_post_call_has_not_run_is_deferred_not_accused(self):
        """The stub the dialler writes: status initiated, no duration, no
        transcript. Nothing about it says "this person was never screened" —
        it says "we have not looked yet"."""
        db = self._db(self._cand(), [{
            "id": "v1", "created_at": (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
            "status": "initiated", "duration_seconds": None, "transcript": [],
            "elevenlabs_conversation_id": "conv_x",
        }])
        with patch("voice_service.fetch_elevenlabs_conversation") as fetch:
            res = await ensure_screening_outcome(db, "c1")
        self.assertEqual(res.get("deferred"), "transcript_pending")
        self.notify.assert_not_awaited()
        fetch.assert_not_called()  # deferring already covers it; don't spend the call

    async def test_the_transcript_is_recovered_before_anyone_is_called_unscreened(self):
        """Cosmo's case: filed as no_answer with an empty transcript, so every
        local signal said "no screening". The screening was at ElevenLabs the
        whole time."""
        conv = {"id": "v1", "created_at": "2026-07-23T21:56:09+00:00", "status": "no_answer",
                "duration_seconds": 0, "transcript": [], "elevenlabs_conversation_id": "conv_tj"}
        db = self._db(self._cand(first_name="Cosmo", screening_material="none"), [conv])
        payload = {"transcript": [{"role": t["role"], "message": t["text"]} for t in self._spoken(34)],
                   "metadata": {"call_duration_secs": 283}}
        with patch("voice_service.fetch_elevenlabs_conversation", return_value=payload), \
             patch("ai_service.summarize_call_transcript",
                   AsyncMock(return_value=verdict(verdict="borderline"))), \
             patch("ai_service.summarize_text_screening",
                   AsyncMock(return_value=verdict(verdict="borderline"))):
            res = await ensure_screening_outcome(db, "c1")

        self.notify.assert_not_awaited()
        self.assertTrue(res["summarized"])
        # Recovered into Mongo, not just held in memory — and no longer a
        # "no_answer", so the drawer stops calling a 34-turn call a dead one.
        conv_set = db.conversations.update_one.await_args.args[1]["$set"]
        self.assertEqual(len(conv_set["transcript"]), 34)
        self.assertEqual(conv_set["duration_seconds"], 283)
        self.assertEqual(conv_set["status"], "completed")
        # And the stale flag is cleared, or the SMS screener keeps him in
        # gate-check mode re-asking questions he answered on the call.
        cand_set = db.candidates.update_one.await_args.args[1]["$set"]
        self.assertIsNone(cand_set["screening_material"])
        self.assertEqual(cand_set["verdict"], "borderline")

    async def test_a_genuinely_unscreened_booking_is_still_flagged(self):
        """The alarm has to survive the fix: ElevenLabs agreeing there was
        nothing is exactly the evidence that makes the claim true."""
        db = self._db(self._cand(first_name="Esme"), [{
            "id": "v1", "created_at": "2026-07-30T21:42:45+00:00", "status": "no_answer",
            "duration_seconds": 0, "transcript": [], "elevenlabs_conversation_id": "conv_j",
        }])
        with patch("voice_service.fetch_elevenlabs_conversation", return_value={"transcript": []}), \
             patch("screening_outcome.GATE_CHECKS_BY_TEXT", False):
            res = await ensure_screening_outcome(db, "c1")
        self.assertEqual(res["material_turns"], 0)
        self.notify.assert_awaited_once()
        self.assertIn("never screened", self.notify.await_args.args[2])
        self.assertEqual(
            db.candidates.update_one.await_args.args[1]["$set"]["screening_material"], "none")


class TestNotificationsSpeakLocalTime(unittest.TestCase):
    """The outcome-due nudge announced Downtown's 9:15 AM session as the "1:15 PM
    interview" — the UTC clock, four hours after the room had emptied. Slots are
    stored in UTC; anything shown to a human is converted."""

    def test_a_stored_utc_slot_reads_as_the_local_hour(self):
        self.assertEqual(
            local_slot_label("2026-07-31T13:15:00+00:00", "America/New_York"),
            "Fri 31 Jul, 9:15 AM")

    def test_the_pipelines_own_timezone_is_honoured(self):
        self.assertEqual(
            local_slot_label("2026-07-31T13:15:00+00:00", "America/Los_Angeles"),
            "Fri 31 Jul, 6:15 AM")

    def test_a_naive_value_is_left_where_it_was_written(self):
        """Naive stamps were written local by convention — shifting one would
        invent a four-hour error rather than remove one."""
        self.assertEqual(local_slot_label("2026-07-31T09:15:00"), "Fri 31 Jul, 9:15 AM")

    def test_an_unreadable_stamp_falls_back_instead_of_exploding(self):
        for junk in ("", None, "next tuesday", {}):
            self.assertEqual(local_slot_label(junk), "")

    def test_the_nudge_uses_it(self):
        path = os.path.join(os.path.dirname(__file__), "..", "server.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        block = src[src.index("async def _outcome_due_nudge"):]
        block = block[:block.index("_sms_scheduler.add_job(_outcome_due_nudge")]
        self.assertIn("local_slot_label(appt_at, tz_name)", block)
        self.assertNotIn('when.strftime("%a %-d %b, %-I:%M %p")', block)


if __name__ == "__main__":
    unittest.main()
