"""cg1_alerts transport — office resolution, the webhook contract, and the
never-raise / never-block guarantees.

A starter once texted that she was lost on the way in; the AI answered with
the address and no human was ever pinged. Every emitter
now routes through this module, so its transport rules are load-bearing:
right URL, right header, right payload shape, and a dead CG1 must cost a log
line, never a broken webhook.
"""
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/unused")
os.environ.setdefault("DB_NAME", "test_unused")
# Frozen company profile: offices "downtown" (Downtown) and "riverside" (Riverside).
os.environ["COMPANY_PROFILE_PATH"] = os.path.join(os.path.dirname(__file__), "company_profile.test.json")

import cg1_alerts  # noqa: E402
from cg1_alerts import (  # noqa: E402
    FORWARD_KINDS, office_label, resolve_office_key, send_cg1_admin_alert,
)


class ResolveOfficeKeyTests(unittest.TestCase):
    def test_explicit_cg1_office_key_wins(self):
        self.assertEqual(resolve_office_key({"cg1_office_key": "riverside",
                                             "public_slug": "acme-downtown"}), "riverside")

    def test_slug_sniff_downtown(self):
        self.assertEqual(resolve_office_key({"public_slug": "acme-downtown"}), "downtown")

    def test_name_sniff_riverside(self):
        self.assertEqual(resolve_office_key({"name": "Riverside Office"}), "riverside")

    def test_unmatched_pipeline_is_empty(self):
        self.assertEqual(resolve_office_key({"name": "Springfield"}), "")

    def test_none_pipeline_is_empty(self):
        self.assertEqual(resolve_office_key(None), "")
        self.assertEqual(resolve_office_key({}), "")

    def test_office_labels(self):
        self.assertEqual(office_label("downtown"), "Downtown")
        self.assertEqual(office_label("riverside"), "Riverside")
        self.assertEqual(office_label(""), "unknown office")


class ForwardKindsTests(unittest.TestCase):
    def test_no_bell_kind_forwards_to_cg1(self):
        # Owner's rule (2026-08-18): CG1 admins hear ONLY genuine trainee
        # questions/correspondence, paged directly from the inbound handlers.
        # Nothing that flows through the local bell — cancellations,
        # reschedules, pauses — forwards any more.
        self.assertEqual(FORWARD_KINDS, frozenset())


class _FakeResponse:
    def __init__(self, fail=False):
        self._fail = fail

    def raise_for_status(self):
        if self._fail:
            raise RuntimeError("boom 500")


class _FakeClient:
    """Records the POST; stands in for httpx.AsyncClient. No network."""
    calls: list = []
    fail = False
    raise_on_post = False

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        if _FakeClient.raise_on_post:
            raise ConnectionError("cg1 is down")
        _FakeClient.calls.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(fail=_FakeClient.fail)


ENV = {"CG1_BACKEND_URL": "https://cg1.example.com",
       "CG1_WEBHOOK_SECRET": "s3cret",
       "APP_PUBLIC_URL": "https://cgr.example.com"}


class SendAdminAlertTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _FakeClient.calls = []
        _FakeClient.fail = False
        _FakeClient.raise_on_post = False

    async def test_contract_payload_and_header(self):
        with patch.dict(os.environ, ENV), \
             patch.object(cg1_alerts.httpx, "AsyncClient", _FakeClient):
            ok = await send_cg1_admin_alert(
                "start_morning.reply", "🚨 Start-morning text — Isla M.",
                '"I\'m lost" — starts 1:00 PM today (Downtown)',
                candidate={"id": "c1", "first_name": "Isla", "last_name": "M."},
                pipeline={"cg1_office_key": "downtown"},
            )
        self.assertTrue(ok)
        self.assertEqual(len(_FakeClient.calls), 1)
        call = _FakeClient.calls[0]
        self.assertEqual(call["url"], "https://cg1.example.com/api/webhooks/cgrecruit/admin-alert")
        self.assertEqual(call["headers"], {"x-webhook-secret": "s3cret"})
        payload = call["json"]
        self.assertEqual(payload["kind"], "start_morning.reply")
        self.assertEqual(payload["office_key"], "downtown")
        self.assertEqual(payload["candidate_id"], "c1")
        self.assertEqual(payload["candidate_name"], "Isla M.")
        self.assertEqual(payload["link"], "https://cgr.example.com/?candidate=c1")
        self.assertIn("occurred_at", payload)
        # The full contract shape — CG1 validates against these exact keys.
        self.assertEqual(set(payload), {"kind", "title", "body", "office_key",
                                        "candidate_id", "candidate_name",
                                        "link", "occurred_at"})

    async def test_office_key_param_beats_pipeline(self):
        with patch.dict(os.environ, ENV), \
             patch.object(cg1_alerts.httpx, "AsyncClient", _FakeClient):
            await send_cg1_admin_alert("k", "t", "b",
                                       pipeline={"cg1_office_key": "downtown"},
                                       office_key="riverside")
        self.assertEqual(_FakeClient.calls[0]["json"]["office_key"], "riverside")

    async def test_candidate_pipeline_lookup_fallback(self):
        fake_db = MagicMock()
        fake_db.pipelines.find_one = AsyncMock(return_value={"public_slug": "acme-riverside"})
        with patch.dict(os.environ, ENV), \
             patch.object(cg1_alerts.httpx, "AsyncClient", _FakeClient), \
             patch("deps.db", fake_db):
            await send_cg1_admin_alert("k", "t", "b",
                                       candidate={"id": "c1", "pipeline_id": "p1"})
        self.assertEqual(_FakeClient.calls[0]["json"]["office_key"], "riverside")

    async def test_env_unset_is_a_noop(self):
        with patch.dict(os.environ, {**ENV, "CG1_BACKEND_URL": ""}), \
             patch.object(cg1_alerts.httpx, "AsyncClient", _FakeClient):
            ok = await send_cg1_admin_alert("k", "t", "b")
        self.assertFalse(ok)
        self.assertEqual(_FakeClient.calls, [])

    async def test_dead_cg1_never_raises(self):
        _FakeClient.raise_on_post = True
        with patch.dict(os.environ, ENV), \
             patch.object(cg1_alerts.httpx, "AsyncClient", _FakeClient):
            ok = await send_cg1_admin_alert("k", "t", "b")
        self.assertFalse(ok)

    async def test_http_error_never_raises(self):
        _FakeClient.fail = True
        with patch.dict(os.environ, ENV), \
             patch.object(cg1_alerts.httpx, "AsyncClient", _FakeClient):
            ok = await send_cg1_admin_alert("k", "t", "b")
        self.assertFalse(ok)

    async def test_explicit_link_passes_through(self):
        with patch.dict(os.environ, ENV), \
             patch.object(cg1_alerts.httpx, "AsyncClient", _FakeClient):
            await send_cg1_admin_alert("k", "t", "b", office_key="downtown",
                                       link="https://cgr.example.com/inbox?candidate=c9")
        self.assertEqual(_FakeClient.calls[0]["json"]["link"],
                         "https://cgr.example.com/inbox?candidate=c9")


class SpawnTests(unittest.IsolatedAsyncioTestCase):
    async def test_spawn_fires_send_without_blocking(self):
        import asyncio
        with patch.object(cg1_alerts, "send_cg1_admin_alert", new=AsyncMock()) as sent:
            cg1_alerts.spawn_cg1_admin_alert("k", "t", "b", office_key="downtown")
            await asyncio.sleep(0)  # let the task run
        sent.assert_awaited_once_with("k", "t", "b", office_key="downtown")

    def test_spawn_without_a_loop_never_raises(self):
        # Sync/offline contexts (scripts, teardown) just drop the alert.
        cg1_alerts.spawn_cg1_admin_alert("k", "t", "b")


if __name__ == "__main__":
    unittest.main()
