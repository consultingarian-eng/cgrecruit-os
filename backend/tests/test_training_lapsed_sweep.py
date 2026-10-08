"""Training-lapsed archive sweep — decision logic + unarchive-on-contact.

    DB_NAME=cgrecruit_local_test backend/.venv/bin/python -m pytest backend/tests/test_training_lapsed_sweep.py
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dialer.training_archive_sweep import (  # noqa: E402
    should_archive_training,
    unarchive_on_contact,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _cand(**over):
    base = {
        "id": "c1",
        "stage": "TRAINING",
        "archived_at": None,
        # Naive local (ET) ISO — the shape training_start_at actually has.
        "training_start_at": "2026-08-03T13:00:00",  # 5 days before NOW
    }
    base.update(over)
    return base


class ShouldArchiveTests(unittest.TestCase):
    def test_five_days_past_archives(self):
        self.assertTrue(should_archive_training(_cand(), NOW))

    def test_two_days_past_waits(self):
        self.assertFalse(should_archive_training(_cand(training_start_at="2026-08-06T13:00:00"), NOW))

    def test_exactly_inside_grace_waits(self):
        # Aug 5 + 4 days = Aug 9 ET > NOW — still inside the window.
        self.assertFalse(should_archive_training(_cand(training_start_at="2026-08-05T13:00:00"), NOW))

    def test_future_training_untouched(self):
        self.assertFalse(should_archive_training(_cand(training_start_at="2026-08-11T13:00:00"), NOW))

    def test_no_date_is_visible_work(self):
        self.assertFalse(should_archive_training(_cand(training_start_at=None), NOW))
        self.assertFalse(should_archive_training(_cand(training_start_at=""), NOW))

    def test_fresh_contact_hold_blocks(self):
        hold = (NOW + timedelta(days=2)).isoformat()
        self.assertFalse(should_archive_training(_cand(training_lapse_hold_until=hold), NOW))

    def test_expired_hold_archives(self):
        hold = (NOW - timedelta(hours=1)).isoformat()
        self.assertTrue(should_archive_training(_cand(training_lapse_hold_until=hold), NOW))

    def test_pending_future_reschedule_blocks(self):
        self.assertFalse(should_archive_training(
            _cand(sms_proposed_reschedule_date="2026-08-10T11:00:00"), NOW))

    def test_stale_reschedule_proposal_does_not_block(self):
        self.assertTrue(should_archive_training(
            _cand(sms_proposed_reschedule_date="2026-08-01T11:00:00"), NOW))

    def test_other_stages_untouched(self):
        self.assertFalse(should_archive_training(_cand(stage="APPOINTMENT"), NOW))

    def test_already_archived_untouched(self):
        self.assertFalse(should_archive_training(_cand(archived_at="2026-08-07T00:00:00+00:00"), NOW))


class UnarchiveOnContactTests(unittest.IsolatedAsyncioTestCase):
    async def test_training_lapsed_comes_back(self):
        db = AsyncMock()
        cand = _cand(archived_at="2026-08-08T00:00:00+00:00", archived_reason="training_lapsed")
        changed = await unarchive_on_contact(db, cand)
        self.assertTrue(changed)
        db.candidates.update_one.assert_awaited_once()
        _, update = db.candidates.update_one.await_args.args
        self.assertIsNone(update["$set"]["archived_at"])
        self.assertIsNone(update["$set"]["archived_reason"])
        self.assertTrue(update["$set"]["training_lapse_hold_until"])
        # Caller keeps working with live state — the dict is mutated in place.
        self.assertIsNone(cand["archived_at"])
        self.assertTrue(cand["training_lapse_hold_until"])

    async def test_rejected_stays_archived(self):
        db = AsyncMock()
        cand = _cand(archived_at="2026-08-08T00:00:00+00:00", archived_reason="rejected")
        self.assertFalse(await unarchive_on_contact(db, cand))
        db.candidates.update_one.assert_not_awaited()

    async def test_withdrawn_stays_archived(self):
        db = AsyncMock()
        cand = _cand(archived_at="2026-08-08T00:00:00+00:00", archived_reason="withdrawn")
        self.assertFalse(await unarchive_on_contact(db, cand))
        db.candidates.update_one.assert_not_awaited()

    async def test_merged_duplicate_stays_archived(self):
        db = AsyncMock()
        cand = _cand(archived_at="2026-08-08T00:00:00+00:00", archived_reason="merged_into:abc123")
        self.assertFalse(await unarchive_on_contact(db, cand))
        db.candidates.update_one.assert_not_awaited()

    async def test_hand_archived_training_no_show_comes_back(self):
        # The 2026-08-10 walk-ins: bulk-archived by hand on Aug 5 (reason
        # "manual"), one rebooked himself by text and stayed invisible.
        db = AsyncMock()
        cand = _cand(archived_at="2026-08-05T10:31:01+00:00", archived_reason="manual")
        self.assertTrue(await unarchive_on_contact(db, cand))
        db.candidates.update_one.assert_awaited_once()
        self.assertIsNone(cand["archived_at"])
        self.assertTrue(cand["training_lapse_hold_until"])

    async def test_custom_reason_training_no_show_comes_back(self):
        db = AsyncMock()
        cand = _cand(archived_at="2026-08-05T10:31:01+00:00", archived_reason="no show cleanup")
        self.assertTrue(await unarchive_on_contact(db, cand))

    async def test_hand_archived_outside_training_stays_archived(self):
        # Manual restore is a TRAINING-only courtesy: a SCREENING candidate
        # archived as max_attempts_no_contact goes through revival, not this.
        db = AsyncMock()
        cand = _cand(stage="SCREENING", archived_at="2026-07-18T08:10:44+00:00",
                     archived_reason="max_attempts_no_contact")
        self.assertFalse(await unarchive_on_contact(db, cand))
        db.candidates.update_one.assert_not_awaited()

    async def test_training_lapsed_comes_back_regardless_of_stage(self):
        db = AsyncMock()
        cand = _cand(stage="APPOINTMENT", archived_at="2026-08-08T00:00:00+00:00",
                     archived_reason="training_lapsed")
        self.assertTrue(await unarchive_on_contact(db, cand))

    async def test_unarchived_candidate_is_noop(self):
        db = AsyncMock()
        self.assertFalse(await unarchive_on_contact(db, _cand()))
        db.candidates.update_one.assert_not_awaited()


class ConfirmationTickArchiveFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_tick_query_excludes_archived(self):
        # A starter archived for days was still texted "see you at
        # 1:00 PM today" by the tick. The query itself must exclude archived —
        # a rebooking no-show is unarchived on their first inbound text, so
        # they still get their confirmation.
        from unittest.mock import MagicMock
        from training_sms import scheduler_tick

        class _EmptyCursor:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        db = MagicMock()
        db.candidates.find = MagicMock(return_value=_EmptyCursor())
        await scheduler_tick(db)
        query = db.candidates.find.call_args.args[0]
        self.assertIn({"archived_at": None}, query.get("$or", []))


if __name__ == "__main__":
    unittest.main()
