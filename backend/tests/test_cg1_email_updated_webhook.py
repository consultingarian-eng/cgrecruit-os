"""CG1's email-updated webhook: candidate card + sheet follow the new email.

Fired when a trainee swaps their Indeed relay address for their real email via
the CG1 in-app prompt. The candidate is resolved id → cg1_user_id → old email,
the card's email is overwritten (old kept in previous_email), the permanent
cg1_user_id link is (re)stamped, and the NEW HIRES sheet row is rewritten.
"""
import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("CG1_WEBHOOK_SECRET", "test-cg1-secret")

import server  # noqa: E402
import sheets_service  # noqa: E402


class _FakeRequest:
    def __init__(self, body):
        self._body = body
        import os
        self.headers = {"x-webhook-secret": os.getenv("CG1_WEBHOOK_SECRET", "")}

    async def json(self):
        return self._body


def _fake_db(candidate, pipeline):
    db = MagicMock()
    db.candidates.find_one = AsyncMock(return_value=candidate)
    db.candidates.update_one = AsyncMock()
    db.pipelines.find_one = AsyncMock(return_value=pipeline)
    return db


CAND = {"id": "cand-1", "first_name": "Rowan", "last_name": "Sample",
        "email": "rowansample_a2q@indeedemail.com", "pipeline_id": "pipe-1"}
PIPE = {"cg1_office_key": "downtown"}


def _call(body, db):
    req = _FakeRequest(body)
    with patch.object(server, "db", db), \
         patch.object(sheets_service, "update_hire_email", return_value=True) as sheet:
        result = asyncio.run(server.cg1_email_updated_callback(req))
    return result, sheet


def test_updates_candidate_and_sheet():
    db = _fake_db(CAND, PIPE)
    result, sheet = _call({
        "candidate_id": "cand-1", "cg1_user_id": "abc123",
        "old_email": "rowansample_a2q@indeedemail.com",
        "new_email": "rowan.sample10@example.com",
    }, db)
    assert result == {"ok": True, "candidate_id": "cand-1"}
    update = db.candidates.update_one.call_args.args[1]["$set"]
    assert update["email"] == "rowan.sample10@example.com"
    assert update["previous_email"] == "rowansample_a2q@indeedemail.com"
    assert update["cg1_user_id"] == "abc123"
    sheet.assert_called_once_with(
        office_key="downtown",
        old_email="rowansample_a2q@indeedemail.com",
        name="Rowan Sample",
        new_email="rowan.sample10@example.com",
    )


def test_resolves_by_cg1_user_id_when_no_candidate_id():
    db = _fake_db(None, PIPE)
    # First find_one (by cg1_user_id) hits, so return the candidate on call 1.
    db.candidates.find_one = AsyncMock(side_effect=[CAND])
    result, _ = _call({
        "cg1_user_id": "abc123",
        "old_email": "rowansample_a2q@indeedemail.com",
        "new_email": "rowan.sample10@example.com",
    }, db)
    assert result["ok"] is True
    query = db.candidates.find_one.call_args.args[0]
    assert query == {"cg1_user_id": "abc123"}


def test_unknown_candidate_404s():
    db = _fake_db(None, PIPE)
    with pytest.raises(Exception) as exc:
        _call({"cg1_user_id": "nope", "old_email": "x@indeedemail.com", "new_email": "y@z.com"}, db)
    assert getattr(exc.value, "status_code", None) == 404


def test_missing_new_email_400s():
    db = _fake_db(CAND, PIPE)
    with pytest.raises(Exception) as exc:
        _call({"candidate_id": "cand-1", "new_email": ""}, db)
    assert getattr(exc.value, "status_code", None) == 400
