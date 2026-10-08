"""update_hire_email must rewrite the right cell when an Indeed relay address
is replaced with the person's real email via the CG1 in-app prompt.

The cell is found by locating the old address inside the matched row (robust
to drifted column order), falling back to the office's fixed email column.
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# A frozen company profile (offices "downtown" and "riverside") and a dummy
# sheet id, so these tests never depend on your own configuration.
os.environ["COMPANY_PROFILE_PATH"] = str(Path(__file__).resolve().parent / "company_profile.test.json")
os.environ.setdefault("NEW_HIRES_SHEET_ID", "test-sheet-id")

import sheets_service as ss


def _mock_service(rows):
    svc = MagicMock()
    sheets = svc.spreadsheets.return_value
    sheets.values.return_value.get.return_value.execute.return_value = {"values": rows}
    return svc, sheets


def test_rewrites_email_cell_downtown():
    rows = [
        ["WE", "INTERVIEWER", "NAME", "EMAIL", "PHONE"],
        ["WE 8/16/26", "X", "Rowan Sample", "rowansample4_73d@indeedemail.com", "1"],
    ]
    svc, sheets = _mock_service(rows)
    with patch.object(ss, "_build_service", return_value=svc):
        assert ss.update_hire_email(
            "downtown", "rowansample4_73d@indeedemail.com",
            "Rowan Sample", "rowansample@example.com",
        ) is True
    update = sheets.values.return_value.update.call_args.kwargs
    assert update["range"] == "'DOWNTOWN'!D2"
    assert update["body"] == {"values": [["rowansample@example.com"]]}


def test_finds_cell_when_columns_drift():
    # Email sitting in an unexpected column — the old-address scan must win
    # over the OFFICE_COLUMNS fallback (which would say column D).
    rows = [
        ["WE 8/16/26", "old@indeedemail.com", "Somebody Real", "", "1"],
    ]
    svc, sheets = _mock_service(rows)
    with patch.object(ss, "_build_service", return_value=svc):
        assert ss.update_hire_email("downtown", "old@indeedemail.com", "Somebody Real", "new@x.com") is True
    assert sheets.values.return_value.update.call_args.kwargs["range"] == "'DOWNTOWN'!B1"


def test_falls_back_to_name_and_fixed_column():
    # Old email absent from the sheet (e.g. row was hand-edited) — match by
    # name and write the office's fixed email column.
    rows = [
        ["8/16/26", "Kai Example", "1", "typo@wrong.com"],
    ]
    svc, sheets = _mock_service(rows)
    with patch.object(ss, "_build_service", return_value=svc):
        assert ss.update_hire_email("riverside", "missing@indeedemail.com", "Kai Example", "kai@x.com") is True
    # Riverside layout: we_date, name, phone, email → index 3 → column D
    assert sheets.values.return_value.update.call_args.kwargs["range"] == "'RIVERSIDE'!D1"


def test_prefers_last_matching_row():
    rows = [
        ["WE 1/01/26", "X", "Repeat Hire", "old@indeedemail.com", "1"],
        ["WE 8/16/26", "X", "Repeat Hire", "old@indeedemail.com", "1"],
    ]
    svc, sheets = _mock_service(rows)
    with patch.object(ss, "_build_service", return_value=svc):
        assert ss.update_hire_email("downtown", "old@indeedemail.com", "Repeat Hire", "new@x.com") is True
    assert sheets.values.return_value.update.call_args.kwargs["range"] == "'DOWNTOWN'!D2"


def test_no_match_returns_false_not_raise():
    rows = [["WE", "INTERVIEWER", "NAME", "EMAIL", "PHONE"]]
    svc, sheets = _mock_service(rows)
    with patch.object(ss, "_build_service", return_value=svc):
        assert ss.update_hire_email("downtown", "ghost@indeedemail.com", "Nobody Here", "x@y.com") is False
    sheets.values.return_value.update.assert_not_called()
