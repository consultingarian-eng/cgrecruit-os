"""Running the screening over SMS, all the way to a booked slot.

The SMS assistant already existed but at screening stage it was a concierge: it
answered questions about the role and handed out links. Three things stopped it
conducting a screening — it was never given the questions, it was never given
the slots, and `[BOOK:]` was honoured only at APPOINTMENT stage, so the model
could say "you're booked for Tuesday" and nothing at all would happen.

The fourth problem was subtler and is the one most of these tests are about: the
yes/no classifier runs on the raw text before anything knows what question was
asked. "No" to "can you commute four days a week?" was being read as a
withdrawal and sent to the branch that cancels appointments and archives people.

Pure: no database writes, no LLM, no Twilio.
"""
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/unused")
os.environ.setdefault("DB_NAME", "test_unused")

from training_sms import _classify, _is_withdrawn, _screening_block  # noqa: E402

QUESTIONS = [
    {"question": "Are you 18 or over?", "auto_screen": True},
    {"question": "Do you have the full right to work here?", "auto_screen": True},
    {"question": "Can you commute to our Downtown office 4+ days a week?", "auto_screen": True},
    {"question": "What are you looking for in your next role?", "auto_screen": False},
]


def enrichment(slots=None, questions=None, primary=2, fallback=1, mode="chat_first"):
    return {
        "settings": {
            "screen_call_agent": {
                "screening_mode": mode,
                "screening_questions": questions if questions is not None else QUESTIONS,
            },
            "booking_preferences": {"slots_primary": primary, "slots_fallback": fallback},
        },
        "slots": slots if slots is not None else [
            {"label": "Tue 9:15 AM", "datetime": "2026-08-04T13:15:00+00:00"},
        ],
    }


class TestTheAgentIsGivenTheQuestions(unittest.TestCase):
    def test_every_question_reaches_the_prompt(self):
        block = _screening_block(enrichment(), "SCREENING")
        for q in QUESTIONS:
            self.assertIn(q["question"], block)

    def test_they_come_from_settings_so_all_three_channels_agree(self):
        """Phone, web chat and SMS all read screen_call_agent.screening_questions.
        Editing them in Settings has to change all three, not two."""
        custom = [{"question": "Do you own a car?", "auto_screen": True}]
        block = _screening_block(enrichment(questions=custom), "SCREENING")
        self.assertIn("Do you own a car?", block)
        self.assertNotIn("Are you 18 or over?", block)

    def test_hard_gates_are_marked_and_soft_questions_are_not(self):
        """Left unmarked the model softens a disqualifying 'no' into 'we'll be
        in touch', which is how a 17-year-old stays in the pipeline."""
        block = _screening_block(enrichment(), "SCREENING")
        gate_line = [l for l in block.splitlines() if "18 or over" in l][0]
        soft_line = [l for l in block.splitlines() if "looking for" in l][0]
        self.assertIn("HARD GATE", gate_line)
        # The screener declares WHY it ended ([END:age]) so the rejection
        # doesn't depend on the summariser re-deriving it from a short thread.
        self.assertIn("[END:reason]", gate_line)
        self.assertIn("age", gate_line)
        self.assertNotIn("HARD GATE", soft_line)

    def test_other_stages_get_no_screening_block(self):
        for stage in ("TRAINING", "APPOINTMENT", "FORM", "CLOSE"):
            self.assertEqual(_screening_block(enrichment(), stage), "", stage)

    def test_applicant_stage_is_screened_too(self):
        self.assertNotEqual(_screening_block(enrichment(), "APPLICANT"), "")

    def test_no_questions_configured_produces_nothing_rather_than_a_broken_block(self):
        self.assertEqual(_screening_block(enrichment(questions=[]), "SCREENING"), "")


class TestItOnlyAppliesWhereChatIsTheScreeningChannel(unittest.TestCase):
    """Downtown-only means Downtown-only.

    The block is keyed on screening_mode, not just on stage. Under voice_first
    the phone runs the screening and SMS sits alongside it as a concierge —
    handing that office's assistant a questionnaire and a booking marker would
    switch on text-screening everywhere, including another office's live
    candidates a chat-first rollout is deliberately not touching.
    """

    def test_chat_first_gets_the_questionnaire(self):
        self.assertIn("SCREENING QUESTIONS", _screening_block(enrichment(mode="chat_first"), "SCREENING"))

    def test_chat_only_gets_it_too(self):
        self.assertIn("SCREENING QUESTIONS", _screening_block(enrichment(mode="chat_only"), "SCREENING"))

    def test_voice_first_gets_nothing(self):
        self.assertEqual(_screening_block(enrichment(mode="voice_first"), "SCREENING"), "")

    def test_the_legacy_spelling_riverside_actually_has_gets_nothing(self):
        # Legacy settings documents can still read "voice_only".
        self.assertEqual(_screening_block(enrichment(mode="voice_only"), "SCREENING"), "")

    def test_an_unset_mode_gets_nothing(self):
        """The safe default is today's behaviour. A settings doc that cannot be
        read must not silently start text-screening an office."""
        self.assertEqual(_screening_block(enrichment(mode=None), "SCREENING"), "")


