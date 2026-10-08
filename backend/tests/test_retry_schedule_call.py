"""Tests for the candidate-chosen callback time feature (moment-of-apply
CTAs: chat now / instant callback / pick a time).

Covers:
  - `_compute_call_time_options` — clamps preset labels into the pipeline's
    call window and de-dupes collisions.
  - `POST /public/retry/{token}/schedule-call` — reschedules the pending call
    via `schedule_call_with_window(force=True)`, which (per the duplicate-SMS
    fix in test_duplicate_sms_fix.py) already dedupes any stale call/sms jobs
    before adding the new one — this test just confirms the endpoint wires
    the candidate's chosen time through correctly and rejects bad input.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
import routes.retry as retry_routes  # noqa: E402
from routes.retry import _compute_call_time_options, schedule_call_at_time  # noqa: E402


class ComputeCallTimeOptionsTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "auto_dialer": {
                "call_window_start": "09:00",
                "call_window_end": "19:00",
                "call_window_days": [0, 1, 2, 3, 4, 5, 6],
            },
            "region_language": {"timezone": "UTC"},
        }

    def _fixed_now(self, dt):
        return patch.object(retry_routes, "now_utc", return_value=dt)

    def test_labels_present_and_future(self):
        # Tuesday 10am UTC — comfortably inside the window.
        fixed = datetime(2026, 3, 3, 10, 0, tzinfo=timezone.utc)
        with self._fixed_now(fixed):
            options = _compute_call_time_options(self.settings)
        labels = [o["label"] for o in options]
        self.assertIn("Later today", labels)
        self.assertIn("Tomorrow morning", labels)
        for opt in options:
            iso = datetime.fromisoformat(opt["iso"])
            self.assertGreater(iso, fixed, f"{opt['label']} must be in the future")

    def test_options_clamped_into_window(self):
        # 6am UTC — before the 9am window opens.
        fixed = datetime(2026, 3, 3, 6, 0, tzinfo=timezone.utc)
        with self._fixed_now(fixed):
            options = _compute_call_time_options(self.settings)
        for opt in options:
            iso = datetime.fromisoformat(opt["iso"])
            self.assertGreaterEqual(iso.hour, 9)
            self.assertLessEqual(iso.hour, 19)

    def test_no_duplicate_isos(self):
        fixed = datetime(2026, 3, 3, 10, 0, tzinfo=timezone.utc)
        with self._fixed_now(fixed):
            options = _compute_call_time_options(self.settings)
        isos = [o["iso"] for o in options]
        self.assertEqual(len(isos), len(set(isos)))


class ScheduleCallEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cand = {
            "id": "cand-1", "user_id": "user-1", "pipeline_id": "pipe-1",
            "phone": "+15551234567", "public_token": "tok-1",
            "screening_status": "queued",
        }
        self.db = MagicMock()
        self.db.candidates.find_one = AsyncMock(return_value=self.cand)
        self.patched_db = patch.object(retry_routes, "db", self.db)
        self.patched_db.start()

    async def asyncTearDown(self):
        self.patched_db.stop()

    async def test_rejects_missing_run_at(self):
        with self.assertRaises(HTTPException) as ctx:
            await schedule_call_at_time("tok-1", {})
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_rejects_too_soon(self):
        run_at = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        with self.assertRaises(HTTPException) as ctx:
            await schedule_call_at_time("tok-1", {"run_at": run_at})
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_rejects_too_far_out(self):
        run_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        with self.assertRaises(HTTPException) as ctx:
            await schedule_call_at_time("tok-1", {"run_at": run_at})
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_rejects_unknown_token(self):
        self.db.candidates.find_one = AsyncMock(return_value=None)
        run_at = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        with self.assertRaises(HTTPException) as ctx:
            await schedule_call_at_time("bad-tok", {"run_at": run_at})
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_rejects_completed_screening(self):
        self.cand["screening_status"] = "approved"
        run_at = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        with self.assertRaises(HTTPException) as ctx:
            await schedule_call_at_time("tok-1", {"run_at": run_at})
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_valid_request_reschedules_via_schedule_call_with_window(self):
        run_at = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
        fake_result = {"status": "scheduled", "scheduled_at": run_at}
        with patch("dialer.queue.schedule_call_with_window", new=AsyncMock(return_value=fake_result)) as mocked:
            res = await schedule_call_at_time("tok-1", {"run_at": run_at})
        self.assertTrue(res["ok"])
        mocked.assert_awaited_once()
        _, kwargs = mocked.call_args
        self.assertEqual(mocked.call_args.args[0], "user-1")
        self.assertEqual(mocked.call_args.args[1], "cand-1")
        self.assertTrue(kwargs["force"])
        self.assertAlmostEqual(kwargs["base_delay_minutes"], 300, delta=1)


if __name__ == "__main__":
    unittest.main()
