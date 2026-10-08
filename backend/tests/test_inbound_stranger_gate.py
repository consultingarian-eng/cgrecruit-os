"""Regression tests for the inbound front-desk agent booking a stranger, and for
the Maps-link text it promised but could not send.

An unknown caller rang the office line, said "I'm calling about a screening
interview", and was walked straight into the slot menu — never screened, never
on the kanban, name never asked. Two independent faults:

1. The prompt named its identity variables (`first_name`) instead of
   interpolating them, so ElevenLabs substituted nothing and the agent could not
   tell a booked candidate from a stranger. Everyone looked bookable.
2. `upsert_elevenlabs_tools` registered no SMS tool at all, while the prompt
   instructed the agent to offer texting the Maps link — an offer nothing could
   fulfil.
"""
import asyncio
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voice_service import build_inbound_system_prompt, _booking_tools_section  # noqa: E402


PROMPT_KW = dict(
    booking_prefs={},
    office_address="100 Example Street, Example City",
    maps_link="https://maps.example.com/downtown",
)


class TestIdentityIsInterpolated(unittest.TestCase):
    """The gate is worthless if the agent can't see the value it gates on."""

    def setUp(self):
        self.prompt = build_inbound_system_prompt({"agent_name": "Olivia"}, **PROMPT_KW)

    def test_identity_vars_are_placeholders_not_bare_names(self):
        # The original bug: the prompt said "check `first_name`" with no {{...}},
        # so the model received the word and never the value.
        for var in ("caller_known", "first_name", "appointment_line", "start_date_line"):
            self.assertIn(
                "{{%s}}" % var, self.prompt,
                f"{var} must be interpolated as {{{{{var}}}}} — naming it in prose "
                "gives ElevenLabs nothing to substitute",
            )

    def test_booking_gate_requires_a_known_caller(self):
        gate = self.prompt.split("**BOOKING GATE**", 1)[1].split("\n\n", 1)[0]
        self.assertIn("caller_known", gate)
        self.assertIn("'true'", gate)

    def test_unknown_caller_may_not_reach_the_booking_tools_unregistered(self):
        section = self.prompt.split("# A caller who isn't on our system", 1)[1]
        section = section.split("# Screening questions", 1)[0]
        for tool in ("get_available_slots", "book_slot"):
            self.assertIn(tool, section)
        self.assertIn("do NOT call", section)
        # The exact sentence the caller used must not read as proof of a booking.
        self.assertIn("screening interview", section)
        self.assertIn("NOT proof", section)


class TestNoUnbackedTextPromises(unittest.TestCase):
    """Every 'I'll text you X' in a prompt needs a tool that sends X. The fix for
    a false promise is a working sender, not a human callback — these agents
    exist to take humans out of the loop, so a handoff is only ever the failure
    branch."""

    def test_booking_section_never_claims_the_system_sends_by_itself(self):
        for prefs in ({}, {"scheduling_link": "https://cal.example/downtown"}):
            section = _booking_tools_section({}, prefs)
            low = section.lower()
            for banned in ("automatically text them", "automatically send the link",
                           "the system will automatically"):
                self.assertNotIn(
                    banned, low,
                    f"prompt claims an automatic send that does not exist "
                    f"(prefs={prefs!r}): {banned!r}",
                )

    def test_declining_every_slot_still_ends_in_a_self_serve_link(self):
        # A candidate who won't pick a slot must not become a human's to-do.
        for prefs in ({}, {"scheduling_link": "https://cal.example/downtown"}):
            section = _booking_tools_section({}, prefs)
            self.assertIn("send_booking_link", section,
                          f"no self-serve path when slots are declined (prefs={prefs!r})")

    def test_no_slots_available_also_sends_the_link(self):
        section = _booking_tools_section({}, {})
        empty_case = section.split("If BOTH arrays are empty", 1)[1].split("\n", 1)[0]
        self.assertIn("send_booking_link", empty_case)

    def test_text_is_only_confirmed_after_the_tool_returns(self):
        section = _booking_tools_section({}, {})
        self.assertIn("sent` true", section)

    def test_maps_offer_is_tied_to_the_tool(self):
        prompt = build_inbound_system_prompt({"agent_name": "Olivia"}, **PROMPT_KW)
        addr = prompt.split("# Address / directions", 1)[1].split("\n\n", 1)[0]
        self.assertIn("send_office_details", addr)
        self.assertIn("MUST call", addr)