class TestBookingRules(unittest.TestCase):
    def test_the_two_then_fallback_shape_comes_from_settings(self):
        block = _screening_block(enrichment(primary=3, fallback=2), "SCREENING")
        self.assertIn("FIRST 3 times", block)
        self.assertIn("next 2", block)

    def test_it_is_told_not_to_invent_a_time(self):
        """The one failure that costs a candidate: a confirmed interview that
        exists only in the conversation."""
        block = _screening_block(enrichment(), "SCREENING")
        self.assertIn("NEVER invent", block)
        self.assertIn("[BOOK:", block)

    def test_an_empty_schedule_forbids_offering_anything(self):
        block = _screening_block(enrichment(slots=[]), "SCREENING")
        self.assertIn("do NOT offer any time", block)

    def test_a_populated_schedule_does_not_carry_that_warning(self):
        # Otherwise the instruction above would be present always and mean nothing.
        self.assertNotIn("do NOT offer any time", _screening_block(enrichment(), "SCREENING"))


class TestYesNoAreAnswersNotCommands(unittest.TestCase):
    """The classifier fires before anything knows what was asked."""

    def test_the_classifier_really_does_read_screening_answers_as_commands(self):
        # Establishes the premise — without this the guard below proves nothing.
        self.assertEqual(_classify("no"), "no")
        self.assertEqual(_classify("yes"), "yes")
        self.assertEqual(_classify("sounds good"), "yes")

    def test_a_plain_no_is_not_a_withdrawal(self):
        """"No" to the commute question must not cancel their appointment or
        archive them."""
        for answer in ["no", "No", "nope", "not really", "no i cant"]:
            self.assertFalse(_is_withdrawn(answer), answer)

    def test_an_explicit_withdrawal_still_counts(self):
        for phrase in ["I'm not interested anymore", "please take me off your list"]:
            self.assertTrue(_is_withdrawn(phrase), phrase)

    def test_the_two_are_actually_distinguishable(self):
        # Guards against _is_withdrawn returning a constant, which would make
        # both tests above pass while the branch stayed broken.
        self.assertNotEqual(_is_withdrawn("no"), _is_withdrawn("i am not interested anymore"))


class TestBookingWritesTheRightThings(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = MagicMock()
        self.db.candidates.update_one = AsyncMock()
        self.db.candidates.find_one = AsyncMock(return_value={"id": "c1", "user_id": "u1"})
        cursor = MagicMock()
        cursor.to_list = AsyncMock(return_value=[])
        self.db.candidates.find = MagicMock(return_value=cursor)
        for p in [
            patch("deps.resolve_settings", AsyncMock(return_value={})),
            patch("deps.send_stage_comms", AsyncMock()),
            patch("auto_dialer.schedule_appointment_reminders", AsyncMock()),
            patch("auto_dialer.cancel_pending_retry_calls", MagicMock()),
        ]:
            p.start()
        self.addCleanup(patch.stopall)
        self.cand = {"id": "c1", "user_id": "u1", "pipeline_id": "p1"}
        self.pipe = {"id": "p1", "appointment_link": "https://us02web.zoom.us/j/x"}

    async def test_a_full_slot_is_refused_rather_than_overbooked(self):
        """The slot list is computed at the top of a request and the booking
        happens at the bottom, so two candidates texting at once can both be
        offered — and both take — the last seat."""
        from screening_outcome import book_screening_slot

        with patch("availability_service.slot_capacity_remaining", MagicMock(return_value=0)):
            res = await book_screening_slot(self.db, self.cand, self.pipe, "2026-08-04T13:15:00+00:00")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "full")
        self.db.candidates.update_one.assert_not_awaited()

    async def test_a_time_outside_the_schedule_is_refused(self):
        from screening_outcome import book_screening_slot

        with patch("availability_service.slot_capacity_remaining", MagicMock(return_value=-1)):
            res = await book_screening_slot(self.db, self.cand, self.pipe, "2026-08-04T03:00:00+00:00")
        self.assertEqual(res["reason"], "not_in_schedule")

    async def test_a_good_booking_writes_the_appointment_and_the_audit_line(self):
        from screening_outcome import book_screening_slot

        with patch("availability_service.slot_capacity_remaining", MagicMock(return_value=5)):
            res = await book_screening_slot(self.db, self.cand, self.pipe, "2026-08-04T13:15:00+00:00")
        self.assertTrue(res["ok"])
        call = self.db.candidates.update_one.await_args.args[1]
        self.assertEqual(call["$set"]["stage"], "APPOINTMENT")
        self.assertEqual(call["$set"]["screening_status"], "approved")
        self.assertEqual(call["$set"]["appointment_at"], "2026-08-04T13:15:00+00:00")
        # The Comms tab reads chat_log; without this a booking made by text
        # leaves no trace where a recruiter looks for the story.
        self.assertIn("Booked an interview slot", call["$push"]["chat_log"]["text"])

    async def test_the_meeting_link_falls_back_to_the_pipelines(self):
        from screening_outcome import book_screening_slot

        with patch("availability_service.slot_capacity_remaining", MagicMock(return_value=5)):
            res = await book_screening_slot(self.db, self.cand, self.pipe, "2026-08-04T13:15:00+00:00")
        self.assertEqual(res["link"], "https://us02web.zoom.us/j/x")


