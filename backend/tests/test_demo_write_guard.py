"""Unit tests for the demo-account write guard (demo_guard.py).

A user doc with is_demo: true is a walk-around trial account: full visibility
for its role/tenant, but no mutation may reach production data. The guard is a
pure ASGI middleware wrapped around the whole app, so no endpoint can forget
it; these tests drive it with hand-built ASGI scopes and a stubbed users
collection.
"""
import asyncio
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/x")
os.environ.setdefault("DB_NAME", "x")
os.environ.setdefault("JWT_SECRET", "unit-test-secret-with-32-plus-chars!!")

import demo_guard  # noqa: E402
from auth_service import create_token  # noqa: E402


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, n):
        return self._docs[:n]


class _Users:
    def __init__(self, docs):
        self._docs = docs

    def find(self, *args, **kwargs):
        return _Cursor(self._docs)


class _Db:
    def __init__(self, demo_docs):
        self.users = _Users(demo_docs)


def _scope(method, path, token=None, extra_headers=()):
    headers = list(extra_headers)
    if token:
        headers.append((b"authorization", b"Bearer " + token.encode()))
    return {
        "type": "http", "method": method, "path": path, "headers": headers,
        "query_string": b"", "scheme": "http", "server": ("test", 80),
    }


def _arm(demo_ids):
    demo_guard.db = _Db([{"id": i} for i in demo_ids])
    demo_guard._demo_ids_cache["ids"] = frozenset()
    demo_guard._demo_ids_cache["at"] = 0.0


