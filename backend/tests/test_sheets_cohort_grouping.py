"""NEW HIRES sheet: rows must sit in their week's block, in date order.

Three real failures on live tabs drove this ("people in all
jumbled up dates"):

  1. A week typed one way ("09/06/26") never matched the same week written
     another ("9/6/26" / "WE 9/06/26"), so a booking opened a fresh block at
     the bottom instead of joining the one already there.
  2. A new week was always appended to the END of the tab, so a booking for
     an earlier week landed below a later one.
  3. Rescheduling rewrote the date cell in place and left the person sitting
     inside their OLD cohort — and when the webhook came in as a fresh
     "moved to training", it added a SECOND row for the same person.
"""
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# A frozen company profile (offices "downtown" and "riverside") and a dummy
# sheet id, so these tests never depend on your own configuration.
import os  # noqa: E402
os.environ["COMPANY_PROFILE_PATH"] = str(Path(__file__).resolve().parent / "company_profile.test.json")
os.environ.setdefault("NEW_HIRES_SHEET_ID", "test-sheet-id")

import sheets_service as ss


HEADER = ["Weekending date", "Interviewer", "NEW HIRE NAME", "EMAIL", "NUMBER"]


def _row(we, name, email=""):
    return [we, "Recruiter", name, email, "555"]


def _mock(rows):
    svc = MagicMock()
    sheets = svc.spreadsheets.return_value
    sheets.values.return_value.get.return_value.execute.return_value = {"values": rows}
    svc.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "DOWNTOWN", "sheetId": 0}}]
    }
    return svc, sheets


def _requests(sheets):
    """Every batchUpdate request body issued, flattened."""
    out = []
    for call in sheets.batchUpdate.call_args_list:
        out.extend(call.kwargs["body"]["requests"])
    return out


# ── 1. date parsing, not string matching ────────────────────────────────────


def test_every_spelling_of_a_week_parses_to_one_date():
    for text in ("WE 9/13/26", "9/13/26", "09/13/26", "9/13/2026", "2026/09/13", "WE 09/13/2026"):
        assert ss._parse_we_cell(text) == date(2026, 9, 13), text


def test_junk_cells_parse_to_none():
    for text in ("", None, "NAME", "week ending", "13/13/26", "9/13"):
        assert ss._parse_we_cell(text) is None, text


def test_week_ending_sunday_is_the_sunday_of_that_week():
    assert ss._week_ending_sunday(date(2026, 9, 14)) == date(2026, 9, 20)   # Monday
    assert ss._week_ending_sunday(date(2026, 9, 20)) == date(2026, 9, 20)   # already Sunday


# ── 2. placement is chronological ───────────────────────────────────────────


def test_joins_existing_block_even_when_written_differently():
    rows = [HEADER,
            _row("WE 9/06/26", "Early Bird"),
            [],
            _row("9/13/2026", "Existing One"),      # same week, different spelling
            _row("09/13/26", "Existing Two")]
    target, before, after = ss._target_index(rows, date(2026, 9, 13), 0)
    assert (target, before, after) == (5, False, False)   # straight after row 5


def test_earlier_week_slots_above_a_later_block_not_at_the_bottom():
    rows = [HEADER,
            _row("WE 9/06/26", "A"),
            [],
            _row("WE 9/27/26", "C")]
    target, before, after = ss._target_index(rows, date(2026, 9, 13), 0)
    # Lands in front of the 9/27 block, with a blank after it. The blank above
    # already exists, so none is added there.
    assert target == 3 and before is False and after is True


def test_latest_week_goes_to_the_bottom_after_a_separator():
    rows = [HEADER, _row("WE 9/06/26", "A"), _row("WE 9/06/26", "B")]
    target, before, after = ss._target_index(rows, date(2026, 10, 4), 0)
    assert target == 3 and before is True and after is False


def test_history_above_the_header_is_never_a_placement_anchor():
    rows = [["MC", "MC"], [], HEADER, _row("WE 9/06/26", "A")]
    assert ss._header_index(rows) == 2
    assert ss._dated_rows(rows, 2) == [(3, date(2026, 9, 6))]


# ── 3. rescheduling moves the row, and never duplicates the person ──────────


