"""Scoping and redaction rules for the CGRecruit Claude connector tools.

Run: cd backend && .venv/bin/python tests/test_claude_connector_cgrecruit.py
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/x")
os.environ.setdefault("DB_NAME", "x")

from claude_connector.cgrecruit import TOOLS, CGRecruitAdapter, _candidate_filter, _clean, _exit_category, _scope  # noqa: E402
from claude_connector.core import ToolContext, ToolError  # noqa: E402


def ctx(**user):
    return ToolContext(user={"id": "me", **user}, db=None)


class ScopeTest(unittest.TestCase):
    def test_owner_sees_every_pipeline(self):
        s = _scope({"id": "owner", "role": "recruiter"})  # parentless recruiter is the tenant owner
        self.assertEqual(s["role"], "super_admin")
        self.assertIsNone(s["pipeline_ids"])
        self.assertEqual(s["tenant"], "owner")

    def test_sub_account_is_scoped_to_tenant_and_pipelines(self):
        s = _scope({"id": "rec", "role": "recruiter", "parent_user_id": "owner", "pipeline_ids": ["p1"]})
        self.assertEqual((s["role"], s["tenant"], s["pipeline_ids"]), ("recruiter", "owner", ["p1"]))

    def test_viewer_only_sees_candidates_they_added(self):
        c = ctx(role="viewer", parent_user_id="owner", pipeline_ids=["p1"])
        q = _candidate_filter(c, ["p1"], individual=True)
        self.assertEqual(q["added_by_user_id"], "me")
        self.assertEqual(q["user_id"], "owner")
        self.assertNotIn("added_by_user_id", _candidate_filter(c, ["p1"], individual=False))

    def test_analyst_gets_aggregates_only(self):
        c = ctx(role="analyst", parent_user_id="owner", pipeline_ids=["p1"])
        _candidate_filter(c, ["p1"], individual=False)
        with self.assertRaises(ToolError):
            _candidate_filter(c, ["p1"], individual=True)

    def test_credentials_never_leave(self):
        doc = {"_id": 1, "id": "c1", "public_token": "abc", "nested": {"access_token": "x", "ok": 1},
               "rows": [{"password_hash": "h", "keep": 2}], "phone": "+1555"}
        self.assertEqual(_clean(doc), {"id": "c1", "nested": {"ok": 1}, "rows": [{"keep": 2}], "phone": "+1555"})

    def test_exit_categories_match_the_app(self):
        self.assertEqual(_exit_category("max_attempts_no_contact"), "uncontactable")
        self.assertEqual(_exit_category("no_show_not_rescheduled"), "no_show")
        self.assertEqual(_exit_category("merged_into:abc"), "duplicate")
        self.assertEqual(_exit_category(None), "other")

    def test_demo_login_refused_and_fingerprint_tracks_password(self):
        a = CGRecruitAdapter(db=None)
        self.assertTrue(a.refuse_reason({"is_demo": True}))
        self.assertIsNone(a.refuse_reason({"role": "recruiter"}))
        self.assertNotEqual(a.fingerprint({"password_hash": "a"}), a.fingerprint({"password_hash": "b"}))

    def test_every_tool_is_described_and_read_only(self):
        names = [t.name for t in TOOLS]
        self.assertEqual(len(names), len(set(names)))
        for t in TOOLS:
            d = t.describe()
            self.assertTrue(d["annotations"]["readOnlyHint"])
            self.assertFalse(d["annotations"]["destructiveHint"])
            self.assertTrue(d["description"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
