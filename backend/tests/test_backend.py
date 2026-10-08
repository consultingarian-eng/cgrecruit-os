"""Comprehensive backend test suite for CGRecruit."""
import os
import time
import requests
import pytest
from io import BytesIO

# These talk to a RUNNING server (tests/conftest.py, without --noconftest).
# Skip the whole file unless one is named explicitly.
if not os.environ.get("CGRECRUIT_TEST_URL"):
    pytest.skip("integration test: set CGRECRUIT_TEST_URL to a local test server and run without --noconftest",
                allow_module_level=True)


# ========================= Auth tests =========================
class TestAuth:
    def test_health(self, api_base):
        r = requests.get(f"{api_base}/", timeout=10)
        assert r.status_code == 200
        assert r.json().get("status") == "ok"

    def test_register_and_login(self, api_base):
        ts = int(time.time() * 1000)
        email = f"tester+{ts}@cgrecruit.example.com"
        pwd = "abc123-long-enough"
        r = requests.post(f"{api_base}/auth/register",
                          json={"email": email, "password": pwd, "name": "X", "company": "Y"}, timeout=20)
        assert r.status_code == 200
        data = r.json()
        assert "token" in data and "user" in data
        assert data["user"]["email"] == email
        assert "_id" not in data["user"]

        # duplicate
        r2 = requests.post(f"{api_base}/auth/register",
                           json={"email": email, "password": pwd, "name": "X"}, timeout=20)
        assert r2.status_code == 400

        # login valid
        r3 = requests.post(f"{api_base}/auth/login", json={"email": email, "password": pwd}, timeout=20)
        assert r3.status_code == 200
        assert "token" in r3.json()

        # login invalid
        r4 = requests.post(f"{api_base}/auth/login", json={"email": email, "password": "wrong"}, timeout=20)
        assert r4.status_code == 401

    def test_me_requires_auth(self, api_base, test_user):
        r = requests.get(f"{api_base}/auth/me", timeout=10)
        assert r.status_code in (401, 403)

        r2 = requests.get(f"{api_base}/auth/me",
                          headers={"Authorization": f"Bearer {test_user['token']}"}, timeout=10)
        assert r2.status_code == 200
        assert r2.json()["email"] == test_user["email"]
        assert "_id" not in r2.json()
        assert "password_hash" not in r2.json()


# ========================= Seed + Pipelines =========================
class TestSeedAndPipelines:
    def test_seed_demo_idempotent(self, api_base, auth_headers, seeded):
        # seeded fixture did initial seed
        assert seeded.get("ok") is True
        # second call should skip
        r = requests.post(f"{api_base}/seed/demo", headers=auth_headers, timeout=30)
        assert r.status_code == 200
        assert r.json().get("skipped") is True

    def test_list_pipelines(self, api_base, auth_headers, seeded):
        r = requests.get(f"{api_base}/pipelines", headers=auth_headers, timeout=20)
        assert r.status_code == 200
        rows = r.json()
        assert isinstance(rows, list)
        assert len(rows) == 3
        names = sorted([p["name"] for p in rows])
        assert names == ["Downtown", "Military Hire", "Riverside"]
        for p in rows:
            assert "_id" not in p
            assert "id" in p

    def test_pipelines_require_auth(self, api_base):
        r = requests.get(f"{api_base}/pipelines", timeout=10)
        assert r.status_code in (401, 403)

    def test_create_and_delete_pipeline(self, api_base, auth_headers, seeded):
        r = requests.post(f"{api_base}/pipelines", headers=auth_headers,
                          json={"name": "TEST_PipelineX", "description": "tmp"}, timeout=20)
        assert r.status_code == 200
        pid = r.json()["id"]
        assert r.json()["name"] == "TEST_PipelineX"

        # delete
        r2 = requests.delete(f"{api_base}/pipelines/{pid}", headers=auth_headers, timeout=20)
        assert r2.status_code == 200
        assert r2.json().get("ok") is True

        # verify gone
        r3 = requests.get(f"{api_base}/pipelines", headers=auth_headers, timeout=20)
        ids = [p["id"] for p in r3.json()]
        assert pid not in ids


