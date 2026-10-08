"""screening_mode must actually decide the arrival sequence.

Background: this setting shipped in the UI and was read by nothing. Selecting
"chat only" changed no behaviour whatsoever — the AI still called ten minutes
after the candidate applied. It is now the single switch that decides what
happens on arrival, so these tests exist to make sure it stays wired, and that
the two legacy spellings that older settings documents may hold keep meaning
what they always meant.

The risk being guarded against is asymmetric. A mode that wrongly *stops*
calling is a silent outage — candidates simply never hear from anyone, which is
exactly the failure that once stranded candidates. A mode that wrongly *keeps* calling
is merely the old behaviour. So every ambiguous input must resolve to
voice_first, and that is asserted rather than assumed.

Pure: no database, no HTTP, no scheduler.
"""
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# A dead address, set before importing anything that builds a Mongo client at
# import time. deps.py does exactly that, and load_dotenv() would otherwise hand
# it the production URL out of backend/.env.
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/unused")
os.environ.setdefault("DB_NAME", "test_unused")

from models import (  # noqa: E402
    ScreenCallAgentSettings,
    SCREENING_MODES,
    resolve_screening_mode,
)
from screening_start import (  # noqa: E402
    arrival_template_key,
    dial_delay_minutes,
    start_screening,
)


def settings(mode=None, **sca):
    doc = {"screen_call_agent": dict(sca)}
    if mode is not None:
        doc["screen_call_agent"]["screening_mode"] = mode
    return doc


# ── mode resolution ─────────────────────────────────────────────────────────

class TestResolveMode(unittest.TestCase):
    def test_the_three_real_modes_pass_through(self):
        for mode in SCREENING_MODES:
            self.assertEqual(resolve_screening_mode(settings(mode)), mode)

    def test_legacy_spellings_still_mean_call_first(self):
        # Both values exist in live settings docs today. Neither ever stopped a
        # call going out, and neither may start doing so now.
        self.assertEqual(resolve_screening_mode(settings("voice_only")), "voice_first")
        self.assertEqual(resolve_screening_mode(settings("voice_and_chat")), "voice_first")

    def test_the_model_default_is_the_historic_behaviour(self):
        # Turning this switch on must not change what an untouched tenant does.
        self.assertEqual(
            resolve_screening_mode(settings(ScreenCallAgentSettings().screening_mode)),
            "voice_first",
        )

    def test_anything_unreadable_falls_back_to_calling(self):
        # The safe failure is the old behaviour, never silence.
        for junk in [None, "", "  ", "CHAT-FIRST", "chatfirst", "wat", 7, [], {}]:
            self.assertEqual(resolve_screening_mode(settings(junk)), "voice_first", junk)
        self.assertEqual(resolve_screening_mode({}), "voice_first")
        self.assertEqual(resolve_screening_mode(None), "voice_first")

    def test_case_and_padding_are_tolerated_for_real_values(self):
        self.assertEqual(resolve_screening_mode(settings("  Chat_First  ")), "chat_first")

    def test_the_fallback_assertion_is_not_vacuous(self):
        # If everything resolved to voice_first the tests above would pass while
        # proving nothing. Something must actually be able to change the answer.
        self.assertNotEqual(
            resolve_screening_mode(settings("chat_first")),
            resolve_screening_mode(settings("voice_only")),
        )


# ── what the candidate is told on arrival ───────────────────────────────────

