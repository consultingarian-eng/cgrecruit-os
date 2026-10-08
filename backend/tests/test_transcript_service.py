"""One readable transcript out of three incompatible stores.

A candidate's words live in `conversations.transcript` (phone), `chat_log` (web
chat) and `training_sms_messages` (SMS) — different collections, different field
names, and different ways of saying who spoke: `role: applicant` here,
`direction: in` there. Only the phone one was ever rendered in the ATS, so a
recruiter could listen back to a call but had literally no way to read a chat.

Tolerable while the phone was the real screening. Now that most candidates are
screened by text, "the recruiter cannot read the screening" is the product.

The tests care about two things: that a turn is attributed to the right person
whichever store it came from, and that the merged order is the order things
actually happened.

Pure: no database.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/unused")
os.environ.setdefault("DB_NAME", "test_unused")

from transcript_service import (  # noqa: E402
    from_chat_log,
    from_conversations,
    from_sms_messages,
    merge,
    summarise,
)

CHAT = [
    {"role": "agent", "channel": "system", "text": "Resume received via email", "at": "2026-08-01T09:00:00Z"},
    {"role": "agent", "channel": "retry", "text": "Are you 18 or over?", "at": "2026-08-01T09:05:00Z"},
    {"role": "applicant", "channel": "retry", "text": "yes", "at": "2026-08-01T09:06:00Z"},
]
SMS = [
    {"direction": "out", "body": "Can you commute?", "timestamp": "2026-08-01T09:10:00Z", "was_llm_reply": True},
    {"direction": "in", "body": "yes I can", "timestamp": "2026-08-01T09:12:00Z"},
]
CALLS = [{
    "id": "conv1", "created_at": "2026-08-01T11:00:00Z", "status": "completed",
    "duration_seconds": 46,
    "transcript": [{"role": "agent", "text": "Hi there"}, {"role": "user", "text": "not a good time"}],
}]


class TestWhoSaidWhat(unittest.TestCase):
    """Each store spells the speaker differently. Getting this wrong would put
    the candidate's words in the assistant's mouth."""

    def test_the_chat_roles(self):
        turns = [t for t in from_chat_log(CHAT) if t["channel"] == "chat"]
        self.assertEqual([t["who"] for t in turns], ["assistant", "candidate"])

    def test_the_sms_directions(self):
        self.assertEqual([t["who"] for t in from_sms_messages(SMS)], ["assistant", "candidate"])

    def test_the_call_roles_including_the_user_spelling(self):
        spoken = [t for t in from_conversations(CALLS) if t["who"] != "system"]
        self.assertEqual([t["who"] for t in spoken], ["assistant", "candidate"])

    def test_every_candidate_spelling_is_recognised(self):
        for spelling in ("applicant", "candidate", "user", "human", "APPLICANT"):
            turn = from_chat_log([{"role": spelling, "channel": "retry", "text": "x"}])[0]
            self.assertEqual(turn["who"], "candidate", spelling)

    def test_the_attribution_is_not_uniform(self):
        # If everything were labelled "assistant" the tests above would still
        # look plausible read individually.
        whos = {t["who"] for t in merge(CHAT, SMS, CALLS)}
        self.assertEqual(whos, {"assistant", "candidate", "system"})


class TestChannelLabelling(unittest.TestCase):
    def test_each_turn_knows_where_it_happened(self):
        by_channel = {}
        for t in merge(CHAT, SMS, CALLS):
            by_channel.setdefault(t["channel"], 0)
            by_channel[t["channel"]] += 1
        self.assertEqual(by_channel, {"system": 1, "chat": 2, "sms": 2, "call": 3})

    def test_sms_written_into_chat_log_is_still_labelled_sms(self):
        """The chat screening writes SMS turns into chat_log with channel=sms.
        Labelling those as web chat would mislead about how a candidate engaged."""
        turn = from_chat_log([{"role": "applicant", "channel": "sms", "text": "yes"}])[0]
        self.assertEqual(turn["channel"], "sms")

    def test_system_notes_are_set_apart_not_shown_as_the_assistant_talking(self):
        """"Resume received via email" is written with role=agent but was never
        said to anybody."""
        note = from_chat_log(CHAT)[0]
        self.assertEqual(note["who"], "system")
        self.assertEqual(note["channel"], "system")

    def test_a_call_is_listed_even_when_nobody_spoke(self):
        """Most calls never connect. "We rang and got nothing" is information."""
        turns = from_conversations([{"id": "c", "created_at": "2026-08-01T11:00:00Z",
                                     "status": "no_answer", "transcript": []}])
        self.assertEqual(len(turns), 1)
        self.assertIn("no_answer", turns[0]["text"])


class TestOrdering(unittest.TestCase):
    def test_the_merged_order_is_the_order_things_happened(self):
        texts = [t["text"] for t in merge(CHAT, SMS, CALLS)]
        self.assertEqual(texts.index("Are you 18 or over?") + 1, texts.index("yes"))
        self.assertLess(texts.index("yes I can"), texts.index("Hi there"))

    def test_turns_inside_a_call_keep_their_spoken_order(self):
        """They share the call's start time — a plain timestamp sort would
        scramble a conversation into an arbitrary order."""
        spoken = [t["text"] for t in merge(None, None, CALLS) if t["who"] != "system"]
        self.assertEqual(spoken, ["Hi there", "not a good time"])

    def test_an_undated_entry_sorts_first_not_last(self):
        """Almost always an early system note. Sorting it last would put "Resume
        received" after the interview was booked."""
        turns = merge([{"role": "agent", "channel": "system", "text": "no date"}] + CHAT, None, None)
        self.assertEqual(turns[0]["text"], "no date")

    def test_empty_turns_are_dropped(self):
        turns = merge([{"role": "applicant", "channel": "retry", "text": "   "}], None, None)
        self.assertEqual(turns, [])