class TestEnrichmentCarriesTheFullSettingsDoc(unittest.TestCase):
    """_screening_block's mode gate reads enrichment["settings"] — and the
    context fetch used to build that dict from a raw find_one with a
    four-field projection that omitted screen_call_agent entirely. The gate
    then resolved voice_first (its safe fallback) and the SMS screener never
    ran in production, while every mocked test passed by handing the block a
    full settings dict. The fetch must go through resolve_settings: full doc,
    override-or-global fallback, set unconditionally (the old code also
    skipped settings entirely when a starter-email snapshot existed)."""

    def _fetch_block(self):
        path = os.path.join(os.path.dirname(__file__), "..", "training_sms.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        start = src.index("async def _fetch_llm_context")
        return src[start:src.index("# 2.", start)]

    def test_settings_come_from_the_resolver_not_a_projected_query(self):
        block = self._fetch_block()
        self.assertIn("resolve_settings", block)
        self.assertNotIn('"starter_template": 1', block, "projected settings query is back")

    def test_settings_are_set_outside_the_snapshot_branch(self):
        block = self._fetch_block()
        self.assertIn('ctx["settings"] = await resolve_settings', block)


class TestSmsRepliesSeeTheWebChat(unittest.TestCase):
    """The mirror of the portal loading SMS history: a candidate who starts on
    the portal and then texts (e.g. replying to a chase) is mid-conversation,
    and the SMS screener must see their chat answers or it re-asks them. The
    reply branches opt in via include_chat=True; the recruiter SMS inbox and
    the email agent stay channel-pure on the default."""

    def _src(self):
        path = os.path.join(os.path.dirname(__file__), "..", "training_sms.py")
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_every_reply_branch_requests_chat_history(self):
        src = self._src()
        self.assertEqual(src.count("_get_thread(db, cand_id, include_chat=_screening_stage)"), 6)
        # No reply branch left on the SMS-only default.
        self.assertNotIn("history = await _get_thread(db, cand_id)\n", src)

    def test_chat_history_stops_at_the_screening_stages(self):
        # Replies to appointment confirmations and reminders keep channel-pure
        # context — old screening turns would muddy "still coming Thursday?".
        self.assertIn('_screening_stage = stage in ("SCREENING", "APPLICANT")', self._src())

    def test_the_inbox_view_stays_channel_pure(self):
        # The drawer's SMS tab endpoint keeps the default (no chat turns) —
        # the merged view is the Transcript tab's job.
        src = self._src()
        start = src.index("msgs = await _get_thread(db, candidate_id)")
        self.assertNotIn("include_chat", src[start:start + 60])


class TestTheSlotsListActuallyRenders(unittest.TestCase):
    """The booking instructions say "offer the first two from AVAILABLE SLOTS,
    exactly as written there" — and the list itself only rendered at
    APPOINTMENT stage. The first pure-SMS booking attempt read instructions
    about a section that wasn't in its prompt: it invented two times, leaked
    its own confusion mid-reply, and the fabricated time failed validation as
    "that time just went". The list must render for the screening stages."""

    def test_slots_render_for_screening_stages(self):
        path = os.path.join(os.path.dirname(__file__), "..", "training_sms.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        start = src.index("AVAILABLE SLOTS — also called")
        gate = src.rindex('in ("APPOINTMENT", "SCREENING", "APPLICANT")', 0, start)
        # The stage gate guarding the list must be the one just above it.
        self.assertLess(start - gate, 900)

    def test_the_sms_prompt_carries_the_role_facts(self):
        # "Pay rate?" by text was told to call the office while the scripted
        # answer sat unused in screen_call_agent.additional_context.
        path = os.path.join(os.path.dirname(__file__), "..", "training_sms.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("ROLE & OFFICE FACTS", src)
        self.assertIn('additional_context_override', src)

    def test_duplicate_paced_replies_are_suppressed(self):
        # Two rapid inbounds run two handlers; identical replies four seconds
        # apart reached a real candidate. The paced sender drops an exact echo.
        path = os.path.join(os.path.dirname(__file__), "..", "training_sms.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("suppressed — identical to last outbound", src)


class TestABareYesRunsTheScreening(unittest.TestCase):
    """The chat-first arrival text ends with a yes/no question ("are you 18 or
    over?"), so nearly every candidate's FIRST inbound is a bare "Yes" — and
    the yes-classification branch used to route that to a warm-acknowledgment
    LLM call whose markers were stripped, not acted on. Candidate #1 on
    go-live night answered "Yes" and was promised a call instead of being
    asked question 2. The non-training/appointment yes-arm must run the same
    marker-honouring path the screening uses."""

    def test_the_yes_arm_honours_reply_markers(self):
        path = os.path.join(os.path.dirname(__file__), "..", "training_sms.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        start = src.index('# A bare "Yes" outside TRAINING/APPOINTMENT')
        block = src[start:start + 1400]
        self.assertIn("_handle_reply_markers", block)
        self.assertNotIn("_extract_and_strip_marker", block)


if __name__ == "__main__":
    unittest.main()


HARD_GATES = ["age", "work_authorization", "schedule_availability", "commute"]


class TestTheFourHardGates(unittest.TestCase):
    """Age, right to work, schedule, commute. Four, and only these four end a
    screening — everything else is a judgement for a recruiter, not a gate."""

    def test_the_default_set_has_exactly_four(self):
        from models import ScreenCallAgentSettings

        qs = ScreenCallAgentSettings().screening_questions
        self.assertEqual(sum(1 for q in qs if q.get("auto_screen")), 4)

    def test_each_gate_has_a_rejection_reason_to_file_it_under(self):
        from screening_outcome import DISQ_LABELS

        for reason in HARD_GATES:
            self.assertIn(reason, DISQ_LABELS, reason)

    def test_the_gates_cover_the_four_things_and_nothing_else(self):
        from models import ScreenCallAgentSettings

        gates = " ".join(q["question"].lower()
                         for q in ScreenCallAgentSettings().screening_questions
                         if q.get("auto_screen"))
        for topic in ("18", "right to work", "commute"):
            self.assertIn(topic, gates, topic)
        self.assertTrue("monday" in gates or "schedule" in gates)


class TestGoingBackOnAnAnswer(unittest.TestCase):
    """A failed gate ends the conversation; it must not be a locked door.

    People misread "18 or over" on a phone screen, hear the schedule question as
    "every day", or say they can't commute and then realise the office is two
    stops away. If they message back and correct it, carrying on is the only
    sensible response — otherwise they are talking to a system that wrote them
    off and won't say so.
    """

    def _block(self, reason):
        return _screening_block(enrichment(), "SCREENING", {"disqualification_reason": reason})

    def test_a_ruled_out_candidate_is_told_they_can_be_let_back_in(self):
        block = self._block("minimum age requirement (18+)")
        self.assertIn("PREVIOUSLY RULED OUT", block)
        self.assertIn("[REOPEN]", block)

    def test_the_reason_they_were_ruled_out_is_named(self):
        # Otherwise the model cannot tell which answer would count as corrected.
        self.assertIn("minimum age requirement", self._block("minimum age requirement (18+)"))

    def test_it_must_read_the_correction_back_before_accepting_it(self):
        """A reopen on a misheard 'maybe' puts someone into an interview they
        can't attend, and wastes a seat in a capped session."""
        block = self._block("commutability to our office")
        self.assertIn("read it back", block.lower())
        self.assertIn("Do NOT reopen on a maybe", block)

    def test_someone_merely_asking_why_is_not_a_correction(self):
        self.assertIn("simply asking why", self._block("commutability to our office"))

    def test_a_candidate_who_was_never_ruled_out_sees_none_of_this(self):
        self.assertNotIn("PREVIOUSLY RULED OUT", _screening_block(enrichment(), "SCREENING", {}))
        self.assertNotIn("[REOPEN]", _screening_block(enrichment(), "SCREENING", None))

    def test_the_marker_is_stripped_before_anything_is_sent(self):
        import re

        path = os.path.join(os.path.dirname(__file__), "..", "training_sms.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('.replace("[REOPEN]", "")', src)
        self.assertIn("reopen_screening", src)
