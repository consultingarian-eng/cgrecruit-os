"""The screening was cut from eleven questions to six, and booking moved up.

A shorter screening gets candidates to a booked slot sooner, so the qualitative
questions moved to the post-booking form and the four hard gates stayed.

These tests pin the shape of that change. The gates are asserted *verbatim*,
because the failure mode worth catching is not "someone deletes the list" — that
is obvious — but "someone tidies the wording of the work-rights question and
quietly weakens a legal gate while the count still reads six".

Also guards the two prompt lines that were actively working against the booking
rate. Pure: no database, no HTTP, no ElevenLabs.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/unused")
os.environ.setdefault("DB_NAME", "test_unused")
# Frozen company profile, so the hours wording below is fixed.
os.environ["COMPANY_PROFILE_PATH"] = os.path.join(os.path.dirname(__file__), "company_profile.test.json")

from models import (  # noqa: E402
    COMM_TEMPLATE_KEYS,
    ScreenCallAgentSettings,
    get_default_templates,
)

QUESTIONS = ScreenCallAgentSettings().screening_questions

# The four disqualifiers, exactly as the agent must ask them. Changing any of
# these should require changing this list too — deliberately.
HARD_GATES = [
    "Awesome — first up, and I just have to ask: are you 18 or over?",
    "And do you have the full right to work here? Just want to make sure you're not on a student visa.",
    # The hours come from company_profile.json → work_schedule.
    "It's a full-time role - shifts run 10:00 AM – 6:00 PM, Monday to Friday. Does that schedule work for you?",
    "And we're recruiting specifically for our {{city}} office — can you reliably commute there 4+ days a week?",
]


class TestQuestionSet(unittest.TestCase):
    def test_there_are_six_questions(self):
        self.assertEqual(len(QUESTIONS), 6, [q["question"][:40] for q in QUESTIONS])

    def test_the_four_hard_gates_are_first_and_unchanged(self):
        self.assertEqual([q["question"] for q in QUESTIONS[:4]], HARD_GATES)

    def test_every_gate_still_auto_screens_and_nothing_else_does(self):
        """auto_screen is what actually rejects someone. A qualitative question
        flagged True would start disqualifying people on an opinion."""
        self.assertEqual([bool(q.get("auto_screen")) for q in QUESTIONS],
                         [True, True, True, True, False, False])

    def test_the_two_remaining_questions_are_about_motivation(self):
        tail = " ".join(q["question"].lower() for q in QUESTIONS[4:])
        self.assertIn("looking for", tail)
        self.assertIn("sales role", tail)

    def test_the_dropped_questions_are_gone_from_the_call(self):
        """These five moved to the post-booking form. Nothing is lost — they
        just stop standing between the candidate and a time."""
        joined = " ".join(q["question"].lower() for q in QUESTIONS)
        for dropped in ("rejection", "three words", "rapport", "customer-facing", "driving you"):
            self.assertNotIn(dropped, joined)

    def test_the_count_assertion_is_not_vacuous(self):
        # Guards against the list being empty or stubbed, which would make the
        # "dropped" assertions above pass without proving anything.
        self.assertTrue(all(q.get("question", "").strip() for q in QUESTIONS))


class TestPromptsThatFoughtTheBooking(unittest.TestCase):
    def test_the_agent_is_no_longer_told_to_refuse_same_day_slots(self):
        """The script carried "All slots are from tomorrow onwards — never offer
        today." The booking engine has always allowed same-day with an hour's
        notice, and same-or-next-day is the best-converting bucket.
        The agent was simply told not to use it.
        """
        import voice_service

        prompt = voice_service.__dict__.get("__file__")
        with open(prompt, encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("never offer today", source.lower())
        self.assertIn("Slots may include later today", source)

    def test_booking_follows_the_sixth_question_not_the_eleventh(self):
        instructions = ScreenCallAgentSettings().booking_instructions
        self.assertIn("question 6", instructions)
        self.assertNotIn("question 11", instructions)

    def test_there_is_no_preamble_before_the_first_question(self):
        """The scripted intro (AI heads-up + field-based reality check +
        'Ready?') was one long paragraph and a second confirmation before Q1
        ever arrived — and it pitched the role, which the user wants held back
        for the interview. The purpose must now forbid the speech, not script
        one."""
        purpose = ScreenCallAgentSettings().purpose
        self.assertIn("No preamble", purpose)
        self.assertNotIn("field-based", purpose)
        self.assertNotIn("Quick heads-up", purpose)

    def test_the_pacing_target_came_down_with_the_question_count(self):
        # Six questions in a slot sized for eleven just means slower questions.
        self.assertIn("about 3 minutes", ScreenCallAgentSettings().purpose)


class TestChatFirstTemplate(unittest.TestCase):
    def test_the_template_is_registered(self):
        self.assertIn("warmup_chat_first", COMM_TEMPLATE_KEYS)

    def test_it_exists_with_both_channels_filled_in(self):
        tpl = get_default_templates()["warmup_chat_first"]
        self.assertTrue(tpl.subject.strip())
        self.assertTrue(tpl.body.strip())
        # The SMS is the one that matters — it is the first thing they see and
        # the only one they can act on from a phone in thirty seconds.
        self.assertTrue(tpl.sms_body.strip())

    def test_the_sms_is_short_enough_to_arrive_as_one_message(self):
        tpl = get_default_templates()["warmup_chat_first"]
        # Placeholders expand, so this is a ceiling on the fixed text, not the
        # rendered length — but a 400-char template cannot possibly fit.
        self.assertLess(len(tpl.sms_body), 320, tpl.sms_body)

    def test_every_registered_template_key_has_a_default(self):
        # A key in the list with no default renders as an empty message rather
        # than failing loudly, so this is worth asserting for all of them.
        defaults = get_default_templates()
        missing = [k for k in COMM_TEMPLATE_KEYS if k not in defaults]
        self.assertEqual(missing, [])

    def test_no_template_wraps_the_role_placeholder_in_its_own_role_phrase(self):
        """[Role] falls back to "the role" when a candidate has no job attached
        (the intake webhook, manual add, an apply without a job selected). A
        template that writes its own article around it — "the [Role] role",
        "our [Role] position" — renders as "the the role role" for exactly those
        candidates, in the arrival SMS every applicant gets. Templates must use
        [Role Phrase], whose fallback is grammatical either way."""
        for key, tpl in get_default_templates().items():
            for channel in ("body", "sms_body"):
                text = getattr(tpl, channel, "") or ""
                for bad in ("[Role] role", "[Role] position", "[Role] interview", "[Role] application"):
                    self.assertNotIn(bad, text, f"{key}.{channel} wraps [Role] in a role phrase")



# GSM-7: the character set a plain SMS uses. One character outside it switches
# the whole message to UCS-2 and the segment size drops from 160 to 70.
GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
    "^{}\\[~]|€"
)


class TestTheArrivalTextIsCheapToSend(unittest.TestCase):
    """This is the message every single applicant receives, so it is the
    highest-volume send in the system and the one where encoding matters."""

    def _sms(self):
        return get_default_templates()["warmup_chat_first"].sms_body

    def test_it_uses_only_characters_a_plain_sms_can_carry(self):
        """An em dash or a curly apostrophe flips the message to UCS-2, taking
        it from 160 characters per segment to 70 — one text becomes four, on
        every applicant."""
        offenders = sorted({c for c in self._sms() if c not in GSM7})
        self.assertEqual(offenders, [], f"non-GSM-7 characters: {offenders}")

    def test_the_reengage_text_is_also_gsm7_and_opens_with_q1(self):
        """The backlog-revival SMS goes to a whole cohort in one press — same
        cost math as the arrival text, and same design: answering IS starting."""
        from models import get_default_templates
        sms = get_default_templates()["reengage_chat"].sms_body
        offenders = sorted({c for c in sms if c not in GSM7})
        self.assertEqual(offenders, [], f"non-GSM-7 characters: {offenders}")
        self.assertIn("18 or over", sms)

    def test_reengage_is_dry_run_by_default_and_respects_opt_outs(self):
        """A mass send must never fire from a permissions slip or a stray
        click: dry_run defaults true, opted-out and disqualified candidates are
        excluded, and the stamp prevents a double-send."""
        path = os.path.join(os.path.dirname(__file__), "..", "server.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        start = src.index("async def reengage_by_text")
        block = src[start:src.index("@api.get", start)]
        self.assertIn('payload.get("dry_run", True)', block)
        self.assertIn('"sms_opted_out": {"$ne": True}', block)
        self.assertIn('"disqualification_reason": None', block)
        self.assertIn('"reengaged_by_text_at": None', block)

    def test_the_check_is_not_vacuous(self):
        # If GSM7 accidentally contained everything, the test above would pass
        # on any string at all.
        for bad in ("—", "’", "😀", "…"):
            self.assertNotIn(bad, GSM7, bad)

    def test_it_fits_a_sane_number_of_segments_once_rendered(self):
        rendered = (self._sms()
                    .replace("[First Name]", "Kai")
                    .replace("[Role Phrase]", "Customer Service / Sales Representative role")
                    .replace("[Company]", "Example Co"))
        self.assertLessEqual(len(rendered), 320, f"{len(rendered)} chars = 3+ segments")

    def test_it_still_opens_with_the_first_gate(self):
        self.assertIn("18", self._sms())
        self.assertTrue(self._sms().rstrip().endswith("?"))

if __name__ == "__main__":
    unittest.main()