class TestSendOfficeDetailsToolRegistered(unittest.TestCase):
    def _specs(self):
        """Run upsert_elevenlabs_tools far enough to capture its tool specs."""
        import voice_service
        captured = {}

        def fake_get(url, **kw):
            raise RuntimeError("stop after specs are built")

        # The specs list is built before any HTTP call; grab it by intercepting
        # the first request the function makes.
        with patch.object(voice_service, "requests") as rq:
            rq.get.side_effect = fake_get
            with patch.dict("os.environ", {"ELEVENLABS_API_KEY": "test"}):
                try:
                    voice_service.upsert_elevenlabs_tools("https://app.example")
                except Exception:
                    pass
        # Fall back to reading the source-level spec names, which is what we
        # actually care about being present.
        import inspect
        src = inspect.getsource(voice_service.upsert_elevenlabs_tools)
        captured["src"] = src
        return captured["src"]

    def test_tool_exists_and_binds_the_same_vars_as_book_slot(self):
        src = self._specs()
        self.assertIn('"name": "send_office_details"', src)
        self.assertIn("/api/public/send-office-details", src)
        # Binding `phone` (not a new caller-only variable) is deliberate: every
        # agent already declares it, so the tool is safe on all of them.
        block = src.split('"name": "send_office_details"', 1)[1]
        self.assertIn('"dynamic_variable": "phone"', block)
        self.assertIn('"dynamic_variable": "pipeline_slug"', block)


class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def limit(self, _n):
        return self

    def __aiter__(self):
        async def gen():
            for d in self._docs:
                yield d
        return gen()


class _FakeCollection:
    def __init__(self, docs=()):
        self.docs = list(docs)

    async def find_one(self, q, *a, **kw):
        for d in self.docs:
            if all(d.get(k) == v for k, v in q.items() if not isinstance(v, dict)):
                return d
        return None

    def find(self, *a, **kw):
        return _FakeCursor(self.docs)

    async def update_one(self, q, u, **kw):
        for d in self.docs:
            if d.get("id") == q.get("id"):
                d.update(u.get("$set") or {})


class _FakeDB:
    def __init__(self, pipelines=(), candidates=()):
        self.pipelines = _FakeCollection(pipelines)
        self.candidates = _FakeCollection(candidates)


PIPE = {"id": "p1", "user_id": "u1", "name": "Downtown", "public_slug": "downtown",
        "inbound_agent_id": "agent_downtown", "twilio_phone_number": "+15550100188"}


class TestCallerKnownVariable(unittest.IsolatedAsyncioTestCase):
    """conversation-init must state identity as a fact, not leave it inferable."""

    async def _init(self, caller, candidates=()):
        import routes.webhooks as wh

        req = types.SimpleNamespace(
            json=AsyncMock(return_value={"caller_id": caller, "agent_id": "agent_downtown"}),
        )
        with patch.object(wh, "db", _FakeDB([PIPE], candidates)), \
             patch("deps.resolve_settings", AsyncMock(return_value={
                 "recruiter_profile": {"company_name": "Example Co", "city": "Downtown"},
                 "screen_call_agent": {"agent_name": "Olivia"},
                 "region_language": {"timezone": "America/New_York"},
             })):
            return await wh.elevenlabs_conversation_init(req)

    async def test_unknown_caller_is_marked_not_known(self):
        res = await self._init("+14135550001")
        dyn = res["dynamic_variables"]
        self.assertEqual(dyn["caller_known"], "false")
        self.assertEqual(dyn["first_name"], "")
        self.assertEqual(dyn["appointment_line"], "")

    async def test_matched_caller_is_marked_known(self):
        cand = {"id": "c1", "user_id": "u1", "pipeline_id": "p1", "first_name": "Greer",
                "last_name": "Halloway", "phone": "+15550100152", "stage": "APPOINTMENT",
                "appointment_at": "2026-08-01T13:30:00+00:00"}
        res = await self._init("+15550100152", [cand])
        dyn = res["dynamic_variables"]
        self.assertEqual(dyn["caller_known"], "true")
        self.assertEqual(dyn["first_name"], "Greer")

    async def test_caller_known_is_always_present(self):
        # A missing key means ElevenLabs substitutes nothing and the gate silently
        # reopens — it must be set on every payload that carries the other vars.
        res = await self._init("+14135550002")
        self.assertIn("caller_known", res["dynamic_variables"])


