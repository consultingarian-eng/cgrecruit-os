"""The per-candidate SMS endpoints and the manual SMS tick used to check
nothing. They must now accept exactly their two real callers - CG1's backend
proxy (shared x-webhook-secret) and a signed-in CGRecruit user who can see the
candidate - and refuse everyone else. Also pins the failed-login throttle.

Run: cd backend && MONGO_URL=mongodb://127.0.0.1:1/x DB_NAME=x .venv/bin/python tests/test_sms_endpoint_auth.py
"""
import asyncio
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/x")
os.environ.setdefault("DB_NAME", "x")
os.environ.setdefault("JWT_SECRET", "unit-test-secret-with-32-plus-chars!!")
os.environ["CGRECRUIT_WEBHOOK_SECRET"] = "shared-secret-for-tests-0123456789"

from fastapi import HTTPException  # noqa: E402
from starlette.requests import Request  # noqa: E402

import deps  # noqa: E402
import login_throttle  # noqa: E402
import training_sms  # noqa: E402
from auth_service import create_token  # noqa: E402

OWNER, OTHER_TENANT = "owner-1", "owner-2"
DOWNTOWN, RIVERSIDE = "p-bos", "p-nh"


class _Coll:
    def __init__(self, docs):
        self.docs = docs

    async def find_one(self, query, projection=None):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return dict(d)
        return None

    async def update_one(self, *a, **k):
        return None


class _DB:
    def __init__(self):
        self.users = _Coll([
            {"id": OWNER, "email": "hr@x.test", "role": "super_admin"},
            {"id": "rec-bos", "email": "bos@x.test", "role": "recruiter", "parent_user_id": OWNER, "pipeline_ids": [DOWNTOWN]},
            {"id": "viewer", "email": "v@x.test", "role": "viewer", "parent_user_id": OWNER, "pipeline_ids": [DOWNTOWN, RIVERSIDE]},
            {"id": OTHER_TENANT, "email": "demo@x.test", "role": "super_admin"},
        ])
        self.candidates = _Coll([
            {"id": "c-nh", "user_id": OWNER, "pipeline_id": RIVERSIDE, "first_name": "Yael", "last_name": "H", "phone": "+1555"},
            {"id": "c-bos", "user_id": OWNER, "pipeline_id": DOWNTOWN, "first_name": "Bo", "last_name": "S", "phone": "+1556", "added_by_user_id": "viewer"},
        ])


def _request(method="GET", secret=None, user_id=None, path="/api/x"):
    headers = []
    if secret is not None:
        headers.append((b"x-webhook-secret", secret.encode()))
    if user_id:
        headers.append((b"authorization", f"Bearer {create_token(user_id, user_id + '@x')}".encode()))
    return Request({"type": "http", "method": method, "path": path, "headers": headers, "query_string": b"", "scheme": "https", "server": ("t", 443)})


def _status(coro):
    try:
        asyncio.run(coro)
        return 200
    except HTTPException as exc:
        return exc.status_code


