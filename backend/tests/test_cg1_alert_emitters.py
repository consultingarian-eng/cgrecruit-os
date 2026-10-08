"""What pages CG1 admins — and, since 2026-08-18, what deliberately does NOT.

The owner's rule: office admins hear genuine questions/correspondence from
TRAINING-stage new hires, and nothing in the stop/withdraw family. Status
flags (withdrawals, declines, repeat reschedules, ghosted archives, the old
FORWARD_KINDS table) are silent on CG1 — the roster and the board tell that
story. These tests pin both directions: correspondence pages, status doesn't,
and the local bell's contract (test_notification_policy.py) is untouched.
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/unused")
os.environ.setdefault("DB_NAME", "test_unused")

import cg1_alerts  # noqa: E402
import notifications_service  # noqa: E402
from notifications_service import ACTIONABLE_KINDS, create_notification  # noqa: E402
from training_sms import ET, _apply_withdrawn, _within_start_watch_window  # noqa: E402


class ForwardHookTests(unittest.IsolatedAsyncioTestCase):
    """FORWARD_KINDS is empty: nothing that flows through the local bell pages
    CG1 any more, and the bell's own behavior is unchanged in both directions."""

    async def test_previously_forwarded_kinds_no_longer_page(self):
        fake_db = MagicMock()
        fake_db.notifications.insert_one = AsyncMock()
        with patch.object(notifications_service, "db", fake_db), \
             patch.object(cg1_alerts, "spawn_forward_notification") as fwd:
            # Suppressed locally AND silent on CG1 now.
            result = await create_notification(
                "u1", "start.rescheduled", "📅 Isla moved her start date",
                body="New start date: Monday, Aug 24",
                link="/inbox?candidate=c1", candidate_id="c1", pipeline_id="p1",
            )
            self.assertIsNone(result)
            fake_db.notifications.insert_one.assert_not_awaited()
            # Actionable locally, but no longer a CG1 page.
            self.assertIn("sms.replies_paused", ACTIONABLE_KINDS)
            paused = await create_notification("u1", "sms.replies_paused",
                                               "🛑 AI paused", candidate_id="c1")
            self.assertIsNotNone(paused)
            fake_db.notifications.insert_one.assert_awaited_once()
        fwd.assert_not_called()

    async def test_routine_kinds_stay_silent_everywhere(self):
        fake_db = MagicMock()
        fake_db.notifications.insert_one = AsyncMock()
        with patch.object(notifications_service, "db", fake_db), \
             patch.object(cg1_alerts, "spawn_forward_notification") as fwd:
            self.assertIsNone(await create_notification("u1", "sms.reply", "💬 hi"))
            self.assertIsNotNone(await create_notification("u1", "call.inbound", "📞"))
        fwd.assert_not_called()


class StartMorningWindowTests(unittest.TestCase):
    """The watch window: max(LEAD_HOURS, 3)h before the start to 3h after."""

    START = "2026-08-17T13:00:00"  # naive local ET, the shape the field has

    def _at(self, hour, minute=0):
        return datetime(2026, 8, 17, hour, minute, tzinfo=ET)

    def test_nicci_texting_lost_at_1230_is_in_window(self):
        self.assertTrue(_within_start_watch_window(self.START, self._at(12, 30)))

    def test_confirmation_lead_window_is_covered(self):
        # The confirmation SMS goes out ~3h ahead — replies to it count.
        self.assertTrue(_within_start_watch_window(self.START, self._at(10, 30)))

    def test_three_hours_after_still_watched(self):
        self.assertTrue(_within_start_watch_window(self.START, self._at(15, 59)))

    def test_the_night_before_is_not_start_morning(self):
        self.assertFalse(_within_start_watch_window(self.START, self._at(9, 30)))

    def test_the_afternoon_after_is_over(self):
        self.assertFalse(_within_start_watch_window(self.START, self._at(16, 1)))

    def test_no_start_date_no_window(self):
        self.assertFalse(_within_start_watch_window("", self._at(12, 30)))
        self.assertFalse(_within_start_watch_window("not-a-date", self._at(12, 30)))


class TraineeCorrespondenceWatchWiringTests(unittest.TestCase):
    """The watch pages trainee questions any time (siren in the start window,
    calm outside it) and never pages the stop/withdraw family — Isla's
    "I'm lost" still reaches a human, a STOP never does."""

    def _watch_block(self):
        import inspect
        import training_sms
        src = inspect.getsource(training_sms.handle_inbound)
        self.assertIn("Trainee-correspondence watch", src)
        return src.split("Trainee-correspondence watch", 1)[1].split("update: dict =", 1)[0]

    def test_watch_is_gated_on_training_stage_not_the_window(self):
        import inspect
        import training_sms
        src = inspect.getsource(training_sms.handle_inbound)
        gate = src.split("Trainee-correspondence watch", 1)[1]
        gate = gate.split("spawn_cg1_admin_alert", 1)[0]
        self.assertIn('stage == "TRAINING"', gate.split("if cand_id", 1)[1].split(":", 1)[0])

    def test_in_window_question_pages_the_siren_kind(self):
        self.assertIn("start_morning.reply", self._watch_block())

    def test_out_of_window_question_pages_the_calm_kind(self):
        self.assertIn("starter.message", self._watch_block())

    def test_clean_yes_pages_only_in_the_window(self):
        block = self._watch_block()
        self.assertIn("start_morning.confirmed", block)
        # The confirmed page sits under the in-window check.
        self.assertIn("_in_window", block.split("start_morning.confirmed")[0])

    def test_stop_withdraw_family_never_pages(self):
        block = self._watch_block()
        self.assertIn("_withdraw_family", block)
        self.assertIn('classification in ("no", "stop", "maybe_stop")', block)
        self.assertIn("_is_withdrawn", block)
        # The old decline page is gone for good.
        self.assertNotIn("starter.declined", block)


