"""The portal's 1-on-1 phone confirmation (POST /public/applicant/{token}/phone).

Recruiters were dialling CLOSE-stage candidates on numbers that never
connected. The questionnaire card now shows the number on file and the
candidate either vouches for it or corrects it — so the number the dialler
gets is one the candidate chose while looking at their own phone. These tests
pin the contract: a full E.164 number on the way in, stage-gated like the
questionnaire, a chat_log trail for both confirm and correction, and the
candidate doc (which IS the kanban card) updated in place.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/unused")
os.environ.setdefault("DB_NAME", "test_unused")

from fastapi import HTTPException  # noqa: E402
from routes import public  # noqa: E402


def _cand(**over):
    base = {
        "id": "c1", "user_id": "u1", "pipeline_id": "p1",
        "public_token": "tok1", "stage": "FORM",
        "first_name": "Rae", "last_name": "M.",
        "phone": "+16175550134", "chat_log": [],
    }
    base.update(over)
    return base


def _db(cand):
    """find_one returns the candidate pre-update, then the post-update doc
    (the endpoint re-reads after update_one, mirroring public_submit_form)."""
    db = MagicMock()
    state = {"doc": dict(cand)}

    async def find_one(q, *a, **k):
        return dict(state["doc"]) if state["doc"] else None

    async def update_one(q, change):
        state["doc"].update(change.get("$set") or {})

    db.candidates.find_one = AsyncMock(side_effect=find_one)
    db.candidates.update_one = AsyncMock(side_effect=update_one)
    db._state = state
    return db


class PhoneConfirmTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, cand, payload):
        db = _db(cand)
        with patch.object(public, "db", db):
            view = await public.public_confirm_phone("tok1", payload)
        return view, db._state["doc"]

    async def test_confirming_the_number_on_file_stamps_confirmed(self):
        view, doc = await self._call(_cand(), {"phone": "+16175550134"})
        self.assertEqual(doc["phone"], "+16175550134")
        self.assertTrue(doc["phone_confirmed_at"])
        self.assertIn("Confirmed", doc["chat_log"][-1]["text"])
        # The portal payload now carries both fields.
        self.assertEqual(view["phone"], "+16175550134")
        self.assertTrue(view["phone_confirmed_at"])

    async def test_confirm_accepts_the_display_format(self):
        # The portal posts back whatever is on file; a naturally-typed value
        # must count as the same number, not a "change".
        _, doc = await self._call(_cand(), {"phone": "(617) 555-0134"})
        self.assertEqual(doc["phone"], "+16175550134")
        self.assertIn("Confirmed", doc["chat_log"][-1]["text"])

    async def test_correction_updates_the_card_and_logs_old_to_new(self):
        _, doc = await self._call(_cand(), {"phone": "(203) 555-0188"})
        self.assertEqual(doc["phone"], "+12035550188")
        self.assertTrue(doc["phone_confirmed_at"])
        entry = doc["chat_log"][-1]["text"]
        self.assertIn("+16175550134", entry)
        self.assertIn("+12035550188", entry)

    async def test_confirming_normalizes_a_raw_stored_number(self):
        # Intake tolerates raw values; a confirm is the moment to fix them.
        _, doc = await self._call(_cand(phone="617-555-0134"),
                                  {"phone": "617-555-0134"})
        self.assertEqual(doc["phone"], "+16175550134")
        self.assertIn("Confirmed", doc["chat_log"][-1]["text"])

    async def test_close_stage_is_allowed(self):
        # A candidate who spots the wrong number after submitting can still fix it.
        _, doc = await self._call(_cand(stage="CLOSE"), {"phone": "2035550188"})
        self.assertEqual(doc["phone"], "+12035550188")

    async def test_earlier_stages_are_gated(self):
        for stage in ("APPLICANT", "SCREENING", "APPOINTMENT"):
            with self.assertRaises(HTTPException) as ctx:
                await self._call(_cand(stage=stage), {"phone": "+16175550134"})
            self.assertEqual(ctx.exception.status_code, 409, stage)

    async def test_short_or_unreadable_numbers_are_rejected(self):
        for bad in ("", "555-0134", "+316123456", "12345", "not a phone", "+1617555013"):
            with self.assertRaises(HTTPException) as ctx:
                await self._call(_cand(), {"phone": bad})
            self.assertEqual(ctx.exception.status_code, 400, repr(bad))

    async def test_an_overseas_number_with_its_country_code_is_accepted(self):
        _view, doc = await self._call(_cand(), {"phone": "+44 7700 900123"})
        self.assertEqual(doc["phone"], "+447700900123")

    async def test_uk_mode_reads_a_local_mobile(self):
        with patch.dict(os.environ, {"PHONE_DEFAULT_COUNTRY": "GB"}):
            _view, doc = await self._call(_cand(phone="+447700900123"), {"phone": "07700 900456"})
        self.assertEqual(doc["phone"], "+447700900456")

    async def test_unknown_token_is_404(self):
        db = MagicMock()
        db.candidates.find_one = AsyncMock(return_value=None)
        with patch.object(public, "db", db):
            with self.assertRaises(HTTPException) as ctx:
                await public.public_confirm_phone("nope", {"phone": "+16175550134"})
        self.assertEqual(ctx.exception.status_code, 404)

    def test_public_view_exposes_phone_fields(self):
        view = public._public_view(_cand(phone_confirmed_at="2026-08-21T12:00:00Z"))
        self.assertEqual(view["phone"], "+16175550134")
        self.assertEqual(view["phone_confirmed_at"], "2026-08-21T12:00:00Z")


if __name__ == "__main__":
    unittest.main()