class SmsEndpointAuthTest(unittest.TestCase):
    def setUp(self):
        self._db = deps.db
        deps.db = _DB()
        self._thread, self._send, self._tick = training_sms._get_thread, training_sms._send_confirmation_sms, training_sms.scheduler_tick

        async def thread(db, cid):
            return [{"direction": "out", "body": "hi"}]

        async def send(db, cid, force=False):
            return {"ok": True}

        async def tick(db):
            return 0
        training_sms._get_thread, training_sms._send_confirmation_sms, training_sms.scheduler_tick = thread, send, tick

    def tearDown(self):
        deps.db = self._db
        training_sms._get_thread, training_sms._send_confirmation_sms, training_sms.scheduler_tick = self._thread, self._send, self._tick

    def thread(self, cid, **kw):
        return _status(training_sms.get_sms_thread(cid, _request(**kw)))

    def resend(self, cid, **kw):
        return _status(training_sms.resend_confirmation_sms(cid, _request("POST", **kw)))

    def test_cg1_proxy_with_the_shared_secret_still_works(self):
        self.assertEqual(self.thread("c-nh", secret="shared-secret-for-tests-0123456789"), 200)
        self.assertEqual(self.resend("c-nh", secret="shared-secret-for-tests-0123456789"), 200)
        out = asyncio.run(training_sms.get_sms_thread("c-nh", _request(secret="shared-secret-for-tests-0123456789")))
        self.assertEqual(out["name"], "Yael H")
        self.assertEqual(out["messages"][0]["body"], "hi")

    def test_anonymous_and_wrong_secret_are_refused(self):
        self.assertEqual(self.thread("c-nh"), 401)
        self.assertEqual(self.resend("c-nh"), 401)
        self.assertEqual(self.thread("c-nh", secret="guess"), 403)
        self.assertEqual(self.resend("c-nh", secret="guess"), 403)

    def test_secret_check_fails_closed_when_unset(self):
        saved = os.environ.pop("CGRECRUIT_WEBHOOK_SECRET")
        try:
            self.assertEqual(self.thread("c-nh", secret=""), 401)  # empty header = no header -> needs a session
            self.assertEqual(self.thread("c-nh", secret="anything"), 403)
        finally:
            os.environ["CGRECRUIT_WEBHOOK_SECRET"] = saved

    def test_signed_in_users_follow_pipeline_and_tenant_rules(self):
        self.assertEqual(self.thread("c-nh", user_id=OWNER), 200)       # the drawer, as the owner
        self.assertEqual(self.thread("c-bos", user_id="rec-bos"), 200)  # recruiter, own pipeline
        self.assertEqual(self.thread("c-nh", user_id="rec-bos"), 403)   # recruiter, other pipeline
        self.assertEqual(self.thread("c-nh", user_id=OTHER_TENANT), 404)  # another tenant's super admin
        self.assertEqual(self.thread("nope", user_id=OWNER), 404)
        self.assertEqual(self.thread("c-bos", user_id="viewer"), 200)   # viewer, own addition
        self.assertEqual(self.resend("c-bos", user_id="viewer"), 403)   # but read-only
        self.assertEqual(self.resend("c-bos", user_id="rec-bos"), 200)

    def test_manual_tick_needs_secret_or_super_admin(self):
        tick = lambda **kw: _status(training_sms.manual_tick(_request("POST", **kw)))  # noqa: E731
        self.assertEqual(tick(), 401)
        self.assertEqual(tick(user_id="rec-bos"), 403)
        self.assertEqual(tick(user_id=OWNER), 200)
        self.assertEqual(tick(secret="shared-secret-for-tests-0123456789"), 200)


class LoginThrottleTest(unittest.TestCase):
    def setUp(self):
        login_throttle._fails.clear()

    def test_ten_failures_lock_the_account_not_the_office(self):
        a = login_throttle.keys_for("Willa@X.test", "203.0.113.9", "10.0.0.1")
        b = login_throttle.keys_for("someone@x.test", "203.0.113.9", "10.0.0.1")
        self.assertEqual(a["account"], "acct:willa@x.test")
        self.assertEqual(a["ip"], "ip:203.0.113.9")
        for _ in range(9):
            login_throttle.record_failure(a, now=1000.0)
        self.assertFalse(login_throttle.is_blocked(a, now=1000.0))
        login_throttle.record_failure(a, now=1000.0)
        self.assertTrue(login_throttle.is_blocked(a, now=1000.0))
        self.assertFalse(login_throttle.is_blocked(b, now=1000.0))  # same office IP, different person
        self.assertFalse(login_throttle.is_blocked(a, now=1000.0 + 15 * 60 + 1))  # window slides

    def test_rightmost_forwarded_hop_is_the_ip(self):
        k = login_throttle.keys_for("x@x", "1.1.1.1, 198.51.100.7", "10.0.0.1")
        self.assertEqual(k["ip"], "ip:198.51.100.7")
        self.assertEqual(login_throttle.keys_for("x@x", None, "10.0.0.1")["ip"], "ip:10.0.0.1")

    def test_success_clears_the_account(self):
        a = login_throttle.keys_for("a@x", "203.0.113.9", None)
        for _ in range(5):
            login_throttle.record_failure(a)
        login_throttle.clear([a["account"]])
        self.assertNotIn(a["account"], login_throttle._fails)


if __name__ == "__main__":
    unittest.main(verbosity=2)
