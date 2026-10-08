"""A recruiter assigned to some pipelines must not read other pipelines'
candidates through the Calendar, the candidate endpoints or AI insights
(user_id alone is the whole tenant). The owner's view must not change.

Run: cd backend && MONGO_URL=mongodb://127.0.0.1:1/x DB_NAME=x .venv/bin/python tests/test_recruiter_pipeline_scope.py
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

from fastapi import HTTPException  # noqa: E402

import server  # noqa: E402
from routes import ai_insights  # noqa: E402

OWNER, BOS, NH = "owner", "p-bos", "p-nh"


def _match(doc, q):
    for k, v in q.items():
        if isinstance(v, dict) and any(op.startswith("$") for op in v):
            if "$in" in v and doc.get(k) not in v["$in"]:
                return False
            if "$ne" in v and doc.get(k) == v["$ne"]:
                return False
        elif doc.get(k) != v:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, *a, **k):
        return self

    async def to_list(self, n):
        return self.docs[:n]


class _Coll:
    def __init__(self, docs):
        self.docs = docs

    def find(self, q, projection=None):
        return _Cursor([dict(d) for d in self.docs if _match(d, q)])

    async def find_one(self, q, projection=None, sort=None):
        hits = [dict(d) for d in self.docs if _match(d, q)]
        return hits[0] if hits else None


class _DB:
    candidates = _Coll([
        {"id": "c-bos", "user_id": OWNER, "pipeline_id": BOS, "appointment_at": "2026-10-13T13:00:00+00:00"},
        {"id": "c-nh", "user_id": OWNER, "pipeline_id": NH, "appointment_at": "2026-10-14T13:00:00+00:00"},
    ])
    conversations = _Coll([{"candidate_id": "c-nh", "user_id": OWNER, "id": "v1"}])
    communications = _Coll([{"candidate_id": "c-nh", "user_id": OWNER, "id": "m1"}])
    ai_insight_jobs = _Coll([{"id": "j-nh", "user_id": OWNER, "pipeline_id": NH, "status": "complete"}])


owner = {"id": OWNER, "auth_user_id": OWNER, "role": "super_admin", "pipeline_ids": []}
downtown_rec = {"id": OWNER, "auth_user_id": "rec", "role": "recruiter", "parent_user_id": OWNER, "pipeline_ids": [BOS]}
both_rec = {**downtown_rec, "pipeline_ids": [BOS, NH]}


def run(coro):
    try:
        return asyncio.run(coro)
    except HTTPException as exc:
        return exc.status_code


class RecruiterScopeTest(unittest.TestCase):
    def setUp(self):
        self._db, self._ai_db = server.db, ai_insights.db
        server.db = ai_insights.db = _DB()

    def tearDown(self):
        server.db, ai_insights.db = self._db, self._ai_db

    def test_calendar(self):
        ids = lambda u: sorted(c["id"] for c in run(server.list_appointments(pipeline_id=None, limit=100, user=u)))  # noqa: E731
        self.assertEqual(ids(owner), ["c-bos", "c-nh"])
        self.assertEqual(ids(both_rec), ["c-bos", "c-nh"])
        self.assertEqual(ids(downtown_rec), ["c-bos"])
        self.assertEqual(run(server.list_appointments(pipeline_id=NH, limit=100, user=downtown_rec)), 403)

    def test_candidate_and_its_records(self):
        for fn in (server.get_candidate, server.candidate_conversations, server.candidate_communications):
            self.assertNotIsInstance(run(fn("c-nh", user=owner)), int)
            self.assertNotIsInstance(run(fn("c-nh", user=both_rec)), int)
            self.assertEqual(run(fn("c-nh", user=downtown_rec)), 403)
        self.assertEqual(run(server.get_candidate("missing", user=owner)), 404)

    def test_ai_insights(self):
        self.assertEqual(run(ai_insights.get_ai_insights("j-nh", user=owner))["id"], "j-nh")
        self.assertEqual(run(ai_insights.get_ai_insights("j-nh", user=downtown_rec)), 403)
        self.assertEqual(run(ai_insights.get_latest_ai_insights(NH, user=downtown_rec)), 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
