"""mark_attended must find rows past 500 and an office's renamed day-1/2 headers.

Both failure modes have happened for real: a busy tab grew past the old
hardcoded A1:Z500 read range, and an office tab labelled its day-1/2
checkboxes MONDAY ATTEND / TUESDAY ATTEND instead of ATTENDED DAY N. Failures
are non-fatal warnings, so nothing surfaced until candidates stopped ticking.
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
    """A spreadsheets() mock serving `rows` and recording update() calls."""
    svc = MagicMock()
    sheets = svc.spreadsheets.return_value
    sheets.values.return_value.get.return_value.execute.return_value = {"values": rows}
    return svc, sheets


def test_read_range_is_unbounded():
    svc, sheets = _mock_service([["ATTENDED DAY 1"]])
    with patch.object(ss, "_build_service", return_value=svc):
        ss.mark_attended("downtown", "x@y.com", "Somebody Real", day=1)
    rng = sheets.values.return_value.get.call_args.kwargs["range"]
    assert rng == "'DOWNTOWN'!A1:Z"


def test_ticks_row_beyond_500_downtown():
    rows = [["WE", "INTERVIEWER", "NAME", "EMAIL", "PHONE", "ATTENDED DAY 1"]]
    rows += [["WE 1/01/26", "X", f"Filler Person{i}", f"f{i}@x.com", "1", "FALSE"] for i in range(700)]
    rows.append(["WE 8/16/26", "Riverside Recruiter", "Rowan Sample", "rowan@x.com", "1", "FALSE"])
    svc, sheets = _mock_service(rows)
    with patch.object(ss, "_build_service", return_value=svc):
        assert ss.mark_attended("downtown", "rowan@x.com", "Rowan Sample", day=1) is True
    update = sheets.values.return_value.update.call_args.kwargs
    assert update["range"] == f"'DOWNTOWN'!F{len(rows)}"
    assert update["body"] == {"values": [[True]]}


def test_riverside_day1_uses_monday_attend_alias():
    rows = [
        ["DATE", "NAME", "NUMBER", "EMAIL", "MONDAY ATTEND", "TUESDAY ATTEND", "ATTENDED DAY 3"],
        ["8/16/26", "Kai Example", "1", "kai@x.com", "FALSE", "FALSE", "FALSE"],
    ]
    svc, sheets = _mock_service(rows)
    with patch.object(ss, "_build_service", return_value=svc):
        assert ss.mark_attended("riverside", "kai@x.com", "Kai Example", day=1) is True
    assert sheets.values.return_value.update.call_args.kwargs["range"] == "'RIVERSIDE'!E2"


def test_riverside_day2_uses_tuesday_attend_alias():
    rows = [
        ["DATE", "NAME", "NUMBER", "EMAIL", "MONDAY ATTEND", "TUESDAY ATTEND", "ATTENDED DAY 3"],
        ["8/16/26", "Kai Example", "1", "kai@x.com", "TRUE", "FALSE", "FALSE"],
    ]
    svc, sheets = _mock_service(rows)
    with patch.object(ss, "_build_service", return_value=svc):
        assert ss.mark_attended("riverside", "kai@x.com", "Kai Example", day=2) is True
    assert sheets.values.return_value.update.call_args.kwargs["range"] == "'RIVERSIDE'!F2"


def test_missing_header_returns_false_not_raise():
    rows = [["DATE", "NAME"], ["8/16/26", "Kai Example"]]
    svc, _ = _mock_service(rows)
    with patch.object(ss, "_build_service", return_value=svc):
        assert ss.mark_attended("riverside", "kai@x.com", "Kai Example", day=9) is False


def test_name_match_ignores_interviewer_column():
    """A bare first name that equals an interviewer's name must not tick the
    rows that interviewer handled (bug: an 'Indigo' hire ticked a row
    that interviewer 'Indigo' handled). Only the NAME column counts."""
    rows = [["WE", "INTERVIEWER", "NAME", "EMAIL", "PHONE", "ATTENDED DAY 2"]]
    rows.append(["WE 4/12/26", "Boss", "Indigo", "", "1", "FALSE"])          # her row
    rows += [["WE 5/10/26", "Indigo", f"Someone Else{i}", f"s{i}@x.com", "1", "FALSE"]
             for i in range(10)]                                              # rows she interviewed
    svc, sheets = _mock_service(rows)
    with patch.object(ss, "_build_service", return_value=svc):
        assert ss.mark_attended("downtown", "indigo-new@x.com", "Indigo", day=2) is True
    assert sheets.values.return_value.update.call_args.kwargs["range"] == "'DOWNTOWN'!F2"


def test_email_match_beats_later_name_column_match():
    """The email hit is authoritative even when a later row's NAME column also
    contains the candidate's name (e.g. a same-named different person)."""
    rows = [
        ["WE", "INTERVIEWER", "NAME", "EMAIL", "PHONE", "ATTENDED DAY 2"],
        ["WE 8/16/26", "X", "Emery Testwood", "emery.t@x.com", "1", "FALSE"],
        ["WE 8/23/26", "X", "Emery Testwood", "other.emery@x.com", "1", "FALSE"],
    ]
    svc, sheets = _mock_service(rows)
    with patch.object(ss, "_build_service", return_value=svc):
        assert ss.mark_attended("downtown", "emery.t@x.com", "Emery Testwood", day=2) is True
    assert sheets.values.return_value.update.call_args.kwargs["range"] == "'DOWNTOWN'!F2"


