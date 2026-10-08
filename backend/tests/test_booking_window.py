"""The booking window, and what happens when it holds nothing.

`appointments.booking_days_offered` shipped in the settings model and the
settings UI, and was read by nothing — `/public/availability/{slug}` asked for 60
days regardless. An office had it set to 4 and was offering slots three weeks out.
The design keeps bookings close: a slot many days out is easier to forget.

A flat calendar cap is uneven, though: Downtown runs Tue–Sat, so a candidate
screened on Friday would see Saturday and then two dead days, and the window
would shut. Each dead day inside the window pushes its far edge out by one.

Capping creates a case that did not exist before — wanting to book on a day when
everything near is full. Those candidates are parked rather than booked badly,
and the sweep texts them their booking page once the window contains a slot.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from availability_service import (  # noqa: E402
    effective_window_days, MAX_WINDOW_EXTENSION_DAYS,
)

# `dialer/__init__.py` re-exports the sweep function under the same name as its
# module, so `dialer.booking_retry_sweep` resolves to the function and patching
# it as a module fails. Same shape as gate_check_sweep / session_confirm_sweep;
# reach the module through sys.modules, as test_retry_chat_nudge_sweep does.
import dialer  # noqa: E402,F401
sweep_mod = sys.modules["dialer.booking_retry_sweep"]

ET = ZoneInfo("America/New_York")

# A sample office week: Tue, Wed (x2), Thu, Fri, Sat. No Sun/Mon.
DOWNTOWN = {"id": "p1", "availability_rules": [
    {"weekday": 1, "start": "09:15", "end": "09:45", "slot_minutes": 30, "capacity": 25},
    {"weekday": 2, "start": "09:15", "end": "09:45", "slot_minutes": 30, "capacity": 20},
    {"weekday": 2, "start": "14:00", "end": "14:30", "slot_minutes": 30, "capacity": 20},
    {"weekday": 3, "start": "14:00", "end": "14:30", "slot_minutes": 30, "capacity": 20},
    {"weekday": 4, "start": "09:15", "end": "09:45", "slot_minutes": 30, "capacity": 25},
    {"weekday": 5, "start": "13:00", "end": "13:30", "slot_minutes": 30, "capacity": 20},
]}

# Aug 2026: 4th is a Tuesday.
TUE, WED, THU, FRI, SAT, SUN, MON = (datetime(2026, 8, d, 10, 0, tzinfo=ET)
                                     for d in (4, 5, 6, 7, 8, 9, 10))


class TestEffectiveWindowDays(unittest.TestCase):
    def test_midweek_window_is_not_extended(self):
        # Tue -> Wed, Thu, Fri all run interviews. Nothing to absorb.
        self.assertEqual(effective_window_days(DOWNTOWN, 3, TUE), 3)
        self.assertEqual(effective_window_days(DOWNTOWN, 3, WED), 3)

    def test_weekend_is_absorbed_so_the_window_still_reaches_sessions(self):
        # Thu/Fri/Sat run into the dead Sun+Mon, so each gains two days.
        for day in (THU, FRI, SAT):
            self.assertEqual(effective_window_days(DOWNTOWN, 3, day), 5,
                             f"{day:%a} should stretch past the dead Sun/Mon")

    def test_a_friday_screen_still_reaches_three_session_days(self):
        """The case the extension exists for."""
        window = effective_window_days(DOWNTOWN, 3, FRI)
        reachable = {(FRI + timedelta(days=i)).weekday()
                     for i in range(window + 1)}
        runs = {int(r["weekday"]) for r in DOWNTOWN["availability_rules"]}
        # Fri(4), Sat(5), Tue(1), Wed(2) — comfortably more than one chance.
        self.assertGreaterEqual(len(reachable & runs), 3)

    def test_extension_is_bounded(self):
        only_tuesday = {"availability_rules": [{"weekday": 1, "start": "09:15"}]}
        self.assertLessEqual(
            effective_window_days(only_tuesday, 3, WED), 3 + MAX_WINDOW_EXTENSION_DAYS)

    def test_no_rules_returns_the_base_untouched(self):
        # Nothing to reason about — don't loop looking for a day that never comes.
        self.assertEqual(effective_window_days({"availability_rules": []}, 3, TUE), 3)
        self.assertEqual(effective_window_days({}, 3, TUE), 3)

    def test_zero_and_negative_are_safe(self):
        self.assertGreaterEqual(effective_window_days(DOWNTOWN, 0, TUE), 0)
        self.assertGreaterEqual(effective_window_days(DOWNTOWN, -5, TUE), 0)


class _Cur:
    def __init__(self, docs): self._d = docs
    def sort(self, *a, **k): return self
    def limit(self, n): self._d = self._d[:n]; return self
    async def to_list(self, n): return self._d[:n]


class _Coll:
    def __init__(self, docs=()): self.docs = list(docs); self.updates = []
    def find(self, *a, **k): return _Cur(list(self.docs))
    async def find_one(self, q, *a, **k):
        for d in self.docs:
            if all(d.get(kk) == vv for kk, vv in q.items() if not isinstance(vv, dict)):
                return d
        return None
    async def update_one(self, q, u, **k):
        self.updates.append((q, u))
        for d in self.docs:
            if d.get("id") == q.get("id"):
                d.update(u.get("$set") or {})


class _DB:
    def __init__(self, candidates=(), pipelines=()):
        self.candidates = _Coll(candidates)
        self.pipelines = _Coll(pipelines)


SETTINGS = {
    "appointments": {"booking_days_offered": 3, "applicant_limit": 20},
    "region_language": {"timezone": "America/New_York"},
    "booking_preferences": {"booking_retry_days": 2, "booking_retry_max_attempts": 3},
    "recruiter_profile": {"company_name": "Example Co"},
}


def _cand(**kw):
    base = {"id": "c1", "user_id": "u1", "pipeline_id": "p1", "first_name": "Sage",
            "phone": "+14135550001", "public_token": "tok", "appointment_at": None,
            "archived_at": None, "stage": "SCREENING", "booking_retry_attempts": 0,
            "booking_retry_sent_at": None,
            "booking_retry_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}
    base.update(kw)
    return base


class TestBookingRetrySweep(unittest.IsolatedAsyncioTestCase):
    async def _run(self, cand, has_slots, send_result=None):
        brs = sweep_mod
        from dialer import state
        db = _DB([cand], [DOWNTOWN])
        sender = AsyncMock(return_value=send_result or {"status": "sent", "sid": "SM1"})
        with patch.object(state, "_db", db), \
             patch.object(brs, "settings_for", AsyncMock(return_value=SETTINGS)), \
             patch("routes.public.window_has_slots", AsyncMock(return_value=has_slots)), \
             patch("sms_service.send_direct_sms", sender), \
             patch.dict("os.environ", {"APP_PUBLIC_URL": "https://app.example"}):
            res = await brs.booking_retry_sweep()
        return res, db, sender

    async def test_sends_the_link_once_the_window_has_a_slot(self):
        c = _cand()
        res, db, sender = await self._run(c, has_slots=True)
        self.assertEqual(res["sent"], 1)
        body = sender.await_args.args[3]
        self.assertIn("https://app.example/applicant/tok", body)
        self.assertIsNotNone(c["booking_retry_sent_at"])
        self.assertIsNone(c["booking_retry_at"], "a sent retry must not fire again")

    async def test_rolls_forward_rather_than_sending_a_dead_page(self):
        c = _cand()
        res, db, sender = await self._run(c, has_slots=False)
        self.assertEqual(res["sent"], 0)
        self.assertEqual(res["rolled"], 1)
        sender.assert_not_awaited()
        self.assertIsNone(c["booking_retry_sent_at"])
        self.assertEqual(c["booking_retry_attempts"], 1)
        self.assertGreater(c["booking_retry_at"], datetime.now(timezone.utc).isoformat())

    async def test_gives_up_instead_of_rolling_forever(self):
        c = _cand(booking_retry_attempts=2)   # max_attempts is 3
        res, db, sender = await self._run(c, has_slots=False)
        self.assertEqual(res["dropped"], 1)
        self.assertIsNone(c["booking_retry_at"],
                          "must stop rolling so the candidate surfaces as human work")

    async def test_a_failed_send_is_not_retried_forever(self):
        c = _cand()
        res, db, _ = await self._run(
            c, has_slots=True, send_result={"status": "skipped", "reason": "recipient opted out"})
        self.assertEqual(res["dropped"], 1)
        self.assertIsNone(c["booking_retry_at"])

    async def test_candidates_who_booked_meanwhile_are_not_picked_up(self):
        # The query excludes them; assert the filter is actually on the query so a
        # refactor can't quietly start texting booked people.
        import inspect
        src = inspect.getsource(sweep_mod.booking_retry_sweep)
        self.assertIn('"appointment_at": None', src)
        self.assertIn('"archived_at": None', src)
        self.assertIn('"booking_retry_sent_at": None', src)


class TestAvailabilityEndpointHonoursTheWindow(unittest.TestCase):
    def test_endpoint_reads_booking_days_offered(self):
        import inspect
        from routes.public import public_availability
        src = inspect.getsource(public_availability)
        self.assertIn("booking_days_offered", src)
        self.assertIn("effective_window_days", src)

    def test_recruiter_and_internal_doors_stay_uncapped(self):
        """Capping is about what candidates may pick, not what a recruiter may set."""
        import inspect
        import server
        from routes import internal
        for fn in (server.admin_pipeline_slots, internal.internal_pipeline_slots):
            self.assertNotIn("booking_days_offered", inspect.getsource(fn))


if __name__ == "__main__":
    unittest.main()
