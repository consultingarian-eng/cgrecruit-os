"""Regression test for the 2026-07-13 voicemail-detection misfire.

Symptom (user-reported): Downtown candidates couldn't book on the screening
call — the agent said "let me grab the next available times", then abruptly
spoke the VOICEMAIL message mid-conversation and hung up. The post-call
classifiers saw termination_reason=voicemail, voided the whole conversation
(no_answer, verdict discarded) and DND-redialled the live human 3 minutes
later.

Root cause: ElevenLabs' voicemail_detection system tool can misfire on the
silent pause while get_available_slots loads. All three post-call paths
(webhook, auto-sync sweep, manual Sync) honoured the voicemail flag blindly.

Fix: call_classification.resolve_voicemail vetoes the voicemail flag when
the transcript shows a live back-and-forth (2+ real, non-greeting user turns).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from call_classification import (  # noqa: E402
    count_real_user_turns,
    resolve_voicemail,
    transcript_mentions_voicemail,
)


def _t(role, text):
    return {"role": role, "text": text}


class TestVoicemailMisfire(unittest.TestCase):
    def test_true_voicemail_greeting_is_flagged(self):
        transcript = [
            _t("agent", "Hi, this is Olivia from Example Co..."),
            _t("user", "You've reached Dan. Please leave a message after the tone."),
        ]
        vm, misfired = resolve_voicemail(False, transcript)
        self.assertTrue(vm)
        self.assertFalse(misfired)

    def test_platform_flag_honoured_when_nobody_engaged(self):
        transcript = [_t("agent", "Hi, this is Olivia from Example Co...")]
        vm, misfired = resolve_voicemail(True, transcript)
        self.assertTrue(vm)
        self.assertFalse(misfired)

    def test_misfire_on_live_conversation_is_vetoed(self):
        # Toby's 7/13 call: two real user turns, then voicemail_detection
        # fired mid-booking and terminated the call.
        transcript = [
            _t("agent", "Great, do you have the time for a quick three-minute chat?"),
            _t("user", "We already did that three-minute chat."),
            _t("agent", "Hey Toby, thanks for jumping back in!"),
            _t("user", "We already did that. I do not have time at this very moment."),
            _t("agent", "I'm so sorry about that, Toby! Let me just grab the next available times..."),
        ]
        vm, misfired = resolve_voicemail(True, transcript)
        self.assertFalse(vm)
        self.assertTrue(misfired)

    def test_single_hello_does_not_veto_the_flag(self):
        # One short turn is consistent with a greeting caught before the
        # machine took over — keep trusting the platform flag.
        transcript = [_t("agent", "Hi, this is Olivia..."), _t("user", "Hello?")]
        vm, misfired = resolve_voicemail(True, transcript)
        self.assertTrue(vm)
        self.assertFalse(misfired)

    def test_vm_greeting_turns_are_not_real_engagement(self):
        transcript = [
            _t("user", "The person you are calling is not available."),
            _t("user", "At the tone, please record your message."),
        ]
        self.assertEqual(count_real_user_turns(transcript), 0)
        self.assertTrue(transcript_mentions_voicemail(transcript))

    def test_empty_transcript(self):
        vm, misfired = resolve_voicemail(False, [])
        self.assertFalse(vm)
        self.assertFalse(misfired)


if __name__ == "__main__":
    unittest.main()
