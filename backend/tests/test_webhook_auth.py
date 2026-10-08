"""Every route without a logged-in user must prove who is calling, and must
refuse when the secret it needs is not configured (fail closed).

Unit tests for webhook_auth.py plus HTTP-level checks against the real app:
the refusals all happen before any database call, so these run with a dead
MONGO_URL and no network.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/x")
os.environ.setdefault("DB_NAME", "x")
os.environ.setdefault("JWT_SECRET", "unit-test-secret-with-32-plus-chars!!")

from starlette.requests import Request  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import webhook_auth  # noqa: E402

SECRET_ENVS = (
    "ELEVENLABS_TOOL_SECRET", "ELEVENLABS_WEBHOOK_SECRET", "SENDGRID_INBOUND_SECRET",
    "TWILIO_AUTH_TOKEN", "ALLOW_UNSIGNED_TWILIO_WEBHOOKS", "APP_PUBLIC_URL",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in SECRET_ENVS:
        monkeypatch.delenv(name, raising=False)


def _request(headers=None, query=b"", method="POST", path="/x", body=b""):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "method": method, "path": path, "headers": raw,
             "query_string": query, "scheme": "https", "server": ("app.example", 443)}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


# ---------------------------------------------------------------- shared secrets

def test_shared_secret_unset_means_feature_off(monkeypatch):
    with pytest.raises(HTTPException) as e:
        webhook_auth.require_shared_secret(_request(), "SENDGRID_INBOUND_SECRET")
    assert e.value.status_code == 503


def test_shared_secret_wrong_or_missing_is_refused(monkeypatch):
    monkeypatch.setenv("SENDGRID_INBOUND_SECRET", "right-secret")
    for req in (_request(), _request({"x-webhook-secret": "wrong"}), _request(query=b"key=wrong")):
        with pytest.raises(HTTPException) as e:
            webhook_auth.require_sendgrid_inbound_secret(req)
        assert e.value.status_code == 403


def test_sendgrid_secret_accepted_by_query_header_or_basic_auth(monkeypatch):
    monkeypatch.setenv("SENDGRID_INBOUND_SECRET", "right-secret")
    webhook_auth.require_sendgrid_inbound_secret(_request(query=b"key=right-secret"))
    webhook_auth.require_sendgrid_inbound_secret(_request({"x-webhook-secret": "right-secret"}))
    basic = base64.b64encode(b"sendgrid:right-secret").decode()
    webhook_auth.require_sendgrid_inbound_secret(_request({"authorization": f"Basic {basic}"}))


def test_tool_secret_header(monkeypatch):
    with pytest.raises(HTTPException) as e:
        asyncio.run(webhook_auth.require_elevenlabs_tool_secret(_request()))
    assert e.value.status_code == 503
    monkeypatch.setenv("ELEVENLABS_TOOL_SECRET", "tool-secret")
    with pytest.raises(HTTPException) as e:
        asyncio.run(webhook_auth.require_elevenlabs_tool_secret(_request({"X-CGR-Tool-Secret": "nope"})))
    assert e.value.status_code == 403
    asyncio.run(webhook_auth.require_elevenlabs_tool_secret(_request({"X-CGR-Tool-Secret": "tool-secret"})))
    assert webhook_auth.elevenlabs_tool_headers() == {"X-CGR-Tool-Secret": "tool-secret"}


# ---------------------------------------------------------------- ElevenLabs HMAC

def _el_sig(secret, body, ts):
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v0={mac}"


def test_elevenlabs_signature_checks():
    body = b'{"type":"post_call_transcription"}'
    now = time.time()
    good = _el_sig("whsec", body, int(now))
    assert webhook_auth.elevenlabs_signature_valid(body, good, "whsec", now=now)
    assert not webhook_auth.elevenlabs_signature_valid(body + b" ", good, "whsec", now=now)
    assert not webhook_auth.elevenlabs_signature_valid(body, good, "other", now=now)
    stale = _el_sig("whsec", body, int(now) - 3600)
    assert not webhook_auth.elevenlabs_signature_valid(body, stale, "whsec", now=now)
    assert not webhook_auth.elevenlabs_signature_valid(body, "garbage", "whsec", now=now)
    assert not webhook_auth.elevenlabs_signature_valid(body, "", "whsec", now=now)


def test_post_call_requires_a_configured_secret(monkeypatch):
    with pytest.raises(HTTPException) as e:
        asyncio.run(webhook_auth.require_elevenlabs_signature(_request(body=b"{}")))
    assert e.value.status_code == 503
    monkeypatch.setenv("ELEVENLABS_WEBHOOK_SECRET", "whsec")
    with pytest.raises(HTTPException) as e:
        asyncio.run(webhook_auth.require_elevenlabs_signature(_request(body=b"{}")))
    assert e.value.status_code == 403
    body = b'{"data": {}}'
    req = _request({"ElevenLabs-Signature": _el_sig("whsec", body, int(time.time()))}, body=body)
    assert asyncio.run(webhook_auth.require_elevenlabs_signature(req)) == body


# ---------------------------------------------------------------- Twilio

def _twilio_sig(token, url, params):
    from twilio.request_validator import RequestValidator
    return RequestValidator(token).compute_signature(url, params)


def test_twilio_signature_covers_the_query_string(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tw-token")
    url = "https://app.example/x?conv=1"
    sig = _twilio_sig("tw-token", url, {})
    req = _request({"X-Twilio-Signature": sig, "host": "app.example"}, query=b"conv=1", method="GET")
    assert asyncio.run(webhook_auth.twilio_request_is_authentic(req)) is True
    # Same signature, different query string: refused.
    req2 = _request({"X-Twilio-Signature": sig, "host": "app.example"}, query=b"conv=2", method="GET")
    assert asyncio.run(webhook_auth.twilio_request_is_authentic(req2)) is False


def test_twilio_uses_app_public_url_behind_a_proxy(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tw-token")
    monkeypatch.setenv("APP_PUBLIC_URL", "https://recruit.example.co.uk")
    sig = _twilio_sig("tw-token", "https://recruit.example.co.uk/x", {})
    req = _request({"X-Twilio-Signature": sig, "host": "internal:8080"}, method="GET")
    assert asyncio.run(webhook_auth.twilio_request_is_authentic(req)) is True


def test_twilio_without_token_is_refused(monkeypatch):
    req = _request({"X-Twilio-Signature": "anything"}, method="GET")
    assert asyncio.run(webhook_auth.twilio_request_is_authentic(req)) is False


# ---------------------------------------------------------------- stream tokens

def test_stream_tokens_are_bound_and_expire():
    now = time.time()
    tok = webhook_auth.sign_stream_token("conv-1", now=now)
    assert webhook_auth.stream_token_valid("conv-1", tok, now=now + 10)
    assert not webhook_auth.stream_token_valid("conv-2", tok, now=now + 10)
    assert not webhook_auth.stream_token_valid("conv-1", tok, now=now + webhook_auth.STREAM_TOKEN_TTL_SECONDS + 5)
    exp, mac = tok.split(".", 1)
    assert not webhook_auth.stream_token_valid("conv-1", f"{int(exp) + 9999}.{mac}", now=now)
    assert not webhook_auth.stream_token_valid("conv-1", "not-a-token", now=now)


# ---------------------------------------------------------------- the real app

@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    import server
    return TestClient(server.app)


TOOL_PATHS = [
    "/api/public/send-booking-link", "/api/public/register-caller",
    "/api/public/send-office-details", "/api/public/book-by-phone",
    "/api/public/reschedule-start-by-phone", "/api/webhooks/elevenlabs/conversation-init",
]


@pytest.mark.parametrize("path", TOOL_PATHS)
def test_voice_tools_refuse_without_the_tool_secret(client, monkeypatch, path):
    body = {"phone": "+15550100100", "pipeline_slug": "downtown", "first_name": "Rowan",
            "slot_iso": "2030-01-01T10:00:00Z", "new_start_date": "2030-01-01"}
    assert client.post(path, json=body).status_code == 503
    monkeypatch.setenv("ELEVENLABS_TOOL_SECRET", "tool-secret")
    assert client.post(path, json=body).status_code == 403
    assert client.post(path, json=body, headers={"X-CGR-Tool-Secret": "wrong"}).status_code == 403


def test_post_call_webhook_refuses_unsigned(client, monkeypatch):
    body = {"data": {"conversation_id": "c1", "conversation_initiation_client_data": {
        "dynamic_variables": {"call_kind": "web_screening", "candidate_id": "x"}}}}
    assert client.post("/api/webhooks/elevenlabs/post-call", json=body).status_code == 503
    monkeypatch.setenv("ELEVENLABS_WEBHOOK_SECRET", "whsec")
    assert client.post("/api/webhooks/elevenlabs/post-call", json=body).status_code == 403


@pytest.mark.parametrize("path", ["/api/webhooks/sendgrid/inbound", "/api/webhooks/sendgrid/inbound-email"])
def test_sendgrid_inbound_refuses_without_the_key(client, monkeypatch, path):
    form = {"from": "a@example.com", "to": "apply@inbox.example.com", "subject": "x", "text": "x"}
    assert client.post(path, data=form).status_code == 503
    monkeypatch.setenv("SENDGRID_INBOUND_SECRET", "sg-secret")
    assert client.post(path, data=form).status_code == 403
    assert client.post(path + "?key=wrong", data=form).status_code == 403


def test_inbound_sms_refused_without_twilio_token(client):
    r = client.post("/api/webhooks/twilio/inbound-sms", data={"From": "+15550100100", "Body": "YES"})
    assert r.status_code == 403


def test_twiml_voice_requires_twilio_signature(client, monkeypatch):
    assert client.post("/api/twiml/voice/conv-1").status_code == 403
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tw-token")
    r = client.post("/api/twiml/voice/conv-1", headers={"X-Twilio-Signature": "forged"})
    assert r.status_code == 403


def test_twiml_voice_hands_out_a_signed_stream_url(client, monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tw-token")
    url = "http://testserver/api/twiml/voice/conv-1"
    sig = _twilio_sig("tw-token", url, {})
    r = client.post("/api/twiml/voice/conv-1", headers={"X-Twilio-Signature": sig})
    assert r.status_code == 200, r.text
    import re
    m = re.search(r'/api/twiml/stream/conv-1/([^"]+)"', r.text)
    assert m and webhook_auth.stream_token_valid("conv-1", m.group(1))


def test_stream_websocket_refuses_a_bad_token(client):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/twiml/stream/conv-1/123.deadbeef") as ws:
            ws.receive_text()
    # The old unsigned path no longer exists.
    with pytest.raises(Exception):
        with client.websocket_connect("/api/twiml/stream/conv-1") as ws:
            ws.receive_text()


def test_partner_feed_fails_closed(client, monkeypatch):
    monkeypatch.delenv("PARTNER_FEED_API_KEY", raising=False)
    r = client.get("/api/partner/stage-events?date_from=2026-01-01&date_to=2026-01-02")
    assert r.status_code == 503


# ---------------------------------------------------------------- tool sync sends the header

class _Resp:
    def __init__(self, data=None, code=200):
        self._data = data or {}
        self.status_code = code
        self.text = json.dumps(self._data)

    def json(self):
        return self._data


def test_tool_sync_and_init_webhook_carry_the_secret(monkeypatch):
    import voice_service
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-key")
    monkeypatch.setenv("ELEVENLABS_TOOL_SECRET", "tool-secret")
    sent = []
    monkeypatch.setattr(voice_service.requests, "get", lambda *a, **k: _Resp({"tools": []}))
    monkeypatch.setattr(voice_service.requests, "post",
                        lambda url, **k: (sent.append(k.get("json")), _Resp({"id": "t1"}))[1])
    monkeypatch.setattr(voice_service.requests, "patch",
                        lambda url, **k: (sent.append(k.get("json")), _Resp({}))[1])
    voice_service.upsert_elevenlabs_tools("https://app.example")
    tools = [b for b in sent if "tool_config" in b]
    assert tools, "no tools were created"
    for b in tools:
        assert b["tool_config"]["api_schema"]["request_headers"] == {"X-CGR-Tool-Secret": "tool-secret"}
    sent.clear()
    voice_service.ensure_workspace_init_webhook("https://app.example/api/webhooks/elevenlabs/conversation-init")
    hook = sent[0]["conversation_initiation_client_data_webhook"]
    assert hook["request_headers"] == {"X-CGR-Tool-Secret": "tool-secret"}


# ---------------------------------------------------------------- post-call identity

@pytest.fixture
def wdb(monkeypatch):
    mm = pytest.importorskip("mongomock_motor")
    db = mm.AsyncMongoMockClient()["cgr_test"]
    import deps
    import routes.webhooks as wh
    for mod in (deps, wh):
        monkeypatch.setattr(mod, "db", db)
    seen = []

    async def fake_inbound(conv_id, data, init):
        seen.append(dict(init))
        return {"ok": True, "inbound": True}

    async def fake_process(conv, conv_id, data, **k):
        seen.append({"processed": conv.get("candidate_id")})
        return {"ok": True}

    monkeypatch.setattr(wh, "_handle_inbound_post_call", fake_inbound)
    monkeypatch.setattr(wh, "_process_screening_post_call", fake_process)
    asyncio.run(db.pipelines.insert_one({"id": "p1", "user_id": "owner1", "name": "Downtown",
                                         "public_slug": "downtown", "twilio_phone_number": "+15550109000"}))
    asyncio.run(db.candidates.insert_many([
        {"id": "caller", "user_id": "owner1", "pipeline_id": "p1", "first_name": "C",
         "phone": "+15550100001", "stage": "SCREENING", "screening_status": "no_answer"},
        {"id": "victim", "user_id": "owner1", "pipeline_id": "p1", "first_name": "V",
         "phone": "+15550100999", "stage": "SCREENING", "screening_status": "no_answer"},
    ]))
    return db, seen


def _signed_post_call(client, monkeypatch, data):
    monkeypatch.setenv("ELEVENLABS_WEBHOOK_SECRET", "whsec")
    raw = json.dumps({"type": "post_call_transcription", "data": data}).encode()
    ts = int(time.time())
    return client.post("/api/webhooks/elevenlabs/post-call", content=raw,
                       headers={"ElevenLabs-Signature": _el_sig("whsec", raw, ts),
                                "Content-Type": "application/json"})


SPOOFED = {"call_kind": "inbound_screening", "user_id": "owner1", "pipeline_id": "p1",
           "candidate_id": "victim", "phone": "+15550100999"}


def test_the_public_voice_session_route_is_gone(client):
    assert client.post("/api/public/retry/any-token/voice-session").status_code in (404, 405)


@pytest.mark.parametrize("kind", ["inbound_screening", "inbound", "web_screening"])
def test_signed_post_call_without_a_phone_call_touches_nobody(client, monkeypatch, wdb, kind):
    """A browser/widget conversation chooses its own dynamic variables. Signed
    by ElevenLabs or not, they must never pick the candidate."""
    db, seen = wdb
    r = _signed_post_call(client, monkeypatch, {
        "conversation_id": "conv-web",
        "transcript": [{"role": "user", "message": "hi"}],
        "conversation_initiation_client_data": {"dynamic_variables": {**SPOOFED, "call_kind": kind}},
    })
    assert r.status_code == 200 and r.json().get("ok") is False
    assert seen == []
    assert asyncio.run(db.conversations.count_documents({})) == 0


def test_inbound_identity_comes_from_our_record_not_the_echo(client, monkeypatch, wdb):
    """A real inbound phone call: the init webhook's record (matched on the call
    SID and the caller's number) decides who it was, whatever the echo says."""
    import routes.webhooks as wh
    db, seen = wdb
    asyncio.run(wh._remember_inbound_init("CA123", "+15550100001", "agent_in", {
        "call_kind": "inbound", "user_id": "owner1", "pipeline_id": "p1",
        "candidate_id": "caller", "phone": "+15550100001"}))
    r = _signed_post_call(client, monkeypatch, {
        "conversation_id": "conv-phone",
        "metadata": {"phone_call": {"call_sid": "CA123", "external_number": "+15550100001",
                                    "agent_number": "+15550109000"}},
        "conversation_initiation_client_data": {"dynamic_variables": SPOOFED},
    })
    assert r.status_code == 200
    assert len(seen) == 1 and seen[0]["candidate_id"] == "caller"
    assert seen[0]["call_kind"] == "inbound" and seen[0]["phone"] == "+15550100001"