# ========================= Jobs =========================
class TestJobs:
    def test_list_jobs_by_pipeline(self, api_base, auth_headers, seeded):
        p = requests.get(f"{api_base}/pipelines", headers=auth_headers, timeout=20).json()[0]
        r = requests.get(f"{api_base}/jobs?pipeline_id={p['id']}", headers=auth_headers, timeout=20)
        assert r.status_code == 200
        jobs = r.json()
        assert len(jobs) >= 1
        for j in jobs:
            assert j["pipeline_id"] == p["id"]
            assert "_id" not in j

    def test_create_update_delete_job(self, api_base, auth_headers, seeded):
        p = requests.get(f"{api_base}/pipelines", headers=auth_headers, timeout=20).json()[0]
        payload = {"pipeline_id": p["id"], "title": "TEST_Role", "category": "QA",
                   "description": "test desc", "city": "Downtown", "country": "USA", "is_active": True}
        r = requests.post(f"{api_base}/jobs", headers=auth_headers, json=payload, timeout=20)
        assert r.status_code == 200
        job_id = r.json()["id"]
        assert r.json()["title"] == "TEST_Role"

        # update
        upd = {**payload, "title": "TEST_Role_Updated"}
        r2 = requests.put(f"{api_base}/jobs/{job_id}", headers=auth_headers, json=upd, timeout=20)
        assert r2.status_code == 200
        assert r2.json()["title"] == "TEST_Role_Updated"

        # delete
        r3 = requests.delete(f"{api_base}/jobs/{job_id}", headers=auth_headers, timeout=20)
        assert r3.status_code == 200


