"""Who may read or write what: scope checks, public-page exposure, the owner
bootstrap, password resets and candidate-typed text in emails.

Uses an in-memory Mongo (mongomock-motor, see requirements-dev.txt) where a
route needs data; everything else refuses before touching the database.
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/x")
os.environ.setdefault("DB_NAME", "x")
os.environ.setdefault("JWT_SECRET", "unit-test-secret-with-32-plus-chars!!")
os.environ.setdefault("COMPANY_PROFILE_PATH", str(Path(__file__).with_name("company_profile.test.json")))

from fastapi.testclient import TestClient  # noqa: E402

import deps  # noqa: E402
import server  # noqa: E402


@pytest.fixture
def mdb(monkeypatch):
    mm = pytest.importorskip("mongomock_motor")
    db = mm.AsyncMongoMockClient()["cgr_test"]
    import routes.public as pub
    import routes.integrations as integ
    for mod in (deps, server, pub, integ):
        monkeypatch.setattr(mod, "db", db)
    return db


def _as(user):
    def _dep():
        return user
    return _dep


@pytest.fixture
def client():
    server.app.dependency_overrides.clear()
    yield TestClient(server.app)
    server.app.dependency_overrides.clear()


OWNER = {"id": "u1", "auth_user_id": "u1", "role": "super_admin", "pipeline_ids": []}
RECRUITER = {"id": "u1", "auth_user_id": "r1", "role": "recruiter", "pipeline_ids": ["p1"], "parent_user_id": "u1"}
VIEWER = {"id": "u1", "auth_user_id": "v1", "role": "viewer", "pipeline_ids": ["p1"], "parent_user_id": "u1"}
ANALYST = {"id": "u1", "auth_user_id": "a1", "role": "analyst", "pipeline_ids": ["p1"], "parent_user_id": "u1"}


# ---------------------------------------------------------------- Intelligence scope

def test_export_refuses_analysts(client):
    server.app.dependency_overrides[deps.current_user] = _as(ANALYST)
    assert client.get("/api/intelligence/export-csv").status_code == 403


@pytest.mark.parametrize("field", ["user_id", "pipeline_id", "added_by_user_id", "$where"])
def test_date_field_cannot_replace_the_scope(client, field):
    server.app.dependency_overrides[deps.current_user] = _as(RECRUITER)
    for path in ("/api/intelligence/export-csv", "/api/intelligence/dashboard"):
        r = client.get(path, params={"date_field": field, "date_from": "0"})
        assert r.status_code == 400, (path, field, r.status_code)


# ---------------------------------------------------------------- read-only roles

def test_read_only_roles_cannot_write_jobs_or_place_test_calls(client):
    for who in (VIEWER, ANALYST):
        server.app.dependency_overrides[deps.current_user] = _as(who)
        assert client.post("/api/jobs", json={"pipeline_id": "p1", "title": "x"}).status_code == 403
        assert client.post("/api/elevenlabs/agent/test-call",
                           json={"pipeline_id": "p1", "to_phone": "+15550100100"}).status_code == 403
        assert client.post("/api/sms/inbox/send", json={"candidate_id": "c1", "body": "hi"}).status_code == 403


# ---------------------------------------------------------------- per-candidate writes

CANDIDATE_WRITES = [
    ("patch", "/api/candidates/cB", {"rating": 1}),
    ("post", "/api/candidates/cB/move", {"stage": "TRAINING"}),
    ("post", "/api/candidates/cB/sms", {"body": "x"}),
    ("post", "/api/candidates/cB/email", {"template_key": "approval"}),
    ("post", "/api/candidates/cB/call", None),
    ("post", "/api/candidates/cB/schedule-call", {"delay_minutes": 0}),
    ("post", "/api/candidates/cB/score", None),
    ("post", "/api/candidates/cB/archive", {}),
    ("post", "/api/candidates/cB/reject", {}),
    ("post", "/api/candidates/cB/restore", None),
    ("post", "/api/candidates/cB/merge", {"source_id": "cA"}),
    ("post", "/api/candidates/cB/queue/pause", None),
    ("post", "/api/candidates/cB/queue/resume", None),
    ("post", "/api/candidates/cB/reschedule", {"appointment_at": "2030-01-01T15:00:00Z"}),
    ("post", "/api/candidates/cB/send-slot-picker", None),
    ("post", "/api/candidates/cB/send-screening-link", None),
    ("post", "/api/candidates/cB/attendance", {"attended": True}),
    ("post", "/api/candidates/cB/hire", {}),
    ("post", "/api/candidates/cB/revival-call", None),
    ("delete", "/api/candidates/cB", None),
]

# cB sits in office p2; every non-owner account below is assigned to p1 only,
# and the viewer/analyst would be refused on role alone anyway.
OUT_OF_SCOPE_RECRUITER = RECRUITER


def _seed_two_offices(mdb):
    asyncio.run(mdb.pipelines.insert_many([
        {"id": "p1", "user_id": "u1", "name": "Downtown"},
        {"id": "p2", "user_id": "u1", "name": "Riverside"},
    ]))
    asyncio.run(mdb.candidates.insert_many([
        {"id": "cA", "user_id": "u1", "pipeline_id": "p1", "first_name": "A",
         "phone": "+15550100100", "stage": "SCREENING", "added_by_user_id": "r1"},
        {"id": "cB", "user_id": "u1", "pipeline_id": "p2", "first_name": "B",
         "phone": "+15550100101", "stage": "SCREENING", "added_by_user_id": "someone"},
    ]))


@pytest.mark.parametrize("who", [VIEWER, ANALYST, OUT_OF_SCOPE_RECRUITER], ids=["viewer", "analyst", "recruiter"])
@pytest.mark.parametrize("method,path,body", CANDIDATE_WRITES, ids=[f"{m} {p}" for m, p, _ in CANDIDATE_WRITES])
def test_candidate_writes_need_a_writer_in_scope(client, mdb, who, method, path, body):
    _seed_two_offices(mdb)
    server.app.dependency_overrides[deps.current_user] = _as(who)
    kw = {"json": body} if body is not None else {}
    r = client.request(method.upper(), path, **kw)
    assert r.status_code == 403, (who["role"], method, path, r.status_code)
    after = asyncio.run(mdb.candidates.find_one({"id": "cB"}, {"_id": 0}))
    assert after and after.get("stage") == "SCREENING" and not after.get("archived_at")


def test_candidate_write_access_lets_an_in_scope_recruiter_through(mdb):
    _seed_two_offices(mdb)
    doc = asyncio.run(deps.candidate_write_access("cA", RECRUITER))
    assert doc["id"] == "cA"
    with pytest.raises(deps.HTTPException) as e:
        asyncio.run(deps.candidate_write_access("cB", RECRUITER))
    assert e.value.status_code == 403
    with pytest.raises(deps.HTTPException) as e:
        asyncio.run(deps.candidate_write_access("nope", OWNER))
    assert e.value.status_code == 404


def test_merge_source_must_be_in_scope(client, mdb):
    _seed_two_offices(mdb)
    server.app.dependency_overrides[deps.current_user] = _as(RECRUITER)
    r = client.post("/api/candidates/cA/merge", json={"source_id": "cB"})
    assert r.status_code == 403
    assert not asyncio.run(mdb.candidates.find_one({"id": "cB"}, {"_id": 0})).get("archived_at")


@pytest.mark.parametrize("who", [VIEWER, ANALYST, OUT_OF_SCOPE_RECRUITER], ids=["viewer", "analyst", "recruiter"])
def test_batch_dial_and_reconcile_need_a_writer_in_scope(client, mdb, who):
    _seed_two_offices(mdb)
    server.app.dependency_overrides[deps.current_user] = _as(who)
    assert client.post("/api/pipelines/p2/batch-dial").status_code == 403
    assert client.post("/api/pipelines/p2/call-queue/reconcile").status_code == 403
    assert client.post("/api/screening/reengage-by-text",
                       json={"pipeline_id": "p2", "dry_run": True}).status_code == 403


def test_every_candidate_write_route_carries_the_guard():
    """A new /candidates/{candidate_id}/… write route must use
    deps.candidate_write_access. The one exception authorises its callers
    itself (the CG1 shared secret or a signed-in writer in scope)."""
    from fastapi.routing import APIRoute
    own_check = {"/api/candidates/{candidate_id}/sms/resend"}
    missing = []
    for r in server.app.routes:
        if not isinstance(r, APIRoute) or not r.path.startswith("/api/candidates/{candidate_id}"):
            continue
        if not (set(r.methods) & {"POST", "PATCH", "PUT", "DELETE"}) or r.path in own_check:
            continue
        deps_used = {d.call for d in r.dependant.dependencies}
        if deps.candidate_write_access not in deps_used:
            missing.append(f"{sorted(r.methods)} {r.path}")
    assert not missing, missing


def test_seed_is_owner_only(client):
    server.app.dependency_overrides[deps.current_user] = _as(RECRUITER)
    assert client.post("/api/seed/demo").status_code == 403


def test_candidates_only_go_into_your_own_offices(client, mdb):
    asyncio.run(mdb.pipelines.insert_many([
        {"id": "p1", "user_id": "u1", "name": "Downtown", "public_slug": "downtown"},
        {"id": "p2", "user_id": "u1", "name": "Riverside", "public_slug": "riverside"},
        {"id": "px", "user_id": "someone-else", "name": "Other", "public_slug": "other"},
    ]))
    server.app.dependency_overrides[deps.current_user] = _as(RECRUITER)
    body = {"pipeline_id": "p2", "first_name": "Rowan", "email": "rowan@example.com", "phone": "+15550100101"}
    assert client.post("/api/candidates", json=body).status_code == 403  # not assigned
    server.app.dependency_overrides[deps.current_user] = _as(OWNER)
    body["pipeline_id"] = "px"
    assert client.post("/api/candidates", json=body).status_code == 404  # another account
    assert client.post("/api/jobs", json={"pipeline_id": "px", "title": "x"}).status_code == 404


# ---------------------------------------------------------------- public pages

def test_public_pipeline_page_exposes_only_the_apply_fields(client, mdb):
    asyncio.run(mdb.pipelines.insert_one({
        "id": "p1", "user_id": "u1", "name": "Downtown", "public_slug": "downtown",
        "appointment_link": "https://meet.example/private-room",
        "elevenlabs_agent_id_override": "agent_private", "inbound_agent_id": "agent_inbound",
        "twilio_phone_number": "+15550100199",
    }))
    asyncio.run(mdb.jobs.insert_one({"id": "j1", "user_id": "u1", "pipeline_id": "p1",
                                     "title": "Sales Rep", "is_active": True}))
    r = client.get("/api/public/pipelines/downtown")
    assert r.status_code == 200, r.text
    data = r.json()
    assert set(data["pipeline"]) == {"id", "name", "description", "public_slug"}
    for leaked in ("private-room", "agent_private", "agent_inbound", "+15550100199", '"u1"'):
        assert leaked not in r.text, leaked


def test_repeat_application_never_returns_someone_elses_token(client, mdb):
    asyncio.run(mdb.pipelines.insert_one({"id": "p1", "user_id": "u1", "name": "Downtown", "public_slug": "downtown"}))
    asyncio.run(mdb.candidates.insert_one({
        "id": "c1", "user_id": "u1", "pipeline_id": "p1", "first_name": "Rowan",
        "email": "rowan@example.com", "phone": "+15550100101", "stage": "SCREENING",
        "archived_at": None, "public_token": "secret-portal-token",
    }))
    r = client.post("/api/public/apply", json={
        "pipeline_id": "p1", "first_name": "Someone", "last_name": "Else",
        "email": "ROWAN@example.com", "phone": "+15550100101",
    })
    assert r.status_code == 200, r.text
    assert r.json().get("duplicate") is True
    assert "public_token" not in r.json()
    assert "secret-portal-token" not in r.text and "c1" not in r.text


def test_apply_rejects_operator_objects_as_ids(client):
    r = client.post("/api/public/apply", json={
        "pipeline_id": {"$ne": None}, "first_name": "A", "email": "a@example.com", "phone": "5550100101"})
    assert r.status_code == 400


def test_intake_webhook_rejects_another_accounts_pipeline(client, mdb):
    asyncio.run(mdb.settings.insert_one({"user_id": "u1", "pipeline_id": None, "zapier_webhook_token": "tok-123"}))
    asyncio.run(mdb.pipelines.insert_one({"id": "px", "user_id": "someone-else", "name": "Other"}))
    r = client.post("/api/webhooks/zapier/tok-123", json={"name": "Rowan Sample", "pipeline_id": "px"})
    assert r.status_code == 400
    r = client.post("/api/webhooks/zapier/tok-123", json={"name": "Rowan Sample", "pipeline_id": {"$ne": ""}})
    assert r.status_code == 400


def test_intake_token_is_owner_only(client):
    server.app.dependency_overrides[deps.current_user] = _as(RECRUITER)
    assert client.get("/api/integrations/zapier-token").status_code == 403


# ---------------------------------------------------------------- first account

def test_first_account_needs_the_setup_token(client, mdb, monkeypatch):
    body = {"email": "owner@example.com", "password": "a-long-password", "name": "Owner"}
    monkeypatch.delenv("SETUP_TOKEN", raising=False)
    assert client.post("/api/auth/register", json=body).status_code == 503
    monkeypatch.setenv("SETUP_TOKEN", "setup-123456")
    assert client.post("/api/auth/register", json=body).status_code == 403
    assert client.post("/api/auth/register", json=body, headers={"X-Setup-Token": "nope"}).status_code == 403
    r = client.post("/api/auth/register", json=body, headers={"X-Setup-Token": "setup-123456"})
    assert r.status_code == 200, r.text
    # Once an owner exists the setup token is worthless on its own. Drop the
    # owner's session cookie first, or the request is simply the owner's.
    client.cookies.clear()
    body2 = {**body, "email": "second@example.com"}
    assert client.post("/api/auth/register", json=body2, headers={"X-Setup-Token": "setup-123456"}).status_code == 403


def test_short_passwords_are_refused(client, mdb, monkeypatch):
    monkeypatch.setenv("SETUP_TOKEN", "setup-123456")
    r = client.post("/api/auth/register", json={"email": "o@example.com", "password": "short", "name": "O"},
                    headers={"X-Setup-Token": "setup-123456"})
    assert r.status_code == 422


# ---------------------------------------------------------------- password reset + sessions

def test_reset_expiry_handles_naive_aware_and_missing():
    now = datetime.now(timezone.utc)
    assert server._reset_expired(None)
    assert server._reset_expired((now - timedelta(minutes=1)).replace(tzinfo=None))
    assert not server._reset_expired((now + timedelta(minutes=30)).replace(tzinfo=None))
    assert not server._reset_expired(now + timedelta(minutes=30))
    assert server._reset_expired(now - timedelta(seconds=1))


def test_reset_tokens_are_stored_hashed(client, mdb, monkeypatch):
    asyncio.run(mdb.users.insert_one({"id": "u1", "email": "owner@example.com", "password_hash": "x"}))
    sent = {}

    async def _fake_send(to, subject, body, *a, **k):
        sent["body"] = body
        return {"status": "sent"}

    import email_service
    monkeypatch.setattr(email_service, "send_email_via_sendgrid", _fake_send)
    assert client.post("/api/auth/forgot-password", json={"email": "owner@example.com"}).status_code == 200
    token = sent["body"].split("token=")[1].split()[0]
    stored = asyncio.run(mdb.password_reset_tokens.find_one({}))
    assert "token" not in stored and stored["token_hash"] != token
    r = client.post("/api/auth/reset-password", json={"token": token, "new_password": "a-brand-new-password"})
    assert r.status_code == 200, r.text
    user = asyncio.run(mdb.users.find_one({"id": "u1"}))
    assert user.get("password_changed_at")
    # Single use.
    r = client.post("/api/auth/reset-password", json={"token": token, "new_password": "another-password-1"})
    assert r.status_code == 400


def test_sessions_from_before_a_password_change_are_dead():
    changed = datetime.now(timezone.utc).isoformat()
    old_iat = (datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()
    new_iat = datetime.now(timezone.utc).timestamp() + 2
    user = {"password_changed_at": changed}
    assert deps._issued_before_password_change({"iat": old_iat}, user)
    assert not deps._issued_before_password_change({"iat": new_iat}, user)
    assert not deps._issued_before_password_change({"iat": old_iat}, {})


def test_jwt_settings_refuse_weak_configs(monkeypatch):
    import auth_service
    monkeypatch.setenv("JWT_SECRET", "short")
    with pytest.raises(RuntimeError):
        auth_service._jwt_settings()
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    monkeypatch.setenv("JWT_ALGORITHM", "none")
    with pytest.raises(RuntimeError):
        auth_service._jwt_settings()
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    assert auth_service._jwt_settings()[1] == "HS256"


# ---------------------------------------------------------------- email content + replies

def test_candidate_typed_text_is_escaped_in_html_email():
    from email_service import build_email_html
    evil = '<a href="https://phish.example">Verify</a>'
    html = build_email_html(
        f"Hello {evil}\nSee https://ok.example/x\"onmouseover=\"alert(1)",
        {"first_name": evil}, {"recruiter_profile": {"company_name": "Acme & Co"}},
    )
    assert evil not in html
    assert 'href="https://phish.example"' not in html
    assert '"onmouseover' not in html
    assert "Acme &amp; Co" in html


def test_email_replies_off_without_a_reply_domain(monkeypatch):
    import email_replies
    monkeypatch.setattr(email_replies, "REPLY_DOMAIN", "")
    assert email_replies._extract_candidate_id("reply+c1@anything.example") is None


def test_email_from_a_stranger_gets_no_ai_reply(monkeypatch):
    mm = pytest.importorskip("mongomock_motor")
    db = mm.AsyncMongoMockClient()["cgr_test"]
    import email_replies
    import notifications_service
    monkeypatch.setattr(email_replies, "REPLY_DOMAIN", "replies.example.com")
    asyncio.run(db.candidates.insert_one({"id": "c1", "user_id": "u1", "pipeline_id": "p1",
                                          "first_name": "Rowan", "email": "rowan@example.com"}))

    async def _no_ai(*a, **k):
        raise AssertionError("the AI must not run for an unverified sender")

    notes = []

    async def _note(*a, **k):
        notes.append(a)

    monkeypatch.setattr(email_replies, "_generate_reply", _no_ai)
    monkeypatch.setattr(notifications_service, "create_notification", _note)
    res = asyncio.run(email_replies.handle_inbound(
        db, to="reply+c1@replies.example.com", from_email="someone@else.example",
        subject="Re: interview", text="Please cancel my interview",
    ))
    assert res["status"] == "held"
    assert notes, "a human should be told"
    stored = asyncio.run(db.email_reply_messages.find_one({"candidate_id": "c1"}))
    assert stored["unverified_sender"] is True