def test_a_record_for_another_number_is_not_borrowed(client, monkeypatch, wdb):
    import routes.webhooks as wh
    db, seen = wdb
    asyncio.run(wh._remember_inbound_init("CA999", "+15550100999", "agent_in", dict(SPOOFED)))
    r = _signed_post_call(client, monkeypatch, {
        "conversation_id": "conv-phone-2",
        "metadata": {"phone_call": {"call_sid": "CA999", "external_number": "+15550100001",
                                    "agent_number": "+15550109000"}},
        "conversation_initiation_client_data": {"dynamic_variables": SPOOFED},
    })
    assert r.status_code == 200
    # Falls back to the facts of the call: the real caller, front desk only.
    assert len(seen) == 1 and seen[0]["candidate_id"] == "caller"
    assert seen[0]["call_kind"] == "inbound"


def test_agents_are_synced_with_authentication_on(monkeypatch):
    import voice_service
    monkeypatch.delenv("ELEVENLABS_AGENT_AUTH", raising=False)
    assert voice_service.agent_platform_settings()["auth"] == {"enable_auth": True}
    ps = voice_service.agent_platform_settings({"webhook": {"url": "https://x.example/api/w"}})
    assert ps["webhook"]["url"] and ps["auth"]["enable_auth"] is True
    body = voice_service._inbound_agent_body({}, "prompt", "", [], "en",
                                             init_webhook_url="https://x.example/init")
    assert body["platform_settings"]["auth"]["enable_auth"] is True
    assert body["platform_settings"]["overrides"]["enable_conversation_initiation_client_data_from_webhook"]
    monkeypatch.setenv("ELEVENLABS_AGENT_AUTH", "false")
    assert "auth" not in voice_service.agent_platform_settings()


def test_widget_test_session_is_owner_only(client):
    import deps
    import server
    server.app.dependency_overrides[deps.current_user] = lambda: {
        "id": "u1", "auth_user_id": "r1", "role": "recruiter", "pipeline_ids": ["p1"]}
    try:
        r = client.post("/api/elevenlabs/agent/test-session", json={"agent_id": "agent_x"})
        assert r.status_code == 403
    finally:
        server.app.dependency_overrides.clear()