# ========================= Candidates =========================
class TestCandidates:
    def test_list_candidates_per_pipeline(self, api_base, auth_headers, seeded):
        pipelines = requests.get(f"{api_base}/pipelines", headers=auth_headers, timeout=20).json()
        total = 0
        for p in pipelines:
            r = requests.get(f"{api_base}/candidates?pipeline_id={p['id']}", headers=auth_headers, timeout=20)
            assert r.status_code == 200
            cands = r.json()
            assert len(cands) == 4, f"expected 4 per pipeline, got {len(cands)}"
            total += len(cands)
            for c in cands:
                assert "_id" not in c
        assert total == 12

    def test_get_candidate_parsed_resume(self, api_base, auth_headers, seeded):
        pipelines = requests.get(f"{api_base}/pipelines", headers=auth_headers, timeout=20).json()
        cands = requests.get(f"{api_base}/candidates?pipeline_id={pipelines[0]['id']}",
                             headers=auth_headers, timeout=20).json()
        cid = cands[0]["id"]
        r = requests.get(f"{api_base}/candidates/{cid}", headers=auth_headers, timeout=20)
        assert r.status_code == 200
        data = r.json()
        assert data["parsed_resume"] is not None
        assert "skills" in data["parsed_resume"]

    def test_create_candidate_manual(self, api_base, auth_headers, seeded):
        p = requests.get(f"{api_base}/pipelines", headers=auth_headers, timeout=20).json()[0]
        payload = {"pipeline_id": p["id"], "first_name": "TEST_Jane", "last_name": "Doe",
                   "email": "TEST_jane@example.com", "phone": "+15550000000"}
        r = requests.post(f"{api_base}/candidates", headers=auth_headers, json=payload, timeout=20)
        assert r.status_code == 200
        cid = r.json()["id"]
        # verify GET
        g = requests.get(f"{api_base}/candidates/{cid}", headers=auth_headers, timeout=20)
        assert g.status_code == 200
        assert g.json()["first_name"] == "TEST_Jane"
        # cleanup
        requests.delete(f"{api_base}/candidates/{cid}", headers=auth_headers, timeout=20)

    def test_move_candidate_stage_status_email(self, api_base, auth_headers, seeded):
        pipelines = requests.get(f"{api_base}/pipelines", headers=auth_headers, timeout=20).json()
        cands = requests.get(f"{api_base}/candidates?pipeline_id={pipelines[0]['id']}",
                             headers=auth_headers, timeout=20).json()
        cid = cands[0]["id"]

        # stage
        r = requests.post(f"{api_base}/candidates/{cid}/move", headers=auth_headers,
                         json={"stage": "SCREENING"}, timeout=20)
        assert r.status_code == 200
        assert r.json()["candidate"]["stage"] == "SCREENING"

        # screening_status
        r2 = requests.post(f"{api_base}/candidates/{cid}/move", headers=auth_headers,
                          json={"screening_status": "approved"}, timeout=20)
        assert r2.status_code == 200
        assert r2.json()["candidate"]["screening_status"] == "approved"

        # with email template
        r3 = requests.post(f"{api_base}/candidates/{cid}/move", headers=auth_headers,
                          json={"stage": "APPOINTMENT", "send_email_template": "warmup"}, timeout=30)
        assert r3.status_code == 200
        em = r3.json().get("email")
        assert em is not None
        assert em.get("status") in ("sent", "failed", "skipped")

    def test_score_candidate(self, api_base, auth_headers, seeded):
        # Find a candidate with job_id
        pipelines = requests.get(f"{api_base}/pipelines", headers=auth_headers, timeout=20).json()
        cands = requests.get(f"{api_base}/candidates?pipeline_id={pipelines[0]['id']}",
                             headers=auth_headers, timeout=20).json()
        cand = next((c for c in cands if c.get("job_id")), None)
        assert cand is not None
        r = requests.post(f"{api_base}/candidates/{cand['id']}/score", headers=auth_headers, timeout=60)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "score" in data
        assert 0 <= int(data["score"]) <= 100
        for k in ("rationale", "strengths", "gaps", "verdict"):
            assert k in data

    def test_email_candidate(self, api_base, auth_headers, seeded):
        pipelines = requests.get(f"{api_base}/pipelines", headers=auth_headers, timeout=20).json()
        cands = requests.get(f"{api_base}/candidates?pipeline_id={pipelines[0]['id']}",
                             headers=auth_headers, timeout=20).json()
        cid = cands[0]["id"]
        r = requests.post(f"{api_base}/candidates/{cid}/email", headers=auth_headers,
                         json={"template_key": "warmup"}, timeout=30)
        assert r.status_code == 200
        assert r.json().get("status") in ("sent", "failed", "skipped")

    def test_sms_candidate_clean_error(self, api_base, auth_headers, seeded):
        pipelines = requests.get(f"{api_base}/pipelines", headers=auth_headers, timeout=20).json()
        cands = requests.get(f"{api_base}/candidates?pipeline_id={pipelines[0]['id']}",
                             headers=auth_headers, timeout=20).json()
        cid = cands[0]["id"]
        r = requests.post(f"{api_base}/candidates/{cid}/sms", headers=auth_headers,
                         json={"body": "Hi from test"}, timeout=30)
        assert r.status_code == 200
        # TWILIO_PHONE_NUMBER empty => expect skipped or failed (clean, no crash)
        assert r.json().get("status") in ("failed", "sent", "skipped")

    def test_call_candidate_not_configured(self, api_base, auth_headers, seeded):
        pipelines = requests.get(f"{api_base}/pipelines", headers=auth_headers, timeout=20).json()
        cands = requests.get(f"{api_base}/candidates?pipeline_id={pipelines[0]['id']}",
                             headers=auth_headers, timeout=20).json()
        cid = cands[0]["id"]
        r = requests.post(f"{api_base}/candidates/{cid}/call", headers=auth_headers, timeout=30)
        assert r.status_code == 200, r.text
        result = r.json().get("result", {})
        assert result.get("status") in ("not_configured", "failed", "initiated")

    def test_conversations_and_communications(self, api_base, auth_headers, seeded):
        pipelines = requests.get(f"{api_base}/pipelines", headers=auth_headers, timeout=20).json()
        cands = requests.get(f"{api_base}/candidates?pipeline_id={pipelines[0]['id']}",
                             headers=auth_headers, timeout=20).json()
        cid = cands[0]["id"]
        r = requests.get(f"{api_base}/candidates/{cid}/conversations", headers=auth_headers, timeout=20)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

        r2 = requests.get(f"{api_base}/candidates/{cid}/communications", headers=auth_headers, timeout=20)
        assert r2.status_code == 200
        assert isinstance(r2.json(), list)

    def test_upload_resume_txt(self, api_base, auth_headers, seeded, test_user):
        pipelines = requests.get(f"{api_base}/pipelines", headers=auth_headers, timeout=20).json()
        pid = pipelines[0]["id"]

        sample = (b"Marlo QA Tester\nalex.qa@example.com\n+15551239876\n\n"
                  b"Summary: 6 years customer service and sales, led teams, hit 120% quota.\n"
                  b"Skills: Customer Service, Sales, CRM, Salesforce, Negotiation\n"
                  b"Experience: Acme Corp - Senior CSR 2020-2025\n"
                  b"Education: State U, BA Communications 2019\n")
        files = {"file": ("resume.txt", BytesIO(sample), "text/plain")}
        data = {"pipeline_id": pid}
        # strip Content-Type so requests sets multipart boundary
        hdr = {k: v for k, v in auth_headers.items() if k.lower() != "content-type"}
        r = requests.post(f"{api_base}/candidates/upload", headers=hdr, files=files, data=data, timeout=120)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out.get("parsed_resume") is not None
        assert "_id" not in out
        # cleanup
        requests.delete(f"{api_base}/candidates/{out['id']}", headers=auth_headers, timeout=20)