def test_reschedule_moves_the_row_into_the_new_block():
    rows = [HEADER,
            _row("WE 9/13/26", "Stayer One", "a@x.com"),
            _row("WE 9/13/26", "Mover Person", "mover@x.com"),   # row index 2
            [],
            _row("WE 9/27/26", "Later One", "c@x.com")]
    svc, sheets = _mock(rows)
    with patch.object(ss, "_build_service", return_value=svc):
        assert ss.update_hire_start_date("downtown", "mover@x.com", "Mover Person",
                                         "2026-09-21T09:00:00") is True
    moves = [r["moveDimension"] for r in _requests(sheets) if "moveDimension" in r]
    assert len(moves) == 1, "the row must be physically moved, not just relabelled"
    assert moves[0]["source"]["startIndex"] == 2
    # The date cell is rewritten too.
    dates = [c for c in sheets.values.return_value.update.call_args_list
             if c.kwargs["body"]["values"] == [["WE 9/27/26"]]]
    assert dates, "new week-ending date must be written"


def test_rebooking_an_existing_person_moves_them_instead_of_adding_a_row():
    rows = [HEADER,
            _row("WE 9/13/26", "Dupe Risk", "dupe@x.com"),
            [],
            _row("WE 9/27/26", "Someone Else", "z@x.com")]
    svc, sheets = _mock(rows)
    with patch.object(ss, "_build_service", return_value=svc):
        assert ss.append_new_hire("downtown", "Dupe Risk", "dupe@x.com", "555",
                                  "2026-10-01T09:00:00") is True
    # No full candidate row written anywhere — only the date cell.
    written = [c.kwargs["body"]["values"][0] for c in sheets.values.return_value.update.call_args_list]
    assert all(len(v) == 1 for v in written), f"a duplicate row was written: {written}"
    assert any("moveDimension" in r for r in _requests(sheets))


def test_a_genuinely_new_person_is_written_as_a_row():
    rows = [HEADER, _row("WE 9/13/26", "Existing", "e@x.com")]
    svc, sheets = _mock(rows)
    with patch.object(ss, "_build_service", return_value=svc):
        assert ss.append_new_hire("downtown", "Brand New", "new@x.com", "555",
                                  "2026-09-14T09:00:00") is True
    written = [c.kwargs["body"]["values"][0] for c in sheets.values.return_value.update.call_args_list]
    assert any("Brand New" in v for v in written), written


# ── 4. a jumbled tab, reproduced ─────────────────────────────────────────────


def test_a_jumbled_tab_would_have_been_placed_correctly():
    """A real tab's shape: a 9/27 row sat inside the 9/20 block because a
    reschedule relabelled it in place."""
    rows = [HEADER]
    rows += [_row("WE 9/20/26", f"Week20 #{i}") for i in range(5)]
    rows += [_row("WE 9/27/26", "Galen Brightwell")]   # the stray
    rows += [_row("WE 9/20/26", "Juno Ashgrove")]
    rows += [[]]
    rows += [_row("WE 9/27/26", f"Week27 #{i}") for i in range(4)]

    # A 9/27 person belongs in the 9/27 block (last row index 12), not row 6.
    target, _, _ = ss._target_index(rows, date(2026, 9, 27), 0, exclude_row=6)
    assert target == 13, "a 9/27 row belongs after the last 9/27 row"

    # And a fresh 9/20 booking still joins the 9/20 block.
    target20, _, _ = ss._target_index(rows, date(2026, 9, 20), 0)
    assert target20 == 8


# ── 5. separators carry inherited checkboxes ────────────────────────────────


def test_separator_rows_with_inherited_checkboxes_read_as_blank():
    """The live tabs return separators as ['', …, 'FALSE', 'FALSE', 'FALSE']
    because the attendance columns have checkbox validation applied all the
    way down. Counting those as data would stack up a second blank row on
    every insert."""
    assert ss._row_is_blank(["", "", "", "", "", "FALSE", "FALSE", "FALSE"]) is True
    assert ss._row_is_blank([]) is True
    assert ss._row_is_blank(["", "", "Real Person"]) is False
    assert ss._row_is_blank(["WE 9/13/26"]) is False


def test_existing_separator_is_not_doubled_up():
    rows = [HEADER,
            _row("WE 9/06/26", "A"),
            ["", "", "", "", "", "FALSE", "FALSE", "FALSE"],   # existing separator
            _row("WE 9/27/26", "C")]
    target, before, after = ss._target_index(rows, date(2026, 9, 13), 0)
    assert before is False, "a separator already sits above the 9/27 block"
    assert target == 3 and after is True
