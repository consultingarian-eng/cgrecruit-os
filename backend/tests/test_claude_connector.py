"""End-to-end tests for the Claude connector core (claude_connector/core.py).

Drives the real routes through an in-process ASGI client with an in-memory
fake Mongo, covering what Claude does: discovery, dynamic registration, the
sign-in page, PKCE code exchange, MCP calls, refresh rotation, and revocation
when the account changes. Nothing here touches a real database.

Run: cd backend && .venv/bin/python tests/test_claude_connector.py
(also collected by pytest). This file is shared verbatim with CG1.
"""
import asyncio
import base64
import copy
import hashlib
import os
import re
import secrets
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/x")
os.environ.setdefault("DB_NAME", "x")
os.environ.setdefault("JWT_SECRET", "unit-test-secret-with-32-plus-chars!!")

import httpx  # noqa: E402
from starlette.applications import Starlette  # noqa: E402

from claude_connector.core import Connector, LoginRefused, Tool, ToolError  # noqa: E402

BASE = "https://app.example.test"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"


# ---------------------------------------------------------------- fake Mongo

def _aware(v):
    if isinstance(v, datetime) and v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v


def _naive(v):
    # Real motor clients here are tz_aware=False: datetimes come back naive UTC.
    if isinstance(v, datetime) and v.tzinfo is not None:
        return v.astimezone(timezone.utc).replace(tzinfo=None)
    return v


def _matches(doc, query):
    for key, cond in query.items():
        cur = doc.get(key)
        if isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond):
            for op, val in cond.items():
                a, b = _aware(cur), _aware(val)
                if op == "$gt" and not (a is not None and a > b):
                    return False
                if op == "$in" and cur not in val:
                    return False
        elif cur != cond:
            return False
    return True


class FakeCollection:
    def __init__(self):
        self.docs = []

    async def create_index(self, *args, **kwargs):
        return "ok"

    async def insert_one(self, doc):
        self.docs.append({k: _naive(v) for k, v in doc.items()})

    async def find_one(self, query, projection=None):
        for d in self.docs:
            if _matches(d, query):
                return copy.deepcopy(d)
        return None

    async def find_one_and_delete(self, query):
        for i, d in enumerate(self.docs):
            if _matches(d, query):
                return self.docs.pop(i)
        return None

    async def delete_one(self, query):
        for i, d in enumerate(self.docs):
            if _matches(d, query):
                self.docs.pop(i)
                return

    async def delete_many(self, query):
        self.docs = [d for d in self.docs if not _matches(d, query)]

    async def update_one(self, query, update):
        for d in self.docs:
            if _matches(d, query):
                d.update(update.get("$set", {}))
                return

    async def count_documents(self, query):
        return sum(1 for d in self.docs if _matches(d, query))


class FakeDB:
    def __init__(self):
        self._c = {}

    def __getitem__(self, name):
        return self._c.setdefault(name, FakeCollection())

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]


# ---------------------------------------------------------------- fake app

async def echo_tool(ctx, args):
    if args["n"] == 13:
        raise ToolError("13 is not allowed")
    return {"you_are": ctx.user["email"], "n": args["n"]}


class FakeAdapter:
    product = "TestApp"
    instructions = "Test instructions."
    tools = [Tool("echo", "Echo", "Echo a number.", echo_tool,
                  {"n": {"type": "integer", "minimum": 0, "maximum": 100}}, required=["n"])]

    def __init__(self, db):
        self.db = db

    async def authenticate(self, email, password):
        user = await self.db.users.find_one({"email": email})
        if not user or user["password"] != password:
            raise LoginRefused("Wrong email or password.")
        return user

    async def load_user(self, user_id):
        return await self.db.users.find_one({"id": user_id})

    def refuse_reason(self, user):
        return "Demo accounts can't connect." if user.get("is_demo") else None

    def fingerprint(self, user):
        return hashlib.sha256(user["password"].encode()).hexdigest()[:16]


def pkce_pair():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