class WithdrawalStaysSilentTests(unittest.IsolatedAsyncioTestCase):
    """_apply_withdrawn archives and cancels jobs but never pages CG1 —
    stop/withdraw notifications are exactly what the admins asked to lose."""

    def _db(self, cand):
        db = MagicMock()
        db.candidates.find_one = AsyncMock(return_value=cand)
        db.candidates.update_one = AsyncMock()
        return db

    async def test_training_withdrawal_archives_without_paging(self):
        cand = {"id": "c1", "stage": "TRAINING", "first_name": "Arlo",
                "last_name": "Turner", "training_start_at": "2026-08-03T13:00:00"}
        db = self._db(cand)
        with patch.object(cg1_alerts, "spawn_cg1_admin_alert") as spawn:
            await _apply_withdrawn(db, "c1", "u1",
                                   "I have decided not to continue my employment")
        spawn.assert_not_called()
        db.candidates.update_one.assert_awaited_once()

    async def test_pre_training_withdrawal_is_silent_too(self):
        cand = {"id": "c2", "stage": "SCREENING", "first_name": "Jo", "last_name": "B"}
        db = self._db(cand)
        with patch.object(cg1_alerts, "spawn_cg1_admin_alert") as spawn:
            await _apply_withdrawn(db, "c2", "u1", "not interested")
        spawn.assert_not_called()
        db.candidates.update_one.assert_awaited_once()


class RescheduleStaysSilentTests(unittest.IsolatedAsyncioTestCase):
    """Reschedules — first, second, tenth — are status changes, not trainee
    correspondence: no CG1 page from any of the three reschedule paths."""

    def _db(self, cand):
        db = MagicMock()
        db.candidates.find_one = AsyncMock(return_value=cand)
        db.candidates.update_one = AsyncMock()
        return db

    async def test_repeat_reschedule_by_phone_does_not_page(self):
        from routes import public
        cand = {"id": "c1", "user_id": "u1", "stage": "TRAINING",
                "first_name": "Isla", "last_name": "M.", "phone": "+16175551234",
                "training_start_at": "2026-12-28T13:00:00",
                "sms_reschedule_count": 3}
        db = self._db(cand)
        with patch.object(public, "db", db), \
             patch.object(notifications_service, "create_notification", new=AsyncMock()), \
             patch.object(cg1_alerts, "spawn_cg1_admin_alert") as spawn:
            out = await public.public_reschedule_start_by_phone(
                {"phone": "+16175551234", "new_start_date": "2027-01-04"})
        self.assertTrue(out["ok"])
        spawn.assert_not_called()

    def test_no_reschedule_path_carries_the_repeat_kind(self):
        import inspect
        import training_sms
        self.assertNotIn("starter.rescheduled_repeat",
                         inspect.getsource(training_sms.handle_inbound))
        server_src = open(os.path.join(os.path.dirname(__file__), "..", "server.py")).read()
        self.assertNotIn("starter.rescheduled_repeat", server_src)
        public_src = open(os.path.join(os.path.dirname(__file__), "..", "routes", "public.py")).read()
        self.assertNotIn("starter.rescheduled_repeat", public_src)


class GhostedStaysSilentTests(unittest.TestCase):
    def test_lapsed_sweep_archives_without_paging(self):
        import inspect
        from dialer import training_archive_sweep
        src = inspect.getsource(training_archive_sweep.training_lapsed_sweep)
        self.assertNotIn("starter.ghosted", src)
        self.assertNotIn("spawn_cg1_admin_alert", src)
        # The archive itself survives the silence.
        self.assertIn("archived_at", src)


class TraineeEmailCorrespondenceWiringTests(unittest.TestCase):
    """A TRAINING candidate emailing a genuine question pages CG1; an emailed
    withdrawal (cancel) or bare confirm stays a silent status change."""

    def test_email_handler_pages_training_correspondence_only(self):
        import inspect
        import email_replies
        src = inspect.getsource(email_replies.handle_inbound)
        self.assertIn("starter.message", src)
        self.assertIn('== "TRAINING"', src)
        self.assertIn('action not in ("cancel", "confirm")', src)


if __name__ == "__main__":
    unittest.main()