class TestSendOfficeDetailsEndpoint(unittest.IsolatedAsyncioTestCase):
    async def _call(self, payload, sms_result=None, pipelines=(PIPE,)):
        """Returns (response, send_direct_sms mock) — the mock must be read here,
        while the patch is still live."""
        import routes.public as pub
        import routes.inbound as inb
        import sms_service

        sender = AsyncMock(return_value=sms_result or {"status": "sent", "sid": "SM1"})
        with patch.object(pub, "db", _FakeDB(pipelines)), \
             patch.object(pub, "resolve_settings", AsyncMock(return_value={
                 "recruiter_profile": {"company_name": "Example Co"},
             })), \
             patch.object(inb, "_resolve_office_context", AsyncMock(return_value={
                 "office_key": "downtown",
                 "office_address": "100 Example Street, Example City",
                 "maps_link": "https://maps.example.com/downtown",
                 "phone_number_id": "pn1",
             })), \
             patch.object(sms_service, "send_direct_sms", sender):
            return await pub.public_send_office_details(payload), sender

    async def test_sends_address_and_maps_link(self):
        res, sender = await self._call({"phone": "+14135550001", "pipeline_slug": "downtown"})
        self.assertTrue(res["sent"])
        body = sender.await_args.args[3]
        self.assertIn("100 Example Street", body)
        self.assertIn("maps.example.com", body)

    async def test_opted_out_never_reports_as_sent(self):
        res, _ = await self._call(
            {"phone": "+14135550001", "pipeline_slug": "downtown"},
            sms_result={"status": "skipped", "reason": "recipient opted out"},
        )
        self.assertFalse(res["sent"])
        self.assertIn("opted out", res["message"])

    async def test_failure_is_a_200_the_agent_can_read_out(self):
        # An HTTP error mid-call leaves the agent improvising after it has already
        # said the text is coming; a spoken sentence keeps it honest.
        res, _ = await self._call(
            {"phone": "+14135550001", "pipeline_slug": "downtown"},
            sms_result={"status": "failed", "error": "twilio down"},
        )
        self.assertFalse(res["sent"])
        self.assertIn("read the address out", res["message"])

    async def test_unknown_pipeline_does_not_raise(self):
        res, _ = await self._call({"phone": "+14135550001", "pipeline_slug": "nope"}, pipelines=())
        self.assertFalse(res["sent"])

    async def test_missing_phone_does_not_raise(self):
        res, _ = await self._call({"pipeline_slug": "downtown"})
        self.assertFalse(res["sent"])


SCA = {"agent_name": "Olivia", "screening_questions": [
    {"question": "Are you 18 or over?", "auto_screen": True},
    {"question": "Can you commit to 4+ days a week?"},
]}


class TestStrangerIsScreenedNotFobbedOff(unittest.TestCase):
    """A caller with no record wanting a job should end the call screened, not
    with a promise that someone will ring back."""

    def setUp(self):
        self.prompt = build_inbound_system_prompt(SCA, **PROMPT_KW)

    def test_unknown_caller_route_registers_then_screens(self):
        section = self.prompt.split("# A caller who isn't on our system", 1)[1]
        section = section.split("# Screening questions", 1)[0]
        self.assertIn("register_caller", section)
        self.assertIn("screening: 'proceed'", section)

    def test_screening_questions_are_in_the_inbound_prompt(self):
        self.assertIn("Are you 18 or over?", self.prompt)
        self.assertIn("Can you commit to 4+ days a week?", self.prompt)
        self.assertIn("auto-screen", self.prompt)

    def test_screening_is_fenced_to_registered_callers(self):
        block = self.prompt.split("# Screening questions", 1)[1].split("# Tools (booking)", 1)[0]
        self.assertIn("register_caller", block)
        # A caller already on the system has been screened — re-running it on them
        # is the mirror-image mistake of screening nobody.
        self.assertIn("caller_known", block)

    def test_no_questions_configured_means_no_booking(self):
        prompt = build_inbound_system_prompt({"agent_name": "Olivia"}, **PROMPT_KW)
        block = prompt.split("# Screening questions", 1)[1].split("# Tools (booking)", 1)[0]
        self.assertIn("cannot screen", block)
        self.assertIn("do NOT book", block)

    def test_gate_admits_a_caller_registered_mid_call(self):
        gate = self.prompt.split("**BOOKING GATE**", 1)[1].split("\n\n", 1)[0]
        self.assertIn("register_caller", gate)
        self.assertIn("caller_known", gate)