class ConnectorFlowTest(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        asyncio.run(self.db.users.insert_one({"id": "u1", "email": "rep@example.test", "password": "pw-correct"}))
        asyncio.run(self.db.users.insert_one({"id": "u2", "email": "demo@example.test", "password": "pw-demo", "is_demo": True}))
        self.connector = Connector(db=self.db, public_url=BASE, adapter=FakeAdapter(self.db))
        self.app = Starlette(routes=self.connector.routes())

    def run_async(self, coro_fn):
        async def go():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
                return await coro_fn(client)
        return asyncio.run(go())

    # helpers -------------------------------------------------------------

    async def register(self, client, redirect=CALLBACK, method="none"):
        r = await client.post("/oauth/register", json={"redirect_uris": [redirect], "client_name": "Claude",
                                                       "token_endpoint_auth_method": method,
                                                       "grant_types": ["authorization_code", "refresh_token"]})
        return r

    async def sign_in(self, client, client_id, email="rep@example.test", password="pw-correct", redirect=CALLBACK):
        verifier, challenge = pkce_pair()
        q = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect, "state": "st-1",
             "code_challenge": challenge, "code_challenge_method": "S256", "scope": "read", "resource": f"{BASE}/mcp"}
        page = await client.get("/oauth/authorize?" + urlencode(q))
        self.assertEqual(page.status_code, 200, page.text)
        request_id = re.search(r'name="request_id" value="([^"]+)"', page.text).group(1)
        r = await client.post("/oauth/authorize", data={"request_id": request_id, "email": email, "password": password, "action": "allow"})
        return r, verifier, request_id

    async def tokens(self, client):
        reg = (await self.register(client)).json()
        r, verifier, _ = await self.sign_in(client, reg["client_id"])
        code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
        tok = await client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": reg["client_id"],
                                                      "redirect_uri": CALLBACK, "code_verifier": verifier})
        self.assertEqual(tok.status_code, 200, tok.text)
        return reg, tok.json()

    async def rpc(self, client, token, method, params=None, msg_id=1):
        body = {"jsonrpc": "2.0", "method": method}
        if msg_id is not None:
            body["id"] = msg_id
        if params is not None:
            body["params"] = params
        return await client.post("/mcp", json=body, headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"})

    # tests -----------------------------------------------------------------

    def test_discovery(self):
        async def t(client):
            r = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            self.assertEqual(r.status_code, 401)
            self.assertIn(f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"', r.headers["www-authenticate"])
            prm = (await client.get("/.well-known/oauth-protected-resource/mcp")).json()
            self.assertEqual(prm["resource"], f"{BASE}/mcp")
            self.assertEqual(prm["authorization_servers"], [BASE])
            asm = (await client.get("/.well-known/oauth-authorization-server")).json()
            self.assertEqual(asm["issuer"], BASE)
            self.assertIn("S256", asm["code_challenge_methods_supported"])
            self.assertIn("none", asm["token_endpoint_auth_methods_supported"])
            self.assertTrue(asm["registration_endpoint"].endswith("/oauth/register"))
        self.run_async(t)

    def test_registration_only_allows_claude_redirects(self):
        async def t(client):
            evil = await self.register(client, redirect="https://evil.example/cb")
            self.assertEqual(evil.status_code, 400)
            self.assertEqual(evil.json()["error"], "invalid_redirect_uri")
            ok = await self.register(client)
            self.assertEqual(ok.status_code, 201)
            self.assertNotIn("client_secret", ok.json())
            loop = await self.register(client, redirect="http://localhost:53682/callback")
            self.assertEqual(loop.status_code, 201)
            confidential = await self.register(client, method="client_secret_post")
            self.assertIn("client_secret", confidential.json())
        self.run_async(t)

    def test_full_flow_initialize_list_call(self):
        async def t(client):
            _, tok = await self.tokens(client)
            self.assertEqual(tok["token_type"], "Bearer")
            init = (await self.rpc(client, tok["access_token"], "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})).json()
            self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
            self.assertEqual(init["result"]["serverInfo"]["title"], "TestApp")
            self.assertEqual(init["result"]["instructions"], "Test instructions.")
            unknown = (await self.rpc(client, tok["access_token"], "initialize", {"protocolVersion": "2099-01-01"})).json()
            self.assertEqual(unknown["result"]["protocolVersion"], "2025-11-25")
            note = await self.rpc(client, tok["access_token"], "notifications/initialized", msg_id=None)
            self.assertEqual(note.status_code, 202)
            tools = (await self.rpc(client, tok["access_token"], "tools/list")).json()["result"]["tools"]
            self.assertEqual([t_["name"] for t_ in tools], ["echo"])
            self.assertTrue(tools[0]["annotations"]["readOnlyHint"])
            call = (await self.rpc(client, tok["access_token"], "tools/call", {"name": "echo", "arguments": {"n": 7}})).json()["result"]
            self.assertFalse(call["isError"])
            self.assertIn('"you_are":"rep@example.test"', call["content"][0]["text"])
            bad = (await self.rpc(client, tok["access_token"], "tools/call", {"name": "echo", "arguments": {"n": 500}})).json()["result"]
            self.assertTrue(bad["isError"])
            refused = (await self.rpc(client, tok["access_token"], "tools/call", {"name": "echo", "arguments": {"n": 13}})).json()["result"]
            self.assertEqual(refused["content"][0]["text"], "13 is not allowed")
            extra = (await self.rpc(client, tok["access_token"], "tools/call", {"name": "echo", "arguments": {"n": 1, "x": 2}})).json()["result"]
            self.assertTrue(extra["isError"])
            nope = (await self.rpc(client, tok["access_token"], "tools/call", {"name": "write_stuff", "arguments": {}})).json()
            self.assertEqual(nope["error"]["code"], -32602)
            discover = (await self.rpc(client, tok["access_token"], "server/discover", {})).json()
            self.assertEqual(discover["error"]["code"], -32601)  # 2026-07-28 clients fall back to initialize
            get = await client.get("/mcp", headers={"Authorization": f"Bearer {tok['access_token']}"})
            self.assertEqual(get.status_code, 405)
            audit = self.db["connector_audit"].docs
            self.assertEqual(len(audit), 4)
            self.assertEqual(audit[0]["user_id"], "u1")
        self.run_async(t)

    def test_code_is_single_use_and_pkce_checked(self):
        async def t(client):
            reg = (await self.register(client)).json()
            r, verifier, _ = await self.sign_in(client, reg["client_id"])
            loc = urlparse(r.headers["location"])
            self.assertEqual(f"{loc.scheme}://{loc.netloc}{loc.path}", CALLBACK)
            params = parse_qs(loc.query)
            self.assertEqual(params["state"], ["st-1"])
            code = params["code"][0]
            wrong = await client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": reg["client_id"],
                                                            "redirect_uri": CALLBACK, "code_verifier": "x" * 50})
            self.assertEqual(wrong.json()["error"], "invalid_grant")
            again = await client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": reg["client_id"],
                                                            "redirect_uri": CALLBACK, "code_verifier": verifier})
            self.assertEqual(again.json()["error"], "invalid_grant")  # burned by the first attempt
        self.run_async(t)

    def test_refresh_rotates_and_old_tokens_die(self):
        async def t(client):
            reg, tok = await self.tokens(client)
            fresh = await client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": reg["client_id"]})
            self.assertEqual(fresh.status_code, 200, fresh.text)
            new = fresh.json()
            self.assertNotEqual(new["refresh_token"], tok["refresh_token"])
            reuse = await client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": reg["client_id"]})
            self.assertEqual(reuse.json()["error"], "invalid_grant")
            self.assertEqual((await self.rpc(client, tok["access_token"], "ping")).status_code, 401)
            self.assertEqual((await self.rpc(client, new["access_token"], "ping")).status_code, 200)
        self.run_async(t)

    def test_password_change_ends_connection(self):
        async def t(client):
            reg, tok = await self.tokens(client)
            await self.db.users.update_one({"id": "u1"}, {"$set": {"password": "pw-new"}})
            r = await self.rpc(client, tok["access_token"], "ping")
            self.assertEqual(r.status_code, 401)
            self.assertIn('error="invalid_token"', r.headers["www-authenticate"])
            ref = await client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": reg["client_id"]})
            self.assertEqual(ref.json()["error"], "invalid_grant")
        self.run_async(t)

    def test_deleted_or_demo_user_cannot_use_connector(self):
        async def t(client):
            reg = (await self.register(client)).json()
            r, _, _ = await self.sign_in(client, reg["client_id"], email="demo@example.test", password="pw-demo")
            self.assertEqual(r.status_code, 400)
            self.assertIn("Demo accounts", r.text)
            reg2, tok = await self.tokens(client)
            await self.db.users.delete_many({"id": "u1"})
            self.assertEqual((await self.rpc(client, tok["access_token"], "ping")).status_code, 401)
        self.run_async(t)

    def test_wrong_password_and_throttle(self):
        async def t(client):
            reg = (await self.register(client)).json()
            r, _, request_id = await self.sign_in(client, reg["client_id"], password="nope")
            self.assertEqual(r.status_code, 400)
            self.assertIn("Wrong email or password", r.text)
            for _ in range(10):
                r = await client.post("/oauth/authorize", data={"request_id": request_id, "email": "rep@example.test", "password": "nope", "action": "allow"})
            r = await client.post("/oauth/authorize", data={"request_id": request_id, "email": "rep@example.test", "password": "pw-correct", "action": "allow"})
            self.assertIn("Too many attempts", r.text)
        self.run_async(t)

    def test_cancel_and_bad_links(self):
        async def t(client):
            reg = (await self.register(client)).json()
            verifier, challenge = pkce_pair()
            q = {"response_type": "code", "client_id": reg["client_id"], "redirect_uri": CALLBACK, "state": "s",
                 "code_challenge": challenge, "code_challenge_method": "S256"}
            page = await client.get("/oauth/authorize?" + urlencode(q))
            self.assertIn("form-action 'self' https://claude.ai", page.headers["content-security-policy"])
            self.assertEqual(page.headers["x-frame-options"], "DENY")
            rid = re.search(r'name="request_id" value="([^"]+)"', page.text).group(1)
            r = await client.post("/oauth/authorize", data={"request_id": rid, "action": "cancel"})
            self.assertEqual(parse_qs(urlparse(r.headers["location"]).query)["error"], ["access_denied"])
            bad_client = await client.get("/oauth/authorize?" + urlencode({**q, "client_id": "nope"}))
            self.assertEqual(bad_client.status_code, 400)
            self.assertNotIn("location", bad_client.headers)
            bad_redirect = await client.get("/oauth/authorize?" + urlencode({**q, "redirect_uri": "https://evil.example/cb"}))
            self.assertEqual(bad_redirect.status_code, 400)
            no_pkce = await client.get("/oauth/authorize?" + urlencode({k: v for k, v in q.items() if not k.startswith("code_")}))
            self.assertIn("error=invalid_request", no_pkce.headers["location"])
        self.run_async(t)

    def test_loopback_redirect_any_port(self):
        async def t(client):
            reg = (await self.register(client, redirect="http://127.0.0.1:40001/callback")).json()
            r, _, _ = await self.sign_in(client, reg["client_id"], redirect="http://127.0.0.1:51234/callback")
            self.assertTrue(r.headers["location"].startswith("http://127.0.0.1:51234/callback?code="))
        self.run_async(t)

    def test_foreign_origin_refused(self):
        async def t(client):
            _, tok = await self.tokens(client)
            r = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                  headers={"Authorization": f"Bearer {tok['access_token']}", "Origin": "https://evil.example"})
            self.assertEqual(r.status_code, 403)
            ok = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                   headers={"Authorization": f"Bearer {tok['access_token']}", "Origin": "https://claude.ai"})
            self.assertEqual(ok.status_code, 200)
        self.run_async(t)

    def test_confidential_client_needs_secret(self):
        async def t(client):
            reg = (await self.register(client, method="client_secret_post")).json()
            r, verifier, _ = await self.sign_in(client, reg["client_id"])
            code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
            no_secret = await client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": reg["client_id"],
                                                                "redirect_uri": CALLBACK, "code_verifier": verifier})
            self.assertEqual(no_secret.status_code, 401)
            r, verifier, _ = await self.sign_in(client, reg["client_id"])
            code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
            basic = base64.b64encode(f"{reg['client_id']}:{reg['client_secret']}".encode()).decode()
            ok = await client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK, "code_verifier": verifier},
                                   headers={"Authorization": f"Basic {basic}"})
            self.assertEqual(ok.status_code, 200, ok.text)
        self.run_async(t)

    def test_tokens_are_stored_hashed(self):
        async def t(client):
            _, tok = await self.tokens(client)
            stored = self.db["connector_tokens"].docs
            self.assertTrue(stored)
            flat = repr(stored)
            self.assertNotIn(tok["access_token"], flat)
            self.assertNotIn(tok["refresh_token"], flat)
        self.run_async(t)


if __name__ == "__main__":
    unittest.main(verbosity=2)
