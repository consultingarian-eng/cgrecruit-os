"""Hard-gate rejections stayed in the Screening column.

Thirteen candidates across both offices carried the red "Rejected" pill and were
still sitting in Screening — five who answered no to the 18+ gate, eight who
could not work 10:30–19:30. Their rejection emails had already gone out.

The two screening channels disagreed. The voice path archives on any
disqualification ("failed a hard gate — archive immediately"). The text path
archived `withdrawn` alone, so the other four gates — age, work authorisation,
schedule, commute — left the candidate live and rejected forever.

The one case that must NOT archive is a candidate who already holds a booking and
then fails the gate-check: booking wins, and a human decides what to do with the
slot.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import screening_outcome  # noqa: E402

GATES = ["age", "work_authorization", "schedule_availability", "commute"]


class TestTextPathArchivesEveryGate(unittest.TestCase):
    """Read from source: conclude_text_screening needs a DB, an LLM summary and a
    notification stack to run, and the branch logic is what regressed."""

    def setUp(self):
        import inspect
        self.src = inspect.getsource(screening_outcome.conclude_text_screening)
        self.branch = self.src.split("elif disq_label:", 1)[1].split("elif complete:", 1)[0]

    def test_archive_is_not_conditional_on_withdrawn(self):
        # The regression: `if raw_disq == "withdrawn":` gating the archive.
        self.assertNotIn('if raw_disq == "withdrawn":', self.branch,
                         "archiving must not be limited to withdrawals")

    def test_the_branch_archives(self):
        self.assertIn('update["archived_at"]', self.branch)
        self.assertIn('update["archived_reason"]', self.branch)

    def test_reason_still_distinguishes_withdrawn_from_rejected(self):
        self.assertIn('"withdrawn" if raw_disq == "withdrawn" else "rejected"', self.branch)

    def test_the_dialler_is_still_stood_down(self):
        self.assertIn('update["auto_dial"] = False', self.branch)
        self.assertIn('update["next_call_at"] = None', self.branch)

    def test_a_booked_candidate_is_not_archived_by_the_gate_check(self):
        """Booking wins — the archive lives in the `elif`, not the booked branch."""
        booked = self.src.split("if booked:", 1)[1].split("elif disq_label:", 1)[0]
        self.assertNotIn('update["archived_at"]', booked)
        # It clears disq_label so the rejection branch cannot be reached at all.
        self.assertIn("disq_label = None", booked)


class TestVoicePathStampsAReason(unittest.TestCase):
    def test_voice_rejections_name_themselves(self):
        # The twiml path now files outcomes through the shared ladder
        # (call_classification.screening_status_after_call); the guarantee is
        # unchanged — a rejected voice screening archives itself WITH a reason.
        import inspect
        import server
        src = inspect.getsource(server)
        block = src.split("from call_classification import screening_status_after_call", 1)[1][:1200]
        self.assertIn('if _status == "rejected":', block)
        self.assertIn('cand_set["archived_at"]', block)
        self.assertIn('cand_set["archived_reason"]', block)


class TestBothChannelsAgree(unittest.TestCase):
    def test_neither_channel_leaves_a_gate_failure_live(self):
        """The asymmetry itself is the bug — assert it is gone."""
        import inspect
        import server
        text = inspect.getsource(screening_outcome.conclude_text_screening)
        text_branch = text.split("elif disq_label:", 1)[1].split("elif complete:", 1)[0]
        self.assertIn('update["screening_status"] = "rejected"', text_branch)
        self.assertIn('update["archived_at"]', text_branch)
        # Voice: rejection + archive both flow from the shared ladder now.
        voice = inspect.getsource(server).split(
            "from call_classification import screening_status_after_call", 1)[1][:1200]
        self.assertIn('if _status == "rejected":', voice)
        self.assertIn('cand_set["archived_at"]', voice)

    def test_every_post_call_processor_uses_the_shared_ladder(self):
        """Four processors once had four ladders — a borderline candidate's
        message depended on which one won the race. Pin them all to the one
        definition."""
        import inspect
        import server
        from routes import webhooks
        from dialer import scheduler, follow_up
        for name, mod in (("webhook", webhooks), ("sweep", scheduler),
                          ("twiml", server), ("followup", follow_up)):
            self.assertIn("screening_status_after_call", inspect.getsource(mod), name)


class TestGateVocabularyIsUnchanged(unittest.TestCase):
    def test_the_reasons_the_summariser_may_emit(self):
        """Archiving now applies to all of these, so pin the list — a new gate
        added upstream should be a deliberate choice here too."""
        import ai_service
        import inspect
        src = inspect.getsource(ai_service)
        for gate in GATES + ["withdrawn"]:
            self.assertIn(f'"{gate}"', src, f"{gate} missing from the summariser contract")


if __name__ == "__main__":
    unittest.main()