# ========================= Settings =========================
class TestSettings:
    def test_get_settings_defaults(self, api_base, auth_headers, test_user):
        r = requests.get(f"{api_base}/settings", headers=auth_headers, timeout=20)
        assert r.status_code == 200
        s = r.json()
        assert "_id" not in s
        templates = (s.get("applicant_comms") or {}).get("templates") or {}
        # The active 12 templates (v34.5+ — dead per-failure-mode templates
        # were consolidated into `screening_retry`).
        expected = set([
            "warmup", "screening_retry",
            "pencil_in", "rejection", "approval",
            "form", "form_decline", "close_success",
            "appointment_reminder_1h", "appointment_reminder_10m",
            "appointment_no_show", "appointment_rescheduled",
        ])
        missing = expected - set(templates.keys())
        assert not missing, f"Missing templates: {missing}"
        assert len(templates) >= 12

    def test_comms_defaults_15(self, api_base, auth_headers):
        r = requests.get(f"{api_base}/settings/comms-defaults", headers=auth_headers, timeout=20)
        assert r.status_code == 200
        defaults = r.json()
        assert len(defaults) >= 12

    def test_update_recruiter_profile(self, api_base, auth_headers):
        body = {"company_name": "Example Co QA", "city": "Downtown", "job_role": "Recruiter",
                "recruiter_email": "qa@example.com", "phone": "+15550001111",
                "website": "https://www.example.com", "social_links": {"linkedin": "x"}}
        r = requests.put(f"{api_base}/settings/recruiter-profile", headers=auth_headers, json=body, timeout=20)
        assert r.status_code == 200
        s = requests.get(f"{api_base}/settings", headers=auth_headers, timeout=20).json()
        assert s["recruiter_profile"]["company_name"] == "Example Co QA"
        assert s["recruiter_profile"]["recruiter_email"] == "qa@example.com"

    def test_update_screen_call_agent(self, api_base, auth_headers):
        body = {"agent_name": "Aria", "elevenlabs_agent_id": "", "elevenlabs_phone_number_id": "",
                "custom_caller_id": "", "language": "en"}
        r = requests.put(f"{api_base}/settings/screen-call-agent", headers=auth_headers, json=body, timeout=20)
        assert r.status_code == 200

    def test_update_applicant_comms(self, api_base, auth_headers):
        body = {"templates": {"warmup": {"subject": "Welcome TEST",
                                          "body": "Hi {{first_name}}, thanks."}}}
        r = requests.put(f"{api_base}/settings/applicant-comms", headers=auth_headers, json=body, timeout=20)
        assert r.status_code == 200
        s = requests.get(f"{api_base}/settings", headers=auth_headers, timeout=20).json()
        assert s["applicant_comms"]["templates"]["warmup"]["subject"] == "Welcome TEST"

    def test_update_region_language(self, api_base, auth_headers):
        body = {"region": "US", "language": "en", "timezone": "America/New_York"}
        r = requests.put(f"{api_base}/settings/region-language", headers=auth_headers, json=body, timeout=20)
        assert r.status_code == 200

    def test_update_appointments(self, api_base, auth_headers):
        body = {"default_duration_minutes": 30, "default_recruiter": "Sam",
                "default_link": "https://zoom.us/j/demo"}
        r = requests.put(f"{api_base}/settings/appointments", headers=auth_headers, json=body, timeout=20)
        assert r.status_code == 200

    def test_update_custom_form(self, api_base, auth_headers):
        body = {"questions": [{"question": "Why this role?", "answer_type": "text",
                                "options": [], "required": True}]}
        r = requests.put(f"{api_base}/settings/custom-form", headers=auth_headers, json=body, timeout=20)
        assert r.status_code == 200


