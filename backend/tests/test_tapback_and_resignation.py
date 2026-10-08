"""Arlo Larkspur's goodbye thread — a reaction loop, and a reminder for a job
she had already quit.

She resigned one morning by text. Within a quarter of an hour the assistant
had sent a string of farewells and then this:

    "Hey Arlo! Just a reminder your training starts Monday, August 10 at
     1:00 PM at 100 Example Street, Example City. Can you confirm with a YES that
     you'll be there?"

Three separate faults stacked up:

1. Tapbacks were only recognised in the older iOS shape, `Liked "…"`. Her phone
   sent the newer one, `👍 to "…"`, padded with hair and zero-width spaces. Each
   reaction therefore arrived as an ordinary message and drew another reply —
   and every reply drew another reaction.
2. `_is_withdrawn` was consulted only when a message classified as "no". Hers was
   polite and long, so it classified "unknown", went straight to the model, and
   nothing cancelled her reminders or archived her. A human did it much later.
3. With nothing left to say to a thumbs-up, the model fell back to the training
   script — and invented a date. Her training had already happened, and she attended it;
   the Monday it named never existed. Fixing 1 and 2 removes the input that
   produced it.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from training_sms import (  # noqa: E402
    _TAPBACK_RE, _strip_invisible, _is_withdrawn, _classify,
)


def suppressed(body: str) -> bool:
    """What handle_inbound now asks before replying to anything."""
    return bool(_TAPBACK_RE.match(_strip_invisible(body)))


# Verbatim from training_sms_messages, hair spaces (U+200A) and zero-width
# spaces (U+200B) included.
HER_TAPBACKS = [
    " ​\U0001F44D​ to “ Thanks for letting us know, Arlo, and for "
    "being so gracious about it. ” ",
    " ​\U0001F44D​ to “ Wishing you all the best, Arlo. "
    "Take care! ” ",
    " ​\U0001F44D​ to “ Take care, Arlo! \U0001F60A ” ",
    " ​\U0001F44D​ to “ You're welcome, Arlo! Wishing you all "
    "the best. ” ",
]

HER_RESIGNATION = (
    "Hi, I wanted to let you know that I have decided not to continue my employment with "
    "Example Co. I appreciate the opportunity and want to thank you for your time and "
    "consideration. I wish you and the team all the best."
)


class TestNewStyleTapbacks(unittest.TestCase):
    def test_every_reaction_from_her_thread_is_suppressed(self):
        for body in HER_TAPBACKS:
            self.assertTrue(suppressed(body), f"still replies to {body!r}")

    def test_other_reaction_emoji_are_suppressed(self):
        for emoji in ("❤️", "\U0001F44E", "\U0001F602", "‼️", "❓"):
            self.assertTrue(suppressed(f"{emoji} to “Take care!”"), emoji)

    def test_removal_form_is_suppressed(self):
        self.assertTrue(suppressed("Removed \U0001F44D from “Take care!”"))

    def test_older_ios_wording_still_works(self):
        for body in ('Liked “Take care!”', 'Loved "See you Tuesday"',
                     'Laughed at “ha”', 'Removed a like from “Hi”'):
            self.assertTrue(suppressed(body), body)


class TestRealMessagesAreNotSwallowed(unittest.TestCase):
    """Suppression is silence — a false positive here loses a real candidate."""

    def test_ordinary_replies_are_not_suppressed(self):
        for body in ("Thanks!", "Yes", "YES", "No thanks", "y",
                     "I'll be there", "Can I move it to Thursday?"):
            self.assertFalse(suppressed(body), body)

    def test_quoted_text_in_a_real_sentence_is_not_suppressed(self):
        # The shape is close to a tapback but the leading token is a word.
        for body in ('ok to “confirm”', 'I can come to “the office” right?',
                     'yes to “Monday”'):
            self.assertFalse(suppressed(body), body)

    def test_a_bare_emoji_is_not_a_tapback(self):
        # No quoted message attached — it's someone texting a thumbs-up, which
        # is a reply, not a reaction to a specific message.
        self.assertFalse(suppressed("\U0001F44D"))


class TestResignationIsAWithdrawal(unittest.TestCase):
    def test_her_message_is_recognised(self):
        self.assertTrue(_is_withdrawn(HER_RESIGNATION))

    def test_it_still_classifies_as_unknown(self):
        # Which is why the check had to be added to that branch — this is the
        # route her message actually took.
        self.assertEqual(_classify(HER_RESIGNATION), "unknown")

    def test_other_ways_people_resign(self):
        for body in (
            "I've decided not to continue with the role",
            "I won't be continuing, sorry",
            "I will not be continuing with Example Co",
            "I'm resigning effective today",
            "I need to hand in my notice",
            "I no longer wish to continue",
        ):
            self.assertTrue(_is_withdrawn(body), body)

    def test_pre_start_withdrawals_still_work(self):
        for body in ("not interested", "I found a job", "please take me off your list"):
            self.assertTrue(_is_withdrawn(body), body)

    def test_continuing_is_not_quitting(self):
        # `_apply_withdrawn` archives and rejects, so a false positive here
        # throws away a starter who was confirming.
        for body in ("I will continue with the training thanks",
                     "Can I continue tomorrow?",
                     "Yes I can make it",
                     "Looking forward to continuing!",
                     "Do I continue to the second week?"):
            self.assertFalse(_is_withdrawn(body), body)


class TestWithdrawalActsOnTheUnknownBranch(unittest.TestCase):
    def test_unknown_branch_calls_apply_withdrawn(self):
        import inspect
        import training_sms
        src = inspect.getsource(training_sms.handle_inbound)
        unknown = src.split('elif classification == "unknown"', 1)[1]
        self.assertIn("_is_withdrawn", unknown)
        self.assertIn("_apply_withdrawn", unknown)


if __name__ == "__main__":
    unittest.main()