class DemoWriteGuardTests(unittest.TestCase):
    def setUp(self):
        _arm(["demo-1"])
        self.demo_token = create_token("demo-1", "demo@example.com")
        self.real_token = create_token("real-1", "real@example.com")

    def test_demo_mutations_are_blocked(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            blocked = asyncio.run(demo_guard.demo_write_blocked(
                _scope(method, "/api/candidates", self.demo_token)))
            self.assertTrue(blocked, method)

    def test_reads_other_users_and_auth_paths_pass(self):
        cases = [
            _scope("GET", "/api/candidates", self.demo_token),
            _scope("POST", "/api/candidates", self.real_token),
            _scope("POST", "/api/candidates"),           # anonymous → auth layer's problem
            _scope("POST", "/api/candidates", "garbage"),
            _scope("POST", "/api/auth/login", self.demo_token),
            _scope("POST", "/api/auth/logout", self.demo_token),
            _scope("POST", "/health", self.demo_token),  # non-/api/ untouched
        ]
        for scope in cases:
            blocked = asyncio.run(demo_guard.demo_write_blocked(scope))
            self.assertFalse(blocked, f"{scope['method']} {scope['path']}")

    def test_cookie_token_is_blocked_too(self):
        scope = _scope("POST", "/api/candidates",
                       extra_headers=[(b"cookie", f"cgr_token={self.demo_token}".encode())])
        self.assertTrue(asyncio.run(demo_guard.demo_write_blocked(scope)))

    def test_middleware_answers_403_without_reaching_the_app(self):
        inner_called = []

        async def inner_app(scope, receive, send):
            inner_called.append(scope["path"])

        sent = []

        async def send(message):
            sent.append(message)

        guard = demo_guard.DemoWriteGuard(inner_app)
        asyncio.run(guard(_scope("POST", "/api/candidates", self.demo_token), None, send))
        self.assertEqual(inner_called, [])
        self.assertEqual(sent[0]["status"], 403)
        self.assertIn(b"Demo account", sent[1]["body"])

        # And a clean request flows straight through.
        asyncio.run(guard(_scope("GET", "/api/candidates", self.demo_token), None, send))
        self.assertEqual(inner_called, ["/api/candidates"])

    def test_read_only_posts_are_not_treated_as_writes(self):
        """The template preview only renders and returns — blocking it would
        blank out a working screen rather than protect anything."""
        for path in sorted(demo_guard.ALLOWED_READ_POSTS):
            self.assertFalse(
                asyncio.run(demo_guard.demo_write_blocked(_scope("POST", path, self.demo_token))),
                path,
            )
        # A neighbouring path is still a write.
        self.assertTrue(asyncio.run(demo_guard.demo_write_blocked(
            _scope("POST", "/api/templates", self.demo_token))))

    def test_credential_handing_reads_are_refused(self):
        """These GETs mint and return the token that guards
        the intake webhook — which files a real candidate and texts them."""
        for path in sorted(demo_guard.BLOCKED_READS):
            self.assertTrue(
                asyncio.run(demo_guard.demo_write_blocked(_scope("GET", path, self.demo_token))),
                path,
            )
            # A real account keeps them.
            self.assertFalse(
                asyncio.run(demo_guard.demo_write_blocked(_scope("GET", path, self.real_token))),
                path,
            )
        # Ordinary reads are untouched.
        self.assertFalse(asyncio.run(demo_guard.demo_write_blocked(
            _scope("GET", "/api/candidates", self.demo_token))))

    def test_blocked_read_says_why(self):
        sent = []

        async def send(message):
            sent.append(message)

        async def inner_app(scope, receive, send):
            raise AssertionError("must not reach the app")

        guard = demo_guard.DemoWriteGuard(inner_app)
        asyncio.run(guard(
            _scope("GET", "/api/integrations/zapier-token", self.demo_token), None, send))
        self.assertEqual(sent[0]["status"], 403)
        self.assertIn(b"credentials", sent[1]["body"])




class CredentialScrubTests(unittest.TestCase):
    """A demo account may read a candidate; it may not read the public_token
    that would let it POST to the unauthenticated portal and rebook them."""

    def setUp(self):
        _arm(["demo-1"])
        self.demo_token = create_token("demo-1", "demo@example.com")
        self.real_token = create_token("real-1", "real@example.com")

    def test_scrub_blanks_nested_credentials_and_keeps_shape(self):
        payload = {
            "candidates": [
                {"id": "c1", "email": "a@b.c", "public_token": "deadbeef",
                 "nested": {"zapier_webhook_token": "x", "keep": 1}},
            ],
            "public_token": "top-level",
            "count": 1,
        }
        out = demo_guard.scrub_credentials(payload)
        self.assertEqual(out["candidates"][0]["public_token"], "")
        self.assertEqual(out["candidates"][0]["nested"]["zapier_webhook_token"], "")
        self.assertEqual(out["public_token"], "")
        # Everything else survives, keys included.
        self.assertEqual(out["candidates"][0]["email"], "a@b.c")
        self.assertEqual(out["candidates"][0]["nested"]["keep"], 1)
        self.assertEqual(out["count"], 1)
        self.assertIn("public_token", out["candidates"][0])

    def _run_through_guard(self, token, body, content_type=b"application/json"):
        sent = []

        async def send(message):
            sent.append(message)

        async def inner_app(scope, receive, send):
            await send({
                "type": "http.response.start", "status": 200,
                "headers": [(b"content-type", content_type),
                            (b"content-length", str(len(body)).encode())],
            })
            await send({"type": "http.response.body", "body": body})

        guard = demo_guard.DemoWriteGuard(inner_app)
        asyncio.run(guard(_scope("GET", "/api/candidates", token), None, send))
        return sent

    def test_demo_reads_come_back_scrubbed(self):
        body = json.dumps([{"id": "c1", "public_token": "secret"}]).encode()
        sent = self._run_through_guard(self.demo_token, body)
        out = json.loads(sent[-1]["body"])
        self.assertEqual(out[0]["public_token"], "")
        self.assertEqual(out[0]["id"], "c1")
        # Content-length is corrected, and declared exactly once.
        lengths = [v for k, v in sent[0]["headers"] if k.lower() == b"content-length"]
        self.assertEqual(lengths, [str(len(sent[-1]["body"])).encode()])

    def test_real_accounts_get_the_bytes_untouched(self):
        body = json.dumps([{"id": "c1", "public_token": "secret"}]).encode()
        sent = self._run_through_guard(self.real_token, body)
        self.assertEqual(sent[-1]["body"], body)

    def test_non_json_passes_through(self):
        body = b"<html>not json</html>"
        sent = self._run_through_guard(self.demo_token, body, content_type=b"text/html")
        self.assertEqual(sent[-1]["body"], body)

    def test_unparseable_json_passes_through(self):
        body = b"{not json at all"
        sent = self._run_through_guard(self.demo_token, body)
        self.assertEqual(sent[-1]["body"], body)

    def test_empty_body_keeps_its_declared_length(self):
        """A HEAD or 204 declares a length for a body it never sends."""
        sent = self._run_through_guard(self.demo_token, b"")
        lengths = [v for k, v in sent[0]["headers"] if k.lower() == b"content-length"]
        self.assertEqual(lengths, [b"0"])
        self.assertEqual(sent[-1]["body"], b"")

    def test_non_api_and_anonymous_requests_cost_no_lookup(self):
        """Static assets and health checks must not reach Mongo just to learn
        they are not a demo account."""
        calls = []
        real_lookup = demo_guard._demo_user_ids

        async def counting_lookup():
            calls.append(1)
            return await real_lookup()

        demo_guard._demo_user_ids = counting_lookup
        try:
            # Anonymous /api/ request: token decode fails, no lookup.
            asyncio.run(demo_guard.is_demo_caller(_scope("GET", "/api/candidates")))
            self.assertEqual(calls, [])
            # A real token does need the lookup.
            asyncio.run(demo_guard.is_demo_caller(
                _scope("GET", "/api/candidates", self.demo_token)))
            self.assertEqual(len(calls), 1)
        finally:
            demo_guard._demo_user_ids = real_lookup


if __name__ == "__main__":
    unittest.main()