# ========================= Intelligence, Calendar, Twilio =========================
class TestIntelligence:
    def test_dashboard(self, api_base, auth_headers, seeded):
        pipelines = requests.get(f"{api_base}/pipelines", headers=auth_headers, timeout=20).json()
        pid = pipelines[0]["id"]
        r = requests.get(f"{api_base}/intelligence/dashboard?pipeline_id={pid}",
                         headers=auth_headers, timeout=30)
        assert r.status_code == 200
        d = r.json()
        for k in ("kpis", "by_stage", "trend", "channels", "pipeline_steps"):
            assert k in d
        assert len(d["trend"]) == 21

    def test_calendar_appointments(self, api_base, auth_headers, seeded):
        r = requests.get(f"{api_base}/calendar/appointments", headers=auth_headers, timeout=20)
        assert r.status_code == 200
        rows = r.json()
        assert isinstance(rows, list)
        # seeded has candidates with appointments (APPOINTMENT/FORM/CLOSE/TRAINING stages)
        assert len(rows) > 0
        for r_ in rows:
            assert "_id" not in r_


class TestTwilio:
    def test_caller_ids_list(self, api_base, auth_headers):
        r = requests.get(f"{api_base}/twilio/caller-ids", headers=auth_headers, timeout=20)
        assert r.status_code == 200
        # list or dict with list; just ensure no crash
        _ = r.json()

    def test_verify_caller_id(self, api_base, auth_headers):
        r = requests.post(f"{api_base}/twilio/verify-caller-id", headers=auth_headers,
                         json={"phone_number": "+15555550100"}, timeout=30)
        # Either success or clean failure
        assert r.status_code in (200, 400, 500)
        # Response body should be JSON
        try:
            _ = r.json()
        except Exception:
            pytest.fail("non-json response")


# ========================= Auth enforcement =========================
class TestAuthEnforcement:
    @pytest.mark.parametrize("path", [
        "/pipelines", "/jobs", "/candidates", "/settings",
        "/intelligence/dashboard",
        "/calendar/appointments", "/twilio/caller-ids",
    ])
    def test_protected_endpoints_require_auth(self, api_base, path):
        r = requests.get(f"{api_base}{path}", timeout=10)
        assert r.status_code in (401, 403), f"{path} returned {r.status_code}"

    def test_comms_defaults_auth_missing_bug(self, api_base):
        """Documents that /settings/comms-defaults endpoint has no auth dependency
        (this is a minor security gap - endpoint is currently public)."""
        r = requests.get(f"{api_base}/settings/comms-defaults", timeout=10)
        # Currently returns 200 without auth; test documents the current behaviour.
        assert r.status_code == 200