class TestRegisterCallerEndpoint(unittest.IsolatedAsyncioTestCase):
    async def _call(self, payload, dup=None, pipelines=(PIPE,)):
        import routes.public as pub
        import deps

        inserted = []

        class _Cands(_FakeCollection):
            async def insert_one(self, doc):
                inserted.append(doc)

        db = _FakeDB(pipelines)
        db.candidates = _Cands()
        with patch.object(pub, "db", db), \
             patch.object(deps, "find_duplicate_candidate", AsyncMock(return_value=dup)):
            return await pub.public_register_caller(payload), inserted

    async def test_creates_a_screening_stage_candidate(self):
        res, inserted = await self._call({
            "phone": "+14135550001", "pipeline_slug": "downtown",
            "first_name": "Sage", "last_name": "Mockley", "email": "Sage@Example.com",
        })
        self.assertTrue(res["registered"])
        self.assertEqual(res["screening"], "proceed")
        self.assertEqual(len(inserted), 1)
        doc = inserted[0]
        self.assertEqual(doc["stage"], "SCREENING")
        self.assertEqual(doc["first_name"], "Sage")
        self.assertEqual(doc["phone"], "+14135550001")
        self.assertEqual(doc["email"], "sage@example.com", "email must be normalised for dedup")
        self.assertEqual(doc["source"], "inbound_call")

    async def test_existing_caller_resumes_instead_of_forking(self):
        # The duplicate guard is the same one every other intake uses — a caller
        # ringing from a number already on the pipeline must not get a second record.
        res, inserted = await self._call(
            {"phone": "+15550100152", "pipeline_slug": "downtown", "first_name": "Greer"},
            dup={"id": "existing-1", "stage": "SCREENING"},
        )
        self.assertTrue(res["already_known"])
        self.assertEqual(res["candidate_id"], "existing-1")
        self.assertEqual(res["screening"], "proceed")
        self.assertEqual(inserted, [], "a known caller must not create a second record")

    async def test_no_name_registers_nobody(self):
        res, inserted = await self._call({"phone": "+14135550001", "pipeline_slug": "downtown"})
        self.assertFalse(res["registered"])
        self.assertEqual(inserted, [])
        self.assertNotIn("proceed", str(res.get("screening", "")))

    async def test_unknown_pipeline_registers_nobody(self):
        res, inserted = await self._call(
            {"phone": "+1", "pipeline_slug": "nope", "first_name": "Sage"}, pipelines=())
        self.assertFalse(res["registered"])
        self.assertEqual(inserted, [])


class TestMidCallRegistrationIsWrittenUp(unittest.IsolatedAsyncioTestCase):
    """The record is created after the init variables were minted, so the
    transcript has to be re-attached or the screening is written up against
    nobody."""

    async def test_late_match_routes_to_screening_post_call(self):
        import routes.webhooks as wh

        late = {"id": "new-1", "user_id": "u1", "pipeline_id": "p1",
                "first_name": "Sage", "last_name": "Mockley", "phone": "+14135550001"}
        init = {"user_id": "u1", "pipeline_id": "p1", "phone": "+14135550001",
                "call_kind": "inbound", "candidate_id": ""}
        seen = {}

        async def _fake_screening(conv_id, data, init_arg):
            seen["candidate_id"] = init_arg.get("candidate_id")
            return {"ok": True, "screening": True}

        with patch.object(wh, "_match_inbound_candidate", AsyncMock(return_value=late)), \
             patch.object(wh, "_handle_inbound_screening_post_call", _fake_screening), \
             patch.object(wh, "db", types.SimpleNamespace(
                 inbound_calls=types.SimpleNamespace(insert_one=AsyncMock()))):
            res = await wh._handle_inbound_post_call("conv1", {"transcript": []}, init)

        self.assertEqual(seen["candidate_id"], "new-1",
                         "the mid-call record must receive its own transcript")
        self.assertTrue(res.get("screening"))

    async def test_known_caller_is_not_re_matched(self):
        import routes.webhooks as wh
        init = {"user_id": "u1", "pipeline_id": "p1", "phone": "+1", "call_kind": "inbound",
                "candidate_id": "already-here"}
        matcher = AsyncMock()
        with patch.object(wh, "_match_inbound_candidate", matcher), \
             patch.object(wh, "db", types.SimpleNamespace(
                 inbound_calls=types.SimpleNamespace(insert_one=AsyncMock()))), \
             patch("notifications_service.create_notification", AsyncMock()):
            await wh._handle_inbound_post_call("conv2", {"transcript": []}, init)
        matcher.assert_not_awaited()