class TestArrivalTemplate(unittest.TestCase):
    def test_chat_modes_use_the_chat_template(self):
        for mode in ("chat_first", "chat_only"):
            self.assertEqual(arrival_template_key(settings(), mode), "warmup_chat_first")

    def test_voice_first_keeps_the_in_hours_out_of_hours_pair(self):
        with patch("email_service.is_in_call_window", return_value=True):
            self.assertEqual(arrival_template_key(settings(), "voice_first"), "warmup")
        with patch("email_service.is_in_call_window", return_value=False):
            self.assertEqual(arrival_template_key(settings(), "voice_first"), "warmup_offhours")

    def test_chat_modes_never_promise_a_call_in_ten_minutes(self):
        """The `warmup` copy says a call is coming in about ten minutes. Under
        chat_first that is false — the call is two hours away, conditional, and
        is only a nudge when it comes — and a first message the system won't
        honour teaches people to ignore the next one.
        """
        from models import get_default_templates

        tpl = get_default_templates()["warmup"]
        self.assertIn("[Call Delay Minutes]", tpl.body + tpl.sms_body)
        chat_tpl = get_default_templates()["warmup_chat_first"]
        self.assertNotIn("[Call Delay Minutes]", chat_tpl.body + chat_tpl.sms_body)

    def test_the_first_text_asks_a_question_rather_than_offering_a_link(self):
        """Speed to engagement. A link is a decision to make later; a question
        is answered on the spot, and answering it puts the candidate inside the
        screening without them having to choose to start one.
        """
        from models import get_default_templates

        sms = get_default_templates()["warmup_chat_first"].sms_body
        self.assertTrue(sms.rstrip().endswith("?"), sms)
        self.assertNotIn("[Retry Link]", sms)
        # The first hard gate, asked immediately.
        self.assertIn("18", sms)

    def test_the_email_carries_one_link_on_its_own_line(self):
        """The email is where someone who would rather click than type finds the
        page. One link only — the old second "pick a time" link went to the same
        page and just split the click. It sits on its own line because the HTML
        wrapper (build_email_html) strips that line and renders a branded
        "Complete screening" button in its place; plain-text clients keep the
        raw URL. The text channel is referenced but never *promised* — the same
        email goes to no-phone (email-intake) candidates."""
        from models import get_default_templates

        body = get_default_templates()["warmup_chat_first"].body
        self.assertIn("\n[Retry Link]\n", body)
        self.assertNotIn("[Pick Time Link]", body)
        self.assertIn("text", body.lower())
        self.assertLess(body.index("[Retry Link]"), body.lower().index("by text"))


# ── when the call happens ───────────────────────────────────────────────────

class TestDialDelay(unittest.TestCase):
    def test_voice_first_uses_the_warmup_delay(self):
        self.assertEqual(dial_delay_minutes(settings(warmup_delay_minutes=10), "voice_first"), 10)

    def test_chat_first_waits_for_a_reply_first(self):
        self.assertEqual(dial_delay_minutes(settings(chat_first_delay_minutes=120), "chat_first"), 120)

    def test_chat_first_delay_cannot_be_configured_to_nothing(self):
        """A 0 here would silently turn chat_first back into voice_first — the
        text and the call would land together and the whole point is lost."""
        for bad in (0, -5, None, ""):
            self.assertGreaterEqual(
                dial_delay_minutes(settings(chat_first_delay_minutes=bad), "chat_first"), 15
            )

    def test_chat_first_waits_materially_longer_than_voice_first(self):
        s = settings(warmup_delay_minutes=10, chat_first_delay_minutes=120)
        self.assertGreater(dial_delay_minutes(s, "chat_first"), dial_delay_minutes(s, "voice_first"))


# ── the orchestration ───────────────────────────────────────────────────────