def test_name_column_fallback_when_sheet_email_differs():
    """Sheet rows often hold an Indeed relay address while CG1 has the real
    one — the NAME-column fallback must still find the row (last match wins
    for repeat hires)."""
    rows = [
        ["WE", "INTERVIEWER", "NAME", "EMAIL", "PHONE", "ATTENDED DAY 2"],
        ["WE 6/28/26", "X", "Sage Mockley", "sm_relay@indeedemail.com", "1", "FALSE"],
        ["WE 8/16/26", "X", "Sage Mockley", "sm_relay2@indeedemail.com", "1", "FALSE"],
    ]
    svc, sheets = _mock_service(rows)
    with patch.object(ss, "_build_service", return_value=svc):
        assert ss.mark_attended("downtown", "sagemockley00@example.com", "Sage Mockley", day=2) is True
    assert sheets.values.return_value.update.call_args.kwargs["range"] == "'DOWNTOWN'!F3"


def test_exact_name_cell_match_beats_later_substring_match():
    """Hire 'Indigo' (bare first name, no email hit) must tick her own row,
    not a later 'Indigo Varnley' whose NAME cell merely contains the name."""
    rows = [
        ["WE", "INTERVIEWER", "NAME", "EMAIL", "PHONE", "ATTENDED DAY 2"],
        ["WE 4/12/26", "Boss", "Indigo", "relay1@indeedemail.com", "1", "FALSE"],
        ["WE 8/23/26", "Boss", "Indigo Varnley", "relay2@indeedemail.com", "1", "FALSE"],
    ]
    svc, sheets = _mock_service(rows)
    with patch.object(ss, "_build_service", return_value=svc):
        assert ss.mark_attended("downtown", "indigo@x.com", "Indigo", day=2) is True
    assert sheets.values.return_value.update.call_args.kwargs["range"] == "'DOWNTOWN'!F2"


def test_riverside_name_column_fallback():
    """Riverside's NAME column is B (live header 'NEW HIRE NAME'); the name
    fallback must work there too when the sheet email differs."""
    rows = [
        ["Weeking date", "NEW HIRE NAME", "NUMBER", "EMAIL", "ATTENDED DAY 1", "ATTENDED DAY 2"],
        ["8/16/26", "Toby Northwind", "1", "", "TRUE", "FALSE"],
    ]
    svc, sheets = _mock_service(rows)
    with patch.object(ss, "_build_service", return_value=svc):
        assert ss.mark_attended("riverside", "toby.n@example.com", "Toby Northwind", day=2) is True
    assert sheets.values.return_value.update.call_args.kwargs["range"] == "'RIVERSIDE'!F2"


def test_name_col_discovered_by_header_survives_inserted_column():
    """If someone inserts a column, the NAME column is found via its header
    ('NEW HIRE NAME'/'NAME'), not the static layout position."""
    rows = [
        ["WE", "INTERVIEWER", "SOURCE", "NEW HIRE NAME", "EMAIL", "ATTENDED DAY 2"],
        ["WE 8/23/26", "Boss", "Indeed", "Emery Testwood", "relay@indeedemail.com", "FALSE"],
    ]
    svc, sheets = _mock_service(rows)
    with patch.object(ss, "_build_service", return_value=svc):
        assert ss.mark_attended("downtown", "emery.t@example.com", "Emery Testwood", day=2) is True
    assert sheets.values.return_value.update.call_args.kwargs["range"] == "'DOWNTOWN'!F2"


def test_empty_email_name_only_still_ticks():
    """Path-B callers may send an empty email (hire with no login) — the
    NAME-column fallback alone must carry the match."""
    rows = [
        ["WE", "INTERVIEWER", "NAME", "EMAIL", "PHONE", "ATTENDED DAY 2"],
        ["WE 8/23/26", "Boss", "Emery Testwood", "", "1", "FALSE"],
    ]
    svc, sheets = _mock_service(rows)
    with patch.object(ss, "_build_service", return_value=svc):
        assert ss.mark_attended("downtown", "", "Emery Testwood", day=2) is True
    assert sheets.values.return_value.update.call_args.kwargs["range"] == "'DOWNTOWN'!F2"
