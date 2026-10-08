"""Shared fixtures for the HTTP integration tests (test_backend.py,
test_v5_features.py).

Those tests talk to a RUNNING CGRecruit server over HTTP, register a user and
seed demo data. They only run when you point them at one explicitly:

    CGRECRUIT_TEST_URL=http://localhost:8001 pytest tests/test_backend.py

Never point this at a deployment with real candidates. With the variable unset
the integration tests skip, and every other test file is unaffected (they are
pure and mocked).
"""
import os
import time
import pytest
import requests

# Every test reads the frozen test company profile, never your own
# backend/company_profile.json, so editing your profile can't break the suite.
os.environ.setdefault(
    "COMPANY_PROFILE_PATH",
    os.path.join(os.path.dirname(__file__), "company_profile.test.json"),
)

BASE_URL = (os.environ.get("CGRECRUIT_TEST_URL") or "").rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="session")
def api_base():
    if not BASE_URL:
        pytest.skip("integration test — set CGRECRUIT_TEST_URL to a local test server")
    return API


@pytest.fixture(scope="session")
def test_user(api_base):
    """Register a fresh test user per test session.

    Registration is no longer public: the target deployment must either have
    no users at all (and CGRECRUIT_TEST_SETUP_TOKEN must match its SETUP_TOKEN),
    or CGRECRUIT_TEST_ADMIN_TOKEN must hold a super-admin bearer token for it. Anything else skips, rather than failing with a bare
    403 that looks like a broken test.
    """
    ts = int(time.time())
    email = f"tester+{ts}@cgrecruit.example.com"
    password = "testpass1234"
    payload = {"email": email, "password": password, "name": "Test User", "company": "Example Co QA"}
    headers = {}
    admin_token = os.environ.get("CGRECRUIT_TEST_ADMIN_TOKEN")
    if admin_token:
        headers["Authorization"] = f"Bearer {admin_token}"
    # The first account on an empty deployment needs that server's SETUP_TOKEN.
    setup_token = os.environ.get("CGRECRUIT_TEST_SETUP_TOKEN")
    if setup_token:
        headers["X-Setup-Token"] = setup_token
    r = requests.post(f"{API}/auth/register", json=payload, headers=headers, timeout=30)
    if r.status_code in (403, 503):
        pytest.skip(
            "registration needs an admin — set CGRECRUIT_TEST_ADMIN_TOKEN to a "
            "super-admin token for this deployment, or point the suite at an empty one"
        )
    assert r.status_code == 200, f"register failed: {r.status_code} {r.text}"
    data = r.json()
    return {"email": email, "password": password, "token": data["token"], "user": data["user"]}


@pytest.fixture(scope="session")
def auth_headers(test_user):
    return {"Authorization": f"Bearer {test_user['token']}", "Content-Type": "application/json"}


@pytest.fixture(scope="session")
def seeded(test_user, auth_headers):
    """Seed demo data once per session."""
    r = requests.post(f"{API}/seed/demo", headers=auth_headers, timeout=60)
    assert r.status_code == 200, f"seed failed: {r.status_code} {r.text}"
    return r.json()
