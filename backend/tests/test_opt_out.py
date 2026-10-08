"""Opting out: one unambiguous way in, and it has to actually work.

Two separate defects, in opposite directions.

The rule was far too broad. Any message merely *containing* "stop",
"unsubscribe", "end" or "quit" as a word unsubscribed the candidate and archived
them. "I quit my last job in June", "I'll be free at the end of the month" and
"sorry, had to stop for petrol" all silently removed a live applicant — and
invisibly, because the symptom is somebody who stops replying, which is
indistinguishable from an ordinary drop-off.

And it never worked anyway. `send_candidate_sms` checks the `sms_optouts`
collection, and `mark_opted_out` was never called from anywhere in the codebase,
so that collection was empty. Even once called, the two sides keyed on different
shapes of the same number — Twilio's "+16175551234" against a candidate record's
"(617) 555-1234" — so a lookup would never have matched.

Now: exactly "STOP" in capitals, on its own. Anything that reads like an opt-out
without being the keyword gets asked to confirm, because both silent options are
wrong — acting on it guesses, ignoring it leaves someone who asked to be left
alone still being messaged.

Pure: no database, no Twilio.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/unused")
os.environ.setdefault("DB_NAME", "test_unused")

from conversation_guard import (  # noqa: E402
    OPT_OUT_CONFIRMATION,
    is_hard_stop,
    looks_like_opt_out,
)
from sms_service import optout_key  # noqa: E402
from training_sms import _classify  # noqa: E402


class TestOnlyTheExactKeywordStops(unittest.TestCase):
    def test_stop_alone_in_capitals(self):
        self.assertTrue(is_hard_stop("STOP"))
        self.assertTrue(is_hard_stop("  STOP  "))     # whitespace is not a word
        self.assertTrue(is_hard_stop("STOP."))        # a full stop is not either

    def test_standalone_stop_opts_out_any_case(self):
        """Owner's rule (2026-08-09): the whole-message anchor is what
        disambiguates, not capitalisation — every phone capitalises "Stop",
        and demanding capitals left people who followed our own instruction
        being called and texted."""
        for text in ["stop", "Stop", "sToP", "STOP", "Stop.", "stop!"]:
            self.assertTrue(is_hard_stop(text), text)

    def test_the_word_inside_a_sentence_never_counts(self):
        REAL_MESSAGES = [
            "I quit my last job in June",
            "I'll be free at the end of the month",
            "sorry, had to stop for petrol on the way",
            "can you stop by the office first?",
            "STOP messaging me at work please",      # not alone — gets asked
            "I want to STOP and think about it",
            "non-stop shifts at the moment",
            "my last role ended in May",
            "STOP AND SEARCH training",
        ]
        for text in REAL_MESSAGES:
            self.assertFalse(is_hard_stop(text), text)

    def test_the_old_keywords_no_longer_opt_anyone_out_on_their_own(self):
        """"end", "quit" and "cancel" were in the stop list. All three are
        ordinary words in a recruiting conversation."""
        for text in ["END", "quit", "QUIT", "cancel", "unsubscribe"]:
            self.assertFalse(is_hard_stop(text), text)

    def test_classification_agrees(self):
        self.assertEqual(_classify("STOP"), "stop")
        self.assertEqual(_classify("stop"), "stop")
        self.assertEqual(_classify("Stop"), "stop")
        self.assertNotEqual(_classify("I quit my last job"), "stop")
        self.assertNotEqual(_classify("please stop texting me"), "stop")

    def test_the_strictness_is_not_vacuous(self):
        # If is_hard_stop always returned False the tests above would all pass
        # while nobody could ever opt out at all.
        self.assertTrue(is_hard_stop("STOP"))


class TestAmbiguousRequestsAreAskedNotGuessed(unittest.TestCase):
    def test_clear_requests_trigger_a_confirmation(self):
        for text in ["please stop texting me", "unsubscribe", "take me off your list",
                     "stop contacting me", "no more messages please", "leave me alone"]:
            self.assertTrue(looks_like_opt_out(text), text)

    def test_those_are_classified_for_the_confirm_branch_not_the_stop_branch(self):
        self.assertEqual(_classify("please stop texting me"), "maybe_stop")

    def test_ordinary_messages_are_not_asked_to_confirm(self):
        for text in ["yes", "I'm 22", "can you stop by the office?",
                     "I quit my last job in June", "what's the pay?"]:
            self.assertFalse(looks_like_opt_out(text), text)

    def test_the_confirmation_names_the_exact_keyword(self):
        """It is the only way out, so it has to be spelled out in the message
        itself — on its own, any case."""
        self.assertIn("STOP", OPT_OUT_CONFIRMATION)
        self.assertIn("on its own", OPT_OUT_CONFIRMATION.lower())

    def test_the_confirmation_does_not_itself_read_as_an_opt_out(self):
        # It contains the word STOP; it must not match the hard rule if it were
        # ever echoed back through classification.
        self.assertFalse(is_hard_stop(OPT_OUT_CONFIRMATION))


class TestTheOptOutListCanActuallyBeMatched(unittest.TestCase):
    def test_every_way_a_number_gets_written_resolves_to_one_key(self):
        """The defect that made the list useless even once written to: Twilio
        sends "+16175551234", the candidate record holds "(617) 555-1234", and
        an exact-string lookup between them never matches."""
        shapes = ["+16175551234", "16175551234", "6175551234",
                  "(617) 555-1234", "617-555-1234", "617.555.1234", " 617 555 1234 "]
        keys = {optout_key(s) for s in shapes}
        self.assertEqual(keys, {"6175551234"}, keys)

    def test_different_numbers_do_not_collide(self):
        self.assertNotEqual(optout_key("+16175551234"), optout_key("+16175551235"))

    def test_junk_does_not_become_a_key_that_matches_everyone(self):
        self.assertEqual(optout_key(""), "")
        self.assertEqual(optout_key("not a phone"), "")

    def test_the_sender_records_the_opt_out_at_all(self):
        """mark_opted_out existed and was called from nowhere, so sms_optouts was
        empty and ~15 send paths checked an empty list. Assert there is now a
        caller."""
        path = os.path.join(os.path.dirname(__file__), "..", "training_sms.py")
        with open(path, encoding="utf-8") as fh:
            self.assertIn("mark_opted_out", fh.read())


if __name__ == "__main__":
    unittest.main()
