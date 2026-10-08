"""A starter who pulls out must come off the board by themselves.

The case behind this file: a starter who had already rescheduled once wrote
back to say, politely, that they had accepted another position and would like
to stay in touch.

The model replied perfectly ("congratulations on the new position! We wish
you all the best") but the message classified as "unknown", so nothing moved.
They stayed on the kanban and in their cohort until somebody archived them by
hand the next morning.

Two defences, because either alone has failed before:
  * the phrase list, extended to how people decline an OFFER; and
  * a [WITHDRAW] marker the model emits itself, so the component that
    understood the message is the one that acts.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import training_sms as ts


WITHDRAWAL_SMS = ("Hi, thank you for the interview and for the offer. Unfortunately I have "
                  "accepted another position, but I would like to stay in touch for future "
                  "opportunities if possible. Thanks again and have a great day.")


# ── the phrase list ─────────────────────────────────────────────────────────


def test_offer_decline_message_is_recognised_as_a_withdrawal():
    assert ts._is_withdrawn(WITHDRAWAL_SMS) is True


def test_the_ways_people_decline_an_offer():
    for msg in [
        "I've accepted another position, thank you so much",
        "I have accepted an offer elsewhere",
        "I've taken another role I'm afraid",
        "Sorry, going with another company",
        "I've decided to decline the offer",
        "Unfortunately I won't be taking the position",
        "another opportunity came up, thank you though",
    ]:
        assert ts._is_withdrawn(msg) is True, msg


def test_the_older_wordings_still_match():
    for msg in ["I got another job", "not interested", "please take me off your list",
                "I have decided not to continue my employment with Example Co",
                "I'm handing in my notice"]:
        assert ts._is_withdrawn(msg) is True, msg


def test_ordinary_replies_are_not_withdrawals():
    """A withdrawal archives someone and kills their reminders, so a false
    positive is worse than a missed one."""
    for msg in ["Yes see you Monday!", "Can't wait", "Can I start Tuesday instead?",
                "What's the address?", "Running 10 minutes late",
                "I accepted the position, thank you!",          # accepting OURS
                "Sorry I can't make today, can we move it?"]:
        assert ts._is_withdrawn(msg) is False, msg


# ── the marker ──────────────────────────────────────────────────────────────


def test_withdraw_marker_is_parsed_and_stripped():
    prose, _p, _b, cancelled, _cb, _sc, withdrawn = ts._extract_markers(
        "Congratulations on the new position, Delphine! All the best. [WITHDRAW]")
    assert withdrawn is True
    assert cancelled is False, "withdrawing is not the same as moving one appointment"
    assert "[WITHDRAW]" not in prose and "WITHDRAW" not in prose
    assert prose.startswith("Congratulations")


def test_cancel_and_withdraw_are_independent():
    _, _, _, cancelled, _, _, withdrawn = ts._extract_markers("Sorry to hear that. [CANCEL]")
    assert cancelled is True and withdrawn is False

    _, _, _, cancelled2, _, _, withdrawn2 = ts._extract_markers("All the best! [WITHDRAW]")
    assert withdrawn2 is True and cancelled2 is False


def test_a_reply_with_no_markers_is_unchanged():
    prose, proposed, book, cancelled, cb, sc, withdrawn = ts._extract_markers("See you Monday!")
    assert (prose, proposed, book, cancelled, cb, sc, withdrawn) == (
        "See you Monday!", None, None, False, False, None, False)


def test_the_model_is_told_how_to_use_the_marker():
    """The marker is worthless if the prompt never mentions it."""
    prompt = ts._llm_system_prompt(
        {"first_name": "Delphine", "stage": "APPOINTMENT"},
        {"name": "Example Co"}, intent="reschedule", enrichment={})
    assert "[WITHDRAW]" in prompt
    assert "accepted another position" in prompt.lower()


# ── a second case ───────────────────────────────────────────────────────────────
# A TRAINING starter: "I got another offer that is more what I am looking for".
# No phrase matched, and the TRAINING prompt never taught [WITHDRAW], so he
# stayed on the Monday new-starts list after the model said goodbye.

OTHER_OFFER_SMS = ("Hi this is Fenn, sorry but I got another offer that is closer to what "
                   "I am looking for. Thank you again for the offer")


def test_another_offer_message_is_recognised_as_a_withdrawal():
    assert ts._is_withdrawn(OTHER_OFFER_SMS) is True


def test_got_rather_than_accepted_an_offer():
    for msg in ["I got a better offer, sorry", "I received another offer",
                "I took another job", "I got a different job, thanks anyway"]:
        assert ts._is_withdrawn(msg) is True, msg


def test_a_starter_model_is_told_how_to_use_the_marker():
    """Withdrawals from new starters were the case the marker was built for."""
    for intent in ("unknown", "follow_up", "no"):
        prompt = ts._llm_system_prompt(
            {"first_name": "Fenn", "stage": "TRAINING"},
            {"name": "Example Co"}, intent=intent, enrichment={})
        assert "[WITHDRAW]" in prompt, intent
        assert "another offer" in prompt.lower(), intent