class TestSummary(unittest.TestCase):
    def test_it_counts_replies_and_channels(self):
        s = summarise(merge(CHAT, SMS, CALLS))
        self.assertEqual(s["candidate_replies"], 3)
        self.assertEqual(s["channels_used"], ["call", "chat", "sms"])

    def test_system_is_not_offered_as_a_channel_the_candidate_used(self):
        s = summarise(merge(CHAT, None, None))
        self.assertNotIn("system", s["channels_used"])

    def test_nothing_at_all_is_handled(self):
        self.assertEqual(merge(None, None, None), [])
        self.assertEqual(summarise([])["turn_count"], 0)

    def test_channels_sort_by_actual_time_not_raw_offset_strings(self):
        """SMS rows are stamped Eastern (-04:00), chat turns UTC. Raw ISO string
        comparison put a 9:16pm ET text before a 10pm UTC chat message that
        actually happened hours earlier — scrambling any candidate who switched
        channels mid-screening. Timestamps must normalise to UTC first."""
        chat = [{"role": "applicant", "channel": "retry", "text": "web says hi",
                 "at": "2026-07-28T22:00:00+00:00"}]          # 22:00 UTC = 6pm ET
        sms = [{"direction": "in", "body": "text came later",
                "timestamp": "2026-07-28T21:16:05-04:00"}]     # 9:16pm ET = 01:16 UTC next day
        turns = merge(chat, sms, None)
        self.assertEqual([t["text"] for t in turns], ["web says hi", "text came later"])

    def test_the_screening_thread_merges_both_channels_in_order(self):
        """A candidate three gates in over SMS who taps into the web chat must
        land mid-conversation — the page used to load only web-chat turns and
        greet them from scratch with question 1."""
        from transcript_service import screening_thread
        chat = [{"role": "agent", "channel": "retry", "text": "welcome back",
                 "at": "2026-07-29T11:35:00+00:00"}]
        sms = [
            {"direction": "out", "body": "are you 18 or over?", "timestamp": "2026-07-29T07:30:00-04:00"},
            {"direction": "in", "body": "Yes", "timestamp": "2026-07-29T07:31:00-04:00"},
        ]
        thread = screening_thread(chat, sms)
        self.assertEqual([m["text"] for m in thread], ["are you 18 or over?", "Yes", "welcome back"])
        self.assertEqual([m["channel"] for m in thread], ["sms", "sms", "retry"])
        self.assertEqual(thread[1]["role"], "applicant")

    def test_the_summary_never_carries_a_key_that_shadows_the_transcript(self):
        """The endpoint returns {"turns": <list>, **summarise(...)}. When the
        summary also had a "turns" key (the count), the unpack replaced the
        whole transcript with an integer and the drawer showed "Nothing said
        yet" for a 21-turn screening — found on the first live night."""
        self.assertNotIn("turns", summarise(merge(CHAT, SMS, CALLS)))


if __name__ == "__main__":
    unittest.main()
