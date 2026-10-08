"""Role boundaries: who may change settings, what an analyst may read, which
secrets GET /settings may return, and what a demo account may see of a
candidate's magic links.

Uses an in-memory Mongo (mongomock-motor, see requirements-dev.txt). The
analyst and demo tests sign in for real (a JWT for a user stored in the
database) rather than overriding deps.current_user: the analyst rule lives
inside current_user and the demo scrub is middleware, and an override would
skip both.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/x")
os.environ.setdefault("DB_NAME", "x")
os.environ.setdefault("JWT_SECRET", "unit-test-secret-with-32-plus-chars!!")
os.environ.setdefault("COMPANY_PROFILE_PATH", str(Path(__file__).with_name("company_profile.test.json")))

from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import deps  # noqa: E402
import demo_guard  # noqa: E402
import server  # noqa: E402
from auth_service import create_token  # noqa: E402
from link_redaction import REDACTED_TOKEN, redact_magic_links  # noqa: E402

TOK = "0123456789abcdef0123456789abcdef"      # c1's portal token (uuid4().hex shape)
TOK2 = "fedcba9876543210fedcba9876543210"     # c2's

OWNER = {"id": "u1", "auth_user_id": "u1", "role": "super_admin", "pipeline_ids": []}
RECRUITER = {"id": "u1", "auth_user_id": "r1", "role": "recruiter", "pipeline_ids": ["p1"], "parent_user_id": "u1"}
VIEWER = {"id": "u1", "auth_user_id": "v1", "role": "viewer", "pipeline_ids": ["p1"], "parent_user_id": "u1"}
ANALYST = {"id": "u1", "auth_user_id": "a1", "role": "analyst", "pipeline_ids": ["p1"], "parent_user_id": "u1"}

R = asyncio.run


@pytest.fixture
def mdb(monkeypatch):
    """A fresh in-memory database, swapped into every module that imported
    deps.db (routers, demo_guard, training_sms...)."""
    mm = pytest.importorskip("mongomock_motor")
    db = mm.AsyncMongoMockClient()["cgr_roles"]
    orig = deps.db
    for mod in list(sys.modules.values()):
        try:
            if mod is not None and getattr(mod, "db", None) is orig:
                monkeypatch.setattr(mod, "db", db)
        except Exception:
            pass
    monkeypatch.setattr(demo_guard, "_demo_ids_cache", {"ids": frozenset(), "at": 0.0})
    monkeypatch.setenv("APP_PUBLIC_URL", "https://cgr.example.org")
    monkeypatch.delenv("CG1_BACKEND_URL", raising=False)
    return db


@pytest.fixture
def client():
    server.app.dependency_overrides.clear()
    yield TestClient(server.app, raise_server_exceptions=False)
    server.app.dependency_overrides.clear()


def _as(user):
    server.app.dependency_overrides[deps.current_user] = lambda: user


def _seed(db):
    R(db.users.insert_many([
        {"id": "u1", "email": "owner@example.com", "role": "super_admin"},
        {"id": "a1", "email": "analyst@example.com", "role": "analyst", "parent_user_id": "u1", "pipeline_ids": ["p1"]},
        {"id": "r1", "email": "recruiter@example.com", "role": "recruiter", "parent_user_id": "u1", "pipeline_ids": ["p1"]},
        {"id": "v1", "email": "viewer@example.com", "role": "viewer", "parent_user_id": "u1", "pipeline_ids": ["p1"]},
        {"id": "d1", "email": "demo@example.com", "role": "super_admin", "parent_user_id": "u1", "is_demo": True},
    ]))
    R(db.pipelines.insert_many([
        {"id": "p1", "user_id": "u1", "name": "Downtown", "public_slug": "downtown",
         "appointment_link": "https://meet.example.com/private-room"},
        {"id": "p2", "user_id": "u1", "name": "Riverside", "public_slug": "riverside"},
        {"id": "px", "user_id": "u9", "name": "Other account", "public_slug": "other"},
    ]))
    R(db.candidates.insert_many([
        {"id": "c1", "user_id": "u1", "pipeline_id": "p1", "first_name": "Pat", "last_name": "Sample",
         "email": "pat@example.com", "phone": "+15550100100", "stage": "APPOINTMENT",
         "appointment_at": "2030-01-01T15:00:00+00:00", "public_token": TOK, "added_by_user_id": "r1",
         "created_at": "2026-01-05T10:00:00+00:00"},
        {"id": "c2", "user_id": "u1", "pipeline_id": "p2", "first_name": "Rowan", "email": "rowan@example.com",
         "phone": "+15550100101", "stage": "SCREENING", "public_token": TOK2, "added_by_user_id": "someone",
         "created_at": "2026-01-05T10:00:00+00:00"},
    ]))
    R(db.communications.insert_one({
        "id": "m1", "user_id": "u1", "candidate_id": "c1", "type": "email", "created_at": "2026-01-05T10:05:00+00:00",
        "subject": "Your interview",
        "body": f"Reschedule: https://cgr.example.org/reschedule/{TOK}\nPortal: /applicant/{TOK}",
    }))
    R(db.training_sms_messages.insert_one({
        "id": "s1", "candidate_id": "c1", "direction": "outbound", "timestamp": "2026-01-05T10:06:00+00:00",
        "body": f"Finish by chat: https://cgr.example.org/retry/{TOK}?tab=schedule",
    }))
    R(db.conversations.insert_one({
        "id": "conv1", "user_id": "u1", "candidate_id": "c1", "status": "completed",
        "created_at": "2026-01-05T10:10:00+00:00",
        "transcript": [{"role": "agent", "message": "Hi Pat"}, {"role": "user", "message": "Hi, Pat here"}],
    }))


def _session(uid):
    email = {"u1": "owner@example.com", "a1": "analyst@example.com", "r1": "recruiter@example.com",
             "v1": "viewer@example.com", "d1": "demo@example.com"}[uid]
    return {"Authorization": "Bearer " + create_token(uid, email)}


def _settings_rows(db):
    return sorted(R(db.settings.find({}, {"_id": 0}).to_list(100)), key=lambda d: str(d.get("pipeline_id")))


# ============================================================ 1. settings writes

SETTINGS_WRITES = [
    ("PUT", "/api/settings/recruiter-profile", {"company_name": "PWNED"}),
    ("PUT", "/api/settings/screen-call-agent", {"opening_message": "PWNED"}),
    ("PUT", "/api/settings/applicant-comms", {"templates": {"approval": {"subject": "PWNED", "body": "http://evil.example"}}}),
    ("PUT", "/api/settings/region-language", {"timezone": "Europe/London"}),
    ("PUT", "/api/settings/appointments", {"booking_days_offered": 3}),
    ("PUT", "/api/settings/booking-preferences", {}),
    ("PUT", "/api/settings/custom-form", {"questions": []}),
    ("PUT", "/api/settings/no-show-revival", {}),
    ("PUT", "/api/settings/auto-dialer", {}),
    ("PUT", "/api/settings/starter-template", {"custom_notes": "PWNED"}),
    ("POST", "/api/settings/starter-template/reset", None),
    ("PUT", "/api/settings/ai-stage-prompts", {"ai_stage_prompts": {}}),
]
OWNER_ONLY_EVEN_PER_OFFICE = {"/api/settings/starter-template", "/api/settings/starter-template/reset",
                              "/api/settings/ai-stage-prompts"}

# (who, pipeline_id): read-only roles everywhere; a p1 recruiter on the global
# doc and on an office they aren't assigned to.
REFUSED = [(VIEWER, None), (VIEWER, "p1"), (VIEWER, "p2"),
           (ANALYST, None), (ANALYST, "p1"), (ANALYST, "p2"),
           (RECRUITER, None), (RECRUITER, "p2")]


@pytest.mark.parametrize("method,path,body", SETTINGS_WRITES, ids=[p for _, p, _ in SETTINGS_WRITES])
def test_settings_writes_need_a_writer_and_the_owner_for_global(client, mdb, method, path, body):
    _seed(mdb)
    R(mdb.settings.insert_one({"user_id": "u1", "pipeline_id": None,
                               "applicant_comms": {"templates": {"approval": {"subject": "Welcome", "body": "b"}}}}))
    before = _settings_rows(mdb)
    kw = {"json": body} if body is not None else {}
    for who, pid in REFUSED:
        _as(who)
        r = client.request(method, path, params={"pipeline_id": pid} if pid else {}, **kw)
        assert r.status_code == 403, (who["role"], pid, path, r.status_code, r.text[:120])
    assert _settings_rows(mdb) == before, "a refused write still changed settings"

    # An in-scope recruiter may save their own office (except owner-only sections).
    _as(RECRUITER)
    r = client.request(method, path, params={"pipeline_id": "p1"}, **kw)
    expected = 403 if path in OWNER_ONLY_EVEN_PER_OFFICE else 200
    assert r.status_code == expected, (path, r.status_code, r.text[:200])


def test_owner_writes_global_and_an_unknown_office_is_404(client, mdb):
    _seed(mdb)
    _as(OWNER)
    r = client.put("/api/settings/applicant-comms",
                   json={"templates": {"approval": {"subject": "Hello again", "body": "b"}}})
    assert r.status_code == 200
    doc = R(mdb.settings.find_one({"user_id": "u1", "pipeline_id": None}))
    assert doc["applicant_comms"]["templates"]["approval"]["subject"] == "Hello again"
    # Another account's office, or one that doesn't exist: no stray override doc.
    for pid in ("px", "nope"):
        assert client.put("/api/settings/region-language", params={"pipeline_id": pid}, json={}).status_code == 404
    assert not R(mdb.settings.find_one({"pipeline_id": {"$in": ["px", "nope"]}}))


def test_recruiter_global_save_leaves_every_office_alone(client, mdb):
    """The exact gate probe: a p1 recruiter rewriting the GLOBAL approval email."""
    _seed(mdb)
    _as(RECRUITER)
    r = client.put("/api/settings/applicant-comms",
                   json={"templates": {"approval": {"subject": "PWNED", "body": "http://evil.example"}}})
    assert r.status_code == 403
    assert not R(mdb.settings.find_one({"applicant_comms.templates.approval.subject": "PWNED"}))


# ============================================================ 2. analyst = figures only

def _fill(path):
    values = {"candidate_id": "c1", "pipeline_id": "p1", "conversation_id": "conv1", "job_id": "j1"}
    out = path
    for name, val in values.items():
        out = out.replace("{" + name + "}", val)
    import re
    return re.sub(r"\{[^}]+\}", "x", out)


def _uses_current_user(dependant):
    for sub in dependant.dependencies:
        if sub.call is deps.current_user or _uses_current_user(sub):
            return True
    return False


# Routes that sign the caller in by calling current_user themselves (not as a
# dependency). They pass the request, so the same rule applies.
SELF_AUTHENTICATING = [
    ("GET", "/api/candidates/{candidate_id}/sms-thread"),
    ("POST", "/api/candidates/{candidate_id}/sms/resend"),
]
# Never requested in the walk: an endless event stream would hang the test if
# the rule ever broke. Checked statically below instead.
NOT_REQUESTED = {("GET", "/api/events/stream")}


def test_every_analyst_route_exists():
    have = {(m, r.path) for r in server.app.routes if isinstance(r, APIRoute) for m in r.methods}
    assert set(deps.ANALYST_ROUTES) <= have, set(deps.ANALYST_ROUTES) - have


def test_every_signed_in_route_refuses_analysts_unless_listed(client, mdb):
    """Walks every route that signs the caller in. As a real analyst session,
    anything not in deps.ANALYST_ROUTES must answer 403 from the analyst rule,
    before the handler runs. A new candidate route is covered automatically."""
    _seed(mdb)
    h = _session("a1")
    walked, leaks = 0, []
    targets = []
    for r in server.app.routes:
        if not isinstance(r, APIRoute) or not _uses_current_user(r.dependant):
            continue
        targets += [(m, r.path) for m in sorted(r.methods)]
    targets += SELF_AUTHENTICATING
    for method, path in targets:
        if deps.analyst_may_call(method, path):
            continue
        if (method, path) in NOT_REQUESTED:
            continue
        resp = client.request(method, _fill(path), headers=h)
        walked += 1
        if resp.status_code != 403 or resp.json().get("detail") != deps.ANALYST_REFUSED:
            leaks.append((method, path, resp.status_code, resp.text[:80]))
    assert not leaks, leaks
    assert walked > 100  # the walk really covered the API
    for method, path in NOT_REQUESTED:
        assert not deps.analyst_may_call(method, path)


CANDIDATE_READS = [
    "/api/candidates", "/api/candidates?search=Pat", "/api/candidates/c1",
    "/api/candidates/c1/transcript", "/api/candidates/c1/conversations",
    "/api/candidates/c1/communications", "/api/candidates/c1/email-thread",
    "/api/candidates/c1/sms-thread", "/api/conversations/conv1/audio",
    "/api/calendar/appointments", "/api/calendar/outcome-due", "/api/duplicates",
    "/api/sms/inbox", "/api/sms/inbox/thread?candidate_id=c1", "/api/needs-attention",
    "/api/notifications", "/api/intelligence/export-csv", "/api/metrics/speed-to-contact",
    "/api/revival/preview?pipeline_id=p1", "/api/pipelines/p1/call-queue",
]


@pytest.mark.parametrize("path", CANDIDATE_READS)
def test_analyst_gets_no_candidate_data(client, mdb, path):
    _seed(mdb)
    r = client.get(path, headers=_session("a1"))
    assert r.status_code == 403, (path, r.status_code)
    for pii in ("pat@example.com", "+15550100100", "Sample", TOK):
        assert pii not in r.text


def test_analyst_still_gets_the_figures(client, mdb):
    _seed(mdb)
    h = _session("a1")
    me = client.get("/api/auth/me", headers=h)
    assert me.status_code == 200 and me.json()["role"] == "analyst"
    dash = client.get("/api/intelligence/dashboard", headers=h)
    assert dash.status_code == 200, dash.text[:200]
    assert dash.json()["kpis"]["applicants_added"] == 1  # c1 only: p1 is the analyst's office
    for path in ("/api/intelligence/cohort-week", "/api/intelligence/activity-week",
                 "/api/intelligence/funnel-report?pipeline_id=p1"):
        r = client.get(path, headers=h)
        assert r.status_code == 200, (path, r.status_code, r.text[:200])
        assert "pat@example.com" not in r.text and TOK not in r.text
    # The office picker: names, no meeting links or phone numbers.
    pipes = client.get("/api/pipelines", headers=h)
    assert pipes.status_code == 200 and [p["name"] for p in pipes.json()] == ["Downtown"]
    assert "private-room" not in pipes.text
    # Settings: the time zone and nothing else.
    s = client.get("/api/settings", headers=h)
    assert s.status_code == 200 and set(s.json()) == {"pipeline_id", "region_language"}


def test_other_roles_are_untouched_by_the_analyst_rule(client, mdb):
    _seed(mdb)
    assert client.get("/api/candidates/c1", headers=_session("r1")).status_code == 200
    assert client.get("/api/candidates/c1/communications", headers=_session("u1")).status_code == 200


def test_direct_current_user_call_without_a_request_refuses_analysts(mdb):
    _seed(mdb)
    payload = {"sub": "a1"}
    with pytest.raises(deps.HTTPException) as e:
        R(deps.current_user(payload))
    assert e.value.status_code == 403
    assert R(deps.current_user({"sub": "r1"}))["role"] == "recruiter"


# ============================================================ 3. secrets in GET /settings

ZAP = "zap-secret-token-1234567890"
CG1 = "cg1-shared-secret-abcdef"


def _seed_secrets(db):
    R(db.settings.insert_one({
        "user_id": "u1", "pipeline_id": None, "zapier_webhook_token": ZAP,
        "integrations": {"cg1_enabled": True, "cg1_webhook_url": "https://field.example.com/hook",
                         "cg1_webhook_secret": CG1},
        "region_language": {"timezone": "Europe/London"},
    }))
    R(db.settings.insert_one({"user_id": "u1", "pipeline_id": "p1", "zapier_webhook_token": ZAP,
                              "integrations": {"cg1_webhook_secret": CG1}}))


@pytest.mark.parametrize("uid", ["u1", "r1", "v1", "a1", "d1"])
def test_get_settings_never_returns_a_credential(client, mdb, uid):
    _seed(mdb)
    _seed_secrets(mdb)
    for params in ({}, {"pipeline_id": "p1"}):
        r = client.get("/api/settings", params=params, headers=_session(uid))
        assert r.status_code == 200, (uid, r.status_code)
        assert ZAP not in r.text and CG1 not in r.text, (uid, params)
    if uid == "u1":
        body = client.get("/api/settings", headers=_session(uid)).json()
        assert body["integrations"]["cg1_webhook_secret"] == ""
        assert body["integrations"]["cg1_webhook_secret_set"] is True
        assert body["zapier_webhook_token"] == "" and body["zapier_webhook_token_set"] is True


def test_strip_settings_secrets_covers_any_depth():
    doc = {"zapier_webhook_token": "a", "nested": {"partner_api_key": "b", "list": [{"x_secret": "c"}]},
           "max_tokens": 5, "keep": "visible", "integrations": {"cg1_webhook_secret": ""}}
    out = deps.strip_settings_secrets(doc)
    assert out["zapier_webhook_token"] == "" and out["zapier_webhook_token_set"] is True
    assert out["nested"]["partner_api_key"] == "" and out["nested"]["list"][0]["x_secret"] == ""
    assert out["integrations"]["cg1_webhook_secret_set"] is False
    assert out["max_tokens"] == 5 and out["keep"] == "visible"


def test_cg1_secret_is_write_only(client, mdb):
    _seed(mdb)
    _seed_secrets(mdb)
    _as(OWNER)
    # The screen sends it back blank: the saved secret stays.
    r = client.put("/api/settings/integrations", json={"cg1_enabled": True, "cg1_webhook_url": "https://field.example.com/hook2",
                                                        "cg1_webhook_secret": ""})
    assert r.status_code == 200 and CG1 not in r.text
    g = R(mdb.settings.find_one({"user_id": "u1", "pipeline_id": None}))
    assert g["integrations"]["cg1_webhook_secret"] == CG1
    assert g["integrations"]["cg1_webhook_url"] == "https://field.example.com/hook2"
    # A typed value replaces it.
    client.put("/api/settings/integrations", json={"cg1_enabled": True, "cg1_webhook_url": "https://field.example.com/hook2",
                                                   "cg1_webhook_secret": "new-one"})
    g = R(mdb.settings.find_one({"user_id": "u1", "pipeline_id": None}))
    assert g["integrations"]["cg1_webhook_secret"] == "new-one"


def test_override_docs_are_cloned_without_credentials(client, mdb):
    _seed(mdb)
    R(mdb.settings.insert_one({"user_id": "u1", "pipeline_id": None, "zapier_webhook_token": ZAP,
                               "integrations": {"cg1_webhook_secret": CG1}}))
    _as(RECRUITER)
    assert client.put("/api/settings/region-language", params={"pipeline_id": "p1"}, json={}).status_code == 200
    override = R(mdb.settings.find_one({"user_id": "u1", "pipeline_id": "p1"}, {"_id": 0}))
    assert override and "zapier_webhook_token" not in override and "integrations" not in override


def test_rotating_the_intake_token_kills_every_copy(client, mdb):
    """Before: an override doc cloned the token, regenerate changed one doc,
    and the old URL kept working from the copy."""
    _seed(mdb)
    _seed_secrets(mdb)  # global and p1 override both carry ZAP
    assert client.get(f"/api/webhooks/zapier/{ZAP}/pipelines").status_code == 200
    _as(OWNER)
    new = client.post("/api/integrations/zapier-token/regenerate").json()["token"]
    server.app.dependency_overrides.clear()
    assert client.get(f"/api/webhooks/zapier/{ZAP}/pipelines").status_code == 403
    assert client.post(f"/api/webhooks/zapier/{ZAP}", json={"name": "Rowan Sample"}).status_code == 403
    assert client.get(f"/api/webhooks/zapier/{new}/pipelines").status_code == 200
    rows = R(mdb.settings.find({"zapier_webhook_token": {"$exists": True}}, {"_id": 0, "pipeline_id": 1}).to_list(10))
    assert rows == [{"pipeline_id": None}]
    # Owner-only endpoint returns the live token.
    _as(OWNER)
    assert client.get("/api/integrations/zapier-token").json()["token"] == new


def test_a_pre_existing_install_keeps_its_token_until_rotated(client, mdb):
    """An install whose live token sits only on an override doc keeps working,
    and the token moves to the global doc."""
    _seed(mdb)
    R(mdb.settings.insert_one({"user_id": "u1", "pipeline_id": None}))
    R(mdb.settings.insert_one({"user_id": "u1", "pipeline_id": "p1", "zapier_webhook_token": "legacy-token-123"}))
    assert client.get("/api/webhooks/zapier/legacy-token-123/pipelines").status_code == 200
    g = R(mdb.settings.find_one({"user_id": "u1", "pipeline_id": None}))
    o = R(mdb.settings.find_one({"user_id": "u1", "pipeline_id": "p1"}))
    assert g.get("zapier_webhook_token") == "legacy-token-123" and "zapier_webhook_token" not in o


# ============================================================ 4. demo accounts and magic links

def test_redact_magic_links_shapes():
    cases = [
        f"https://cgr.example.org/applicant/{TOK}",
        f"/reschedule/{TOK}",
        f"https://cgr.example.org/retry/{TOK}?tab=schedule",
        f"/api/public/applicant/{TOK}/book",
        f"/form/{TOK}", f"/portal/{TOK}",
        f"https%3A%2F%2Fcgr.example.org%2Freschedule%2F{TOK}",
        "see https://x.example/retry/abc_DEF-123 now",
    ]
    for text in cases:
        out = redact_magic_links(text)
        assert TOK not in out and "abc_DEF-123" not in out, (text, out)
        assert REDACTED_TOKEN in out
    for text in ("/apply/downtown", "/applicant/", "/applicant/short", "custom-form/abcdefghij"):
        assert redact_magic_links(text) == text
    assert TOK not in redact_magic_links(f"code {TOK}", [TOK])


def test_scrub_credentials_takes_tokens_out_of_strings():
    resp = {"candidates": [{"public_token": TOK, "note": f"their code is {TOK}"}],
            "messages": [{"body": f"Pick a time: https://cgr.example.org/reschedule/{TOK2}"}]}
    out = demo_guard.scrub_credentials(resp)
    text = json.dumps(out)
    assert TOK not in text and TOK2 not in text
    assert out["candidates"][0]["public_token"] == ""


def test_demo_never_sees_a_portal_token(client, mdb):
    _seed(mdb)
    h = _session("d1")
    preview = client.post("/api/templates/preview", headers=h,
                          json={"body": "[Portal Link] [Reschedule URL] [Retry Link]", "candidate_id": "c1"})
    assert preview.status_code == 200, preview.text[:200]
    assert TOK not in preview.text and "/applicant/" + REDACTED_TOKEN in preview.text
    for path in ("/api/candidates", "/api/candidates/c1", "/api/candidates/c1/communications",
                 "/api/candidates/c1/sms-thread", "/api/candidates/c1/email-thread", "/api/calendar/appointments"):
        r = client.get(path, headers=h)
        assert r.status_code == 200, (path, r.status_code)
        assert TOK not in r.text, path


def test_owner_preview_still_has_the_real_link(client, mdb):
    """Control for the test above: the token is there for a real user, so its
    absence for the demo account is the scrub working."""
    _seed(mdb)
    r = client.post("/api/templates/preview", headers=_session("u1"),
                    json={"body": "[Portal Link]", "candidate_id": "c1"})
    assert r.status_code == 200 and TOK in r.text


def test_preview_of_a_candidate_needs_access_to_that_candidate(client, mdb):
    _seed(mdb)
    _as(RECRUITER)  # assigned p1; c2 is in p2
    assert client.post("/api/templates/preview", json={"body": "[Portal Link]", "candidate_id": "c2"}).status_code == 403
    assert client.post("/api/templates/preview", json={"body": "[Portal Link]", "candidate_id": "c1"}).status_code == 200
    _as(VIEWER)  # c1 is in their office but they didn't add it
    assert client.post("/api/templates/preview", json={"body": "x", "candidate_id": "c1"}).status_code == 404
    _as(OWNER)
    assert client.post("/api/templates/preview", json={"body": "x", "candidate_id": {"$ne": ""}}).status_code == 400


def test_connector_clean_strips_tokens_from_text():
    from claude_connector import cgrecruit as conn
    out = conn._clean({"messages_sent": [{"body": f"Reschedule: https://cgr.example.org/reschedule/{TOK}"}],
                       "public_token": TOK})
    assert TOK not in json.dumps(out)


# ============================================================ 5. bulk and side routes

@pytest.mark.parametrize("who", [VIEWER, ANALYST, RECRUITER], ids=["viewer", "analyst", "recruiter"])
def test_reengage_by_text_needs_a_writer_in_scope(client, mdb, who):
    _seed(mdb)
    _as(who)
    r = client.post("/api/screening/reengage-by-text", json={"pipeline_id": "p2", "dry_run": True})
    assert r.status_code == 403 and "Rowan" not in r.text
    _as(OWNER)
    assert client.post("/api/screening/reengage-by-text", json={"pipeline_id": {"$ne": ""}}).status_code == 400


def test_email_intake_aliases_are_scoped(client, mdb):
    _seed(mdb)
    R(mdb.employee_email_aliases.insert_one({"id": "al2", "user_id": "u1", "pipeline_id": "p2",
                                             "employee_name": "Sam", "slug": "sam-riverside"}))
    body = {"pipeline_id": "p1", "employee_name": "Kim", "slug": "kim-downtown"}
    for who in (VIEWER, ANALYST):
        _as(who)
        assert client.post("/api/email-intake/aliases", json=body).status_code == 403
        assert client.delete("/api/email-intake/aliases/al2").status_code == 403
    _as(RECRUITER)
    assert client.post("/api/email-intake/aliases", json={**body, "pipeline_id": "p2"}).status_code == 403
    assert client.delete("/api/email-intake/aliases/al2").status_code == 403
    assert R(mdb.employee_email_aliases.find_one({"id": "al2"}))
    assert client.post("/api/email-intake/aliases", json=body).status_code == 200
    # No fallback to another account's pipeline.
    _as(OWNER)
    assert client.get("/api/email-intake/pipeline/px").status_code == 404


def test_needs_attention_dismiss_is_scoped(client, mdb):
    _seed(mdb)
    R(mdb.link_only_applications.insert_one({"id": "lo2", "user_id": "u1", "pipeline_id": "p2", "status": "open"}))
    _as(VIEWER)
    assert client.post("/api/needs-attention/dismiss/c1").status_code == 403
    _as(RECRUITER)
    assert client.post("/api/needs-attention/dismiss/lo2").status_code == 403
    assert client.post("/api/needs-attention/dismiss/c2").status_code == 403
    assert R(mdb.link_only_applications.find_one({"id": "lo2"}))["status"] == "open"
    assert client.post("/api/needs-attention/dismiss/c1").status_code == 200


# ============================================================ 6. public-endpoint abuse limits and email senders

def test_sendgrid_sender_verdict():
    import email_replies as er
    v = er.sendgrid_sender_verdict
    assert v(None, None, "rowan@example.com") is None              # no verdicts sent: not SendGrid
    assert v("pass", "none", "rowan@example.com") is True
    assert v("softfail", "{@example.com : pass}", "rowan@example.com") is True
    assert v("fail", "{@mail.example.com : pass}", "rowan@example.com") is False   # another domain signed it
    assert v("fail", "{@example.com : fail}", "rowan@example.com") is False
    assert v("none", "none", "rowan@example.com") is False


def test_spoofed_candidate_address_gets_no_ai_reply(monkeypatch):
    """The right From address but SendGrid says neither SPF nor DKIM passed:
    held for a human, the AI never runs (it can cancel or move an interview)."""
    mm = pytest.importorskip("mongomock_motor")
    db = mm.AsyncMongoMockClient()["cgr_roles_mail"]
    import email_replies
    import notifications_service
    monkeypatch.setattr(email_replies, "REPLY_DOMAIN", "replies.example.com")
    R(db.candidates.insert_one({"id": "c1", "user_id": "u1", "pipeline_id": "p1",
                                "first_name": "Rowan", "email": "rowan@example.com"}))

    async def _no_ai(*a, **k):
        raise AssertionError("the AI must not run for a sender that failed SPF and DKIM")

    notes = []

    async def _note(*a, **k):
        notes.append(a)

    monkeypatch.setattr(email_replies, "_generate_reply", _no_ai)
    monkeypatch.setattr(notifications_service, "create_notification", _note)
    res = R(email_replies.handle_inbound(
        db, to="reply+c1@replies.example.com", from_email="rowan@example.com",
        subject="Re: interview", text="Please cancel my interview", sender_authenticated=False,
    ))
    assert res["status"] == "held" and "SPF" in res["reason"]
    assert notes
    assert R(db.email_reply_messages.find_one({"candidate_id": "c1"}))["unverified_sender"] is True


def test_public_apply_is_limited_per_ip(client, monkeypatch):
    import rate_limit
    rate_limit.reset()
    try:
        ip = "203.0.113.7"
        for _ in range(20):
            assert rate_limit.allow(f"apply:{ip}", 20, 900)
        # Over the limit: refused before anything is filed, texted or called.
        r = client.post("/api/public/apply", headers={"X-Forwarded-For": ip},
                        json={"pipeline_id": "p1", "first_name": "A", "email": "a@example.com", "phone": "+15550100100"})
        assert r.status_code == 429
        # Another connection is unaffected (refused for its own reason: no such pipeline).
        r = client.post("/api/public/apply", headers={"X-Forwarded-For": "198.51.100.9"},
                        json={"pipeline_id": {"$ne": None}, "first_name": "A", "email": "a@example.com", "phone": "1"})
        assert r.status_code == 400
        # The window slides.
        assert rate_limit.allow(f"apply:{ip}", 20, 900, now=__import__("time").time() + 901)
    finally:
        rate_limit.reset()


def test_retry_chat_is_bounded(client, mdb, monkeypatch):
    from datetime import datetime, timedelta, timezone
    import routes.retry as retry
    _seed(mdb)

    async def _no_llm(*a, **k):
        raise AssertionError("the LLM must not be called past the limit")

    monkeypatch.setattr(retry, "resolve_settings", _no_llm)  # first thing the handler does after the guards
    now = datetime.now(timezone.utc)
    busy = [{"role": "applicant", "channel": "retry", "text": "hi",
             "at": (now - timedelta(minutes=i % 50)).isoformat()} for i in range(retry.RETRY_CHAT_TURNS_PER_HOUR)]
    R(mdb.candidates.update_one({"id": "c2"}, {"$set": {"chat_log": busy}}))
    r = client.post(f"/api/public/retry/{TOK2}/chat", json={"text": "one more"})
    assert r.status_code == 200 and "break" in r.json()["reply"]
    assert len(R(mdb.candidates.find_one({"id": "c2"}))["chat_log"]) == len(busy)  # nothing stored
    # Old turns don't count; a huge message is cut, and a non-string refused.
    old = [{**m, "at": (now - timedelta(hours=2)).isoformat()} for m in busy]
    assert not retry._retry_chat_over_limit(old, now)
    assert retry._retry_chat_over_limit([{}] * retry.RETRY_CHAT_MAX_LOG, now)
    assert client.post(f"/api/public/retry/{TOK2}/chat", json={"text": {"$ne": ""}}).status_code == 400
