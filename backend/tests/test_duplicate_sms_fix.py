"""Regression tests for the v34.7 duplicate-SMS bug.

Symptom (user-reported): when a candidate is queued for a screening call
twice in quick succession (e.g. apply portal queues a call, then recruiter
drags them to SCREENING), TWO warmup SMS were being delivered.

Root cause: `dialer.queue.schedule_call_with_window` adds a job with
`f"sms:{user}:{candidate}:{ts}"`. APScheduler's `replace_existing=True` only
matches identical job IDs — the timestamp suffix made each scheduling produce
a DIFFERENT job ID, leaving the previous SMS (and call) jobs alive in the
queue. Both fired → duplicate SMS.

Fix: `schedule_call_with_window` now calls `cancel_pending_call_jobs` first
to wipe ALL pending `call:*` and `sms:*` jobs for this candidate before
scheduling new ones. Also fixes the latent `cancel_pending_retry_calls`
bug where the prefix match (`call:{candidate_id}`) never matched real job
IDs (`call:{user}:{candidate}:{ts}`).
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dialer import state  # noqa: E402
from dialer.queue import schedule_call_with_window  # noqa: E402
from dialer.retry import cancel_pending_call_jobs, cancel_pending_retry_calls  # noqa: E402


class FakeJob:
    def __init__(self, job_id):
        self.id = job_id


class FakeScheduler:
    def __init__(self):
        self.jobs = {}

    def add_job(self, fn, trigger, run_date=None, args=None, id=None, replace_existing=False, misfire_grace_time=None):
        if id in self.jobs and not replace_existing:
            raise RuntimeError(f"job {id} already exists")
        self.jobs[id] = (fn, run_date, args)

    def get_jobs(self):
        return [FakeJob(j) for j in self.jobs.keys()]

    def remove_job(self, job_id):
        self.jobs.pop(job_id, None)


class DuplicateSMSFixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scheduler = FakeScheduler()
        self.db = MagicMock()
        # Settings allow dialing, default windows.
        self.settings = {
            "auto_dialer": {
                "enabled": True,
                "call_window_start": "00:00",
                "call_window_end": "23:59",
                "call_window_days": [0, 1, 2, 3, 4, 5, 6],
                "pre_call_sms_enabled": True,
                "pre_call_sms_minutes": 3,
            },
            "region_language": {"timezone": "UTC"},
        }
        self.db.candidates.find_one = AsyncMock(return_value={
            "id": "cand-1", "user_id": "user-1", "phone": "+15551234567",
            "auto_dial": True, "pipeline_id": "pipe-1",
        })
        self.db.candidates.update_one = AsyncMock()
        self.db.settings.find_one = AsyncMock(return_value=self.settings)
        # state singletons.
        state._scheduler = self.scheduler
        state._db = self.db

    async def asyncTearDown(self):
        state._scheduler = None
        state._db = None

    async def _patched_settings_for(self, *_, **__):
        return self.settings

    async def test_cancel_prefix_bug_now_matches_real_job_ids(self):
        """The original `cancel_pending_retry_calls` prefix `call:{candidate}` 
        never matched real job IDs `call:{user}:{candidate}:{ts}`.  After the 
        fix, both call: AND sms: jobs for this candidate are removed."""
        # Seed the scheduler with realistic job IDs (call + sms + an unrelated
        # call for a different candidate).
        self.scheduler.jobs = {
            "call:user-1:cand-1:1700000000": (None, None, None),
            "sms:user-1:cand-1:1699999800": (None, None, None),
            "call:user-1:cand-2:1700000005": (None, None, None),  # other candidate
            "appt-reminder-1h-email:cand-1": (None, None, None),  # unrelated
        }
        removed = cancel_pending_retry_calls("cand-1")
        self.assertEqual(removed, 2)
        self.assertNotIn("call:user-1:cand-1:1700000000", self.scheduler.jobs)
        self.assertNotIn("sms:user-1:cand-1:1699999800", self.scheduler.jobs)
        # other candidate untouched, unrelated reminder untouched
        self.assertIn("call:user-1:cand-2:1700000005", self.scheduler.jobs)
        self.assertIn("appt-reminder-1h-email:cand-1", self.scheduler.jobs)

    def test_cancel_pending_call_jobs_does_not_clear_next_call_at(self):
        """`cancel_pending_call_jobs` (used by re-scheduling) MUST leave the
        candidate doc alone — the caller is about to set a new next_call_at."""
        self.scheduler.jobs = {
            "call:user-1:cand-1:1700000000": (None, None, None),
            "sms:user-1:cand-1:1699999800": (None, None, None),
        }
        removed = cancel_pending_call_jobs("cand-1")
        self.assertEqual(removed, 2)
        # update_one should NOT have been called for `next_call_at` clearing.
        self.db.candidates.update_one.assert_not_called()

    async def test_rescheduling_does_not_duplicate_sms_jobs(self):
        """Calling schedule_call_with_window twice in a row leaves only ONE
        `call:` and ONE `sms:` job — the older ones are cancelled before the
        new ones are added."""
        # Patch settings_for to use our injected settings dict. It has to be
        # the name `queue` bound at import — patching `dialer.state` leaves the
        # real resolver in place, and this test then dials whatever database
        # MONGO_URL happens to point at.
        from dialer import queue as _state
        original = _state.settings_for
        _state.settings_for = self._patched_settings_for
        try:
            res1 = await schedule_call_with_window("user-1", "cand-1", base_delay_minutes=5, force=True)
            self.assertEqual(res1["status"], "scheduled")
            # After first scheduling: 1 call + 1 sms.
            call_jobs_1 = [j for j in self.scheduler.jobs if j.startswith("call:user-1:cand-1:")]
            sms_jobs_1 = [j for j in self.scheduler.jobs if j.startswith("sms:user-1:cand-1:")]
            self.assertEqual(len(call_jobs_1), 1)
            self.assertEqual(len(sms_jobs_1), 1)

            # Tick the clock by simulating a small wait — generates a different
            # ts suffix.  (Realistic: recruiter drags 1s after apply.)
            import time
            time.sleep(1.1)
            res2 = await schedule_call_with_window("user-1", "cand-1", base_delay_minutes=5, force=True)
            self.assertEqual(res2["status"], "scheduled")

            # After second scheduling: STILL only 1 call + 1 sms (old ones wiped).
            call_jobs_2 = [j for j in self.scheduler.jobs if j.startswith("call:user-1:cand-1:")]
            sms_jobs_2 = [j for j in self.scheduler.jobs if j.startswith("sms:user-1:cand-1:")]
            self.assertEqual(len(call_jobs_2), 1, f"expected 1 call job, found {call_jobs_2}")
            self.assertEqual(len(sms_jobs_2), 1, f"expected 1 sms job, found {sms_jobs_2}")
        finally:
            _state.settings_for = original


if __name__ == "__main__":
    unittest.main()