class TestStartScreening(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cand = {"id": "c1", "phone": "+15551234567", "email": "a@b.com", "auto_dial": True}

    def _patches(self, sched_result=None):
        """Patch the three side-effecting collaborators at their source modules,
        since start_screening imports them lazily inside the call."""
        self.sched = AsyncMock(return_value=sched_result or {"status": "scheduled", "scheduled_at": "x"})
        self.comms = AsyncMock(return_value={"email": {}, "sms": {}})
        self.email = AsyncMock(return_value={})
        db = MagicMock()
        db.candidates.update_one = AsyncMock()
        self.db = db
        return [
            patch("auto_dialer.schedule_call_with_window", self.sched),
            patch("deps.send_stage_comms", self.comms),
            patch("deps.send_stage_email", self.email),
            patch("deps.db", db),
        ]

    async def _run(self, mode, cand=None, sched_result=None, **kw):
        for p in self._patches(sched_result):
            p.start()
        self.addCleanup(patch.stopall)
        return await start_screening("u1", cand or self.cand, settings(mode), **kw)

    async def test_voice_first_schedules_a_call_and_sends_email_only(self):
        with patch("email_service.is_in_call_window", return_value=True):
            res = await self._run("voice_first")
        self.assertEqual(res["mode"], "voice_first")
        self.sched.assert_awaited_once()
        self.assertEqual(self.sched.await_args.kwargs["base_delay_minutes"], 10)
        # In-hours voice_first deliberately withholds the SMS — the pre-call text
        # fires three minutes before the dial and two texts would read as spam.
        self.email.assert_awaited_once()
        self.comms.assert_not_awaited()

    async def test_chat_first_texts_immediately_and_delays_the_call(self):
        res = await self._run("chat_first")
        self.comms.assert_awaited_once()  # email AND sms — the text is the point
        self.assertEqual(self.comms.await_args.args[2], "warmup_chat_first")
        self.assertEqual(self.sched.await_args.kwargs["base_delay_minutes"], 120)
        self.assertIsNone(res["no_dial_reason"])

    async def test_chat_only_never_places_a_call(self):
        res = await self._run("chat_only")
        self.sched.assert_not_awaited()
        self.assertEqual(res["no_dial_reason"], "chat_only")
        self.comms.assert_awaited_once()  # they still get told they applied

    async def test_no_phone_is_recorded_as_a_reason_not_swallowed(self):
        res = await self._run("voice_first", cand={"id": "c2", "email": "a@b.com", "auto_dial": True})
        self.assertEqual(res["no_dial_reason"], "no_phone")
        self.assertEqual(
            self.db.candidates.update_one.await_args.args[1]["$set"]["screening_no_dial_reason"],
            "no_phone",
        )

    async def test_muted_duplicate_is_recorded_as_a_reason(self):
        cand = dict(self.cand, auto_dial=False)
        res = await self._run("voice_first", cand=cand)
        self.sched.assert_not_awaited()
        self.assertEqual(res["no_dial_reason"], "auto_dial_off")

    async def test_a_scheduling_failure_leaves_a_trail_instead_of_a_ghost(self):
        """The old code logged and moved on, leaving the candidate at
        screening_status='pending' with no next_call_at — a state no sweep
        queries. That is how candidates were once never contacted by anyone."""
        for p in self._patches():
            p.start()
        self.addCleanup(patch.stopall)
        self.sched.side_effect = RuntimeError("scheduler down")
        res = await start_screening("u1", self.cand, settings("voice_first"))
        self.assertEqual(res["no_dial_reason"], "error")
        stamped = self.db.candidates.update_one.await_args.args[1]["$set"]
        self.assertEqual(stamped["screening_no_dial_reason"], "error")
        self.assertIn("scheduler down", stamped["screening_start_error"])

    async def test_a_declined_schedule_is_not_reported_as_a_call(self):
        # schedule_call_with_window returns {"status": "skipped"} when the
        # dialler is off. Reporting that as scheduled would hide the outage.
        res = await self._run("voice_first", sched_result={"status": "skipped", "reason": "off"})
        self.assertEqual(res["no_dial_reason"], "dialer_disabled")

    async def test_a_successful_start_clears_any_previous_reason(self):
        res = await self._run("voice_first")
        stamped = self.db.candidates.update_one.await_args.args[1]["$set"]
        self.assertIsNone(res["no_dial_reason"])
        self.assertIsNone(stamped["screening_no_dial_reason"])
        self.assertEqual(stamped["screening_mode_used"], "voice_first")

    async def test_send_comms_false_still_schedules_the_call(self):
        # Used by the stranded-candidate sweep: they were already emailed at
        # intake, it is only the call that never happened.
        res = await self._run("voice_first", send_comms=False)
        self.comms.assert_not_awaited()
        self.email.assert_not_awaited()
        self.sched.assert_awaited_once()
        self.assertIsNone(res["no_dial_reason"])


if __name__ == "__main__":
    unittest.main()