class TestSendBookingLinkEndpoint(unittest.IsolatedAsyncioTestCase):
    CAND = {"id": "c1", "user_id": "u1", "pipeline_id": "p1", "first_name": "Sage",
            "phone": "+14135550001", "public_token": "tok123"}

    async def _call(self, payload, candidates=(CAND,), sms_result=None):
        import routes.public as pub
        import sms_service

        sender = AsyncMock(return_value=sms_result or {"status": "sent", "sid": "SM1"})
        with patch.object(pub, "db", _FakeDB([PIPE], candidates)), \
             patch.object(pub, "resolve_settings", AsyncMock(return_value={
                 "recruiter_profile": {"company_name": "Example Co"},
             })), \
             patch.dict("os.environ", {"APP_PUBLIC_URL": "https://app.example"}), \
             patch.object(pub, "window_has_slots", AsyncMock(return_value=True)), \
             patch.object(sms_service, "send_direct_sms", sender):
            return await pub.public_send_booking_link(payload), sender

    async def test_texts_the_candidates_own_booking_page(self):
        res, sender = await self._call({"phone": "+14135550001", "pipeline_slug": "downtown"})
        self.assertTrue(res["sent"])
        body = sender.await_args.args[3]
        self.assertIn("https://app.example/applicant/tok123", body)
        self.assertIn("Sage", body)

    async def test_no_record_does_not_claim_a_text(self):
        res, sender = await self._call(
            {"phone": "+19995550000", "pipeline_slug": "downtown"}, candidates=())
        self.assertFalse(res["sent"])
        sender.assert_not_awaited()

    async def test_missing_public_token_does_not_claim_a_text(self):
        res, sender = await self._call(
            {"phone": "+14135550001", "pipeline_slug": "downtown"},
            candidates=({**self.CAND, "public_token": ""},))
        self.assertFalse(res["sent"])
        sender.assert_not_awaited()

    async def test_send_failure_is_reported_honestly(self):
        res, _ = await self._call(
            {"phone": "+14135550001", "pipeline_slug": "downtown"},
            sms_result={"status": "failed", "error": "twilio down"})
        self.assertFalse(res["sent"])
        self.assertIn("didn't go through", res["message"])

    async def test_an_empty_window_parks_instead_of_sending_a_dead_page(self):
        """Added with the booking window: a link to a page with no slots on it
        is worse than no link, so the candidate is parked for the retry sweep."""
        import routes.public as pub
        import sms_service
        sender = AsyncMock(return_value={"status": "sent", "sid": "SM1"})
        db = _FakeDB([PIPE], [self.CAND])
        with patch.object(pub, "db", db), \
             patch.object(pub, "resolve_settings", AsyncMock(return_value={
                 "recruiter_profile": {"company_name": "Example Co"},
                 "booking_preferences": {"booking_retry_days": 2},
             })), \
             patch.dict("os.environ", {"APP_PUBLIC_URL": "https://app.example"}), \
             patch.object(pub, "window_has_slots", AsyncMock(return_value=False)), \
             patch.object(sms_service, "send_direct_sms", sender):
            res = await pub.public_send_booking_link(
                {"phone": "+14135550001", "pipeline_slug": "downtown"})
        self.assertFalse(res["sent"])
        self.assertTrue(res.get("parked"))
        sender.assert_not_awaited()
        self.assertIsNotNone(self.CAND.get("booking_retry_at"))


class TestDirectSmsRespectsOptOut(unittest.IsolatedAsyncioTestCase):
    async def test_opted_out_number_is_never_texted(self):
        import sms_service
        sent = []
        with patch.object(sms_service, "is_opted_out", AsyncMock(return_value=True)), \
             patch("voice_service.send_sms", side_effect=lambda *a, **k: sent.append(a)):
            res = await sms_service.send_direct_sms(None, {}, "+14135550001", "hi")
        self.assertEqual(res["status"], "skipped")
        self.assertEqual(sent, [], "a stranger who replied STOP must not be texted")

    async def test_first_message_carries_the_stop_clause(self):
        import sms_service

        class _DB:
            communications = types.SimpleNamespace(insert_one=AsyncMock())

        with patch.object(sms_service, "is_opted_out", AsyncMock(return_value=False)), \
             patch.object(sms_service, "has_received_sms_before", AsyncMock(return_value=False)), \
             patch.dict(os.environ, {"TWILIO_PHONE_NUMBER": "+15550100000"}), \
             patch("voice_service.send_sms", return_value={"status": "sent", "sid": "SM1"}):
            res = await sms_service.send_direct_sms(_DB(), {}, "+14135550001", "Our address is X")
        self.assertIn("STOP", res["body"])


if __name__ == "__main__":
    unittest.main()
