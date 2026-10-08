"""POST /internal/candidates/create-in-training — CG1 books a starter straight into TRAINING.

Must behave exactly like the kanban "+" on the TRAINING column: candidate lands in
TRAINING with a naive-ET training_start_at, the starter email + CG1 new-starter
webhook + NEW HIRES sheet append fire in the background (send_comms=false skips
email and sheet but NEVER the CG1 webhook), and sms_confirmation_sent_at stays
ABSENT so the existing 5-minute sweep arms the confirmation SMS on its own.
No screening / warmup / dialer jobs ever start.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import deps  # noqa: E402
from routes import internal  # noqa: E402


class _FakeRequest:
    def __init__(self, secret=""):
        self.headers = {"x-webhook-secret": secret} if secret else {}


PIPE = {"id": "pipe-1", "user_id": "user-1", "cg1_office_key": "downtown",
        "name": "Downtown", "public_slug": "downtown"}

BODY = {
    "office_key": "downtown",
    "first_name": "Rowan",
    "last_name": "Sample",
    "email": "Rowan@Example.com",
    "phone": "+1 (617) 555-0123",
    "start_date": "2026-08-24",
    "start_time": "09:30",
}


def _fake_db(pipeline=PIPE):
    db = MagicMock()
    db.pipelines.find_one = AsyncMock(return_value=pipeline)
    db.candidates.insert_one = AsyncMock()
    db.candidates.update_one = AsyncMock()
    db.candidates.find_one = AsyncMock(return_value=None)
    db.users.find_one = AsyncMock(return_value={"id": "user-1", "name": "Owner"})
    return db


def _call(body, db=None, dup=None, secret_env="test-secret", secret_header="test-secret"):
    """Invoke the endpoint with db + dedupe + comms trio all mocked out."""
    db = db if db is not None else _fake_db()
    req = _FakeRequest(secret_header)
    with patch.object(internal, "WEBHOOK_SECRET", secret_env), \
         patch.object(internal, "db", db), \
         patch.object(deps, "find_duplicate_candidate", AsyncMock(return_value=dup)), \
         patch.object(internal, "_run_training_comms", AsyncMock()) as comms:
        result = asyncio.run(internal.internal_create_in_training(req, dict(body)))
    return result, db, comms


# ---------- auth ----------

def test_secret_required():
    with pytest.raises(Exception) as exc:
        _call(BODY, secret_env="topsecret", secret_header="wrong")
    assert getattr(exc.value, "status_code", None) == 403


def test_internal_api_fails_closed_without_a_secret():
    with pytest.raises(Exception) as exc:
        _call(BODY, secret_env="", secret_header="")
    assert getattr(exc.value, "status_code", None) == 503


def test_correct_secret_accepted():
    result, _, _ = _call(BODY, secret_env="topsecret", secret_header="topsecret")
    assert result["ok"] is True


# ---------- validation ----------

def test_unknown_office_404s():
    db = _fake_db(pipeline=None)
    with pytest.raises(Exception) as exc:
        _call({**BODY, "office_key": "chicago"}, db=db)
    assert getattr(exc.value, "status_code", None) == 404
    db.candidates.insert_one.assert_not_called()


@pytest.mark.parametrize("missing", ["office_key", "first_name", "start_date", "start_time"])
def test_missing_required_field_400s(missing):
    body = {k: v for k, v in BODY.items() if k != missing}
    with pytest.raises(Exception) as exc:
        _call(body)
    assert getattr(exc.value, "status_code", None) == 400


@pytest.mark.parametrize("bad_date", ["08/24/2026", "2026-8-24x", "tomorrow", "2026-13-40"])
def test_bad_start_date_400s(bad_date):
    with pytest.raises(Exception) as exc:
        _call({**BODY, "start_date": bad_date})
    assert getattr(exc.value, "status_code", None) == 400


@pytest.mark.parametrize("bad_time", ["9:30am", "25:00", "0930", "half nine"])
def test_bad_start_time_400s(bad_time):
    with pytest.raises(Exception) as exc:
        _call({**BODY, "start_time": bad_time})
    assert getattr(exc.value, "status_code", None) == 400


# ---------- created doc shape ----------

def test_created_doc_shape():
    result, db, _ = _call(BODY)
    db.candidates.insert_one.assert_called_once()
    doc = db.candidates.insert_one.call_args.args[0]

    assert doc["stage"] == "TRAINING"
    assert doc["training_start_at"] == "2026-08-24T09:30:00"  # naive ET, no tz suffix
    assert doc["user_id"] == "user-1"
    assert doc["pipeline_id"] == "pipe-1"
    assert doc["first_name"] == "Rowan"
    assert doc["email"] == "rowan@example.com"  # lowered
    assert doc["moved_to_training_at"]
    assert doc["email_sent"] is False  # send_comms defaults true → email not yet sent

    # The 5-min SMS sweep picks up docs where this field does NOT exist —
    # writing it (even as null) would silence the confirmation SMS forever.
    assert "sms_confirmation_sent_at" not in doc

    # Sweep pickup preconditions all hold on the fresh doc.
    assert doc.get("archived_at") is None
    assert doc.get("phone")

    # No screening/warmup ever started: status untouched at its model default,
    # and nothing scheduled a call.
    assert doc.get("screening_status") == "pending"
    assert doc.get("next_call_at") is None

    assert result == {"ok": True, "candidate_id": doc["id"], "duplicate": False}


# ---------- dedupe ----------

def test_duplicate_returns_existing_and_skips_insert():
    dup = {"id": "cand-old", "first_name": "Rowan", "last_name": "Sample", "stage": "APPOINTMENT"}
    result, db, comms = _call(BODY, dup=dup)
    assert result == {"ok": True, "duplicate": True,
                      "existing_id": "cand-old", "existing_stage": "APPOINTMENT"}
    db.candidates.insert_one.assert_not_called()
    comms.assert_not_called()


# ---------- background comms spawn ----------

def test_comms_trio_spawned_with_send_comms_on():
    body = {**BODY, "monday_start": "10:00", "monday_end": "18:00"}
    result, _, comms = _call(body)
    assert result["ok"] is True
    comms.assert_called_once()
    kw = comms.call_args.kwargs
    assert kw["candidate_id"] == result["candidate_id"]
    assert kw["user_id"] == "user-1"
    assert kw["training_start_at"] == "2026-08-24T09:30:00"
    assert kw["send_email"] is True
    assert kw["append_sheet"] is True
    assert kw["interviewer"] == "CG1"  # no booked_by → CG1 fallback
    assert kw["monday_start"] == "10:00"
    assert kw["monday_end"] == "18:00"
    assert kw["tuesday_start"] == ""


def test_send_comms_off_skips_email_and_sheet_but_still_spawns_trio():
    result, db, comms = _call({**BODY, "send_comms": False})
    assert result["ok"] is True
    # The trio still runs — the CG1 webhook inside it always fires.
    comms.assert_called_once()
    kw = comms.call_args.kwargs
    assert kw["send_email"] is False
    assert kw["append_sheet"] is False
    # Kanban parity: skip_notifications stamps email_sent=True on the doc.
    doc = db.candidates.insert_one.call_args.args[0]
    assert doc["email_sent"] is True
    assert "sms_confirmation_sent_at" not in doc


def test_booked_by_lands_as_sheet_interviewer():
    _, _, comms = _call({**BODY, "booked_by": "Jess Smith"})
    assert comms.call_args.kwargs["interviewer"] == "Jess Smith"


# ---------- the trio itself: email + webhook + sheet gating ----------

CAND_DOC = {"id": "cand-1", "user_id": "user-1", "pipeline_id": "pipe-1",
            "first_name": "Rowan", "last_name": "Sample",
            "email": "rowan@example.com", "phone": "6175550123"}


def _run_trio(send_email, append_sheet, interviewer=None):
    db = _fake_db()
    email_mock = AsyncMock(return_value={"status": "sent"})
    webhook_mock = AsyncMock(return_value=None)
    sheet_mock = MagicMock(return_value=True)
    with patch.object(internal, "db", db), \
         patch("server._send_starter_email_direct", email_mock), \
         patch.object(internal, "_fire_cg1_webhook_inline", webhook_mock), \
         patch("sheets_service.append_new_hire", sheet_mock):
        asyncio.run(internal._run_training_comms(
            candidate_id="cand-1", user_id="user-1",
            training_start_at="2026-08-24T09:30:00",
            cand=CAND_DOC,
            monday_start="10:00",
            send_email=send_email, append_sheet=append_sheet,
            interviewer=interviewer,
        ))
    return email_mock, webhook_mock, sheet_mock


def test_trio_full_send():
    email, webhook, sheet = _run_trio(send_email=True, append_sheet=True, interviewer="Jess Smith")
    email.assert_called_once()
    assert email.call_args.kwargs["candidate_id"] == "cand-1"
    assert email.call_args.kwargs["extra_template"] == {"monday_start": "10:00"}
    webhook.assert_called_once()
    assert webhook.call_args.kwargs["monday_start"] == "10:00"
    sheet.assert_called_once()
    skw = sheet.call_args.kwargs
    assert skw["office_key"] == "downtown"
    assert skw["name"] == "Rowan Sample"
    assert skw["interviewer"] == "Jess Smith"
    assert skw["training_start_at"] == "2026-08-24T09:30:00"


def test_trio_comms_off_still_fires_cg1_webhook():
    email, webhook, sheet = _run_trio(send_email=False, append_sheet=False)
    email.assert_not_called()
    sheet.assert_not_called()
    webhook.assert_called_once()  # roster sync is unconditional
    assert webhook.call_args.kwargs["candidate_id"] == "cand-1"


# ---------- refactor guard: move-to-training still uses the same trio ----------

def test_move_to_training_still_spawns_trio():
    db = _fake_db()
    db.candidates.find_one = AsyncMock(return_value={
        "id": "cand-9", "stage": "CLOSE", "user_id": "user-1",
        "pipeline_id": "pipe-1", "archived_at": None,
    })
    req = _FakeRequest("test-secret")
    with patch.object(internal, "WEBHOOK_SECRET", "test-secret"), \
         patch.object(internal, "db", db), \
         patch.object(internal, "_run_training_comms", AsyncMock()) as comms:
        result = asyncio.run(internal.internal_move_to_training(
            "cand-9", req,
            {"training_start_at": "2026-08-24T09:30:00", "monday_start": "10:00"},
        ))
    assert result["ok"] is True and result["stage"] == "TRAINING"
    comms.assert_called_once()
    kw = comms.call_args.kwargs
    assert kw["candidate_id"] == "cand-9"
    assert kw["monday_start"] == "10:00"
    # Move path keeps the trio defaults: email + sheet on, interviewer derived
    # from the pipeline owner (no override).
    assert kw.get("send_email", True) is True
    assert kw.get("append_sheet", True) is True
    assert kw.get("interviewer") is None
