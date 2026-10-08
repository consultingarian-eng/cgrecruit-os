"""Google Sheets integration: append new starters to a NEW HIRES sheet.

Called when a candidate is moved to TRAINING in CGRecruit.
Appends a row to the correct office tab, grouping candidates by week-ending
Sunday with a blank-row separator between cohorts.

Optional. Turn it on with two env vars:
  NEW_HIRES_SHEET_ID       the spreadsheet id (the long string in its URL)
  GOOGLE_SA_JSON_CONTENT   a Google service-account JSON key (or GOOGLE_SA_JSON_PATH)
and share the sheet with the service account's email as an editor. Tabs and
column layouts are per office in backend/company_profile.json
(offices.<key>.sheet_tab / sheet_columns / sheet_week_prefix /
sheet_attended_headers). With no sheet id every function here is a no-op.
"""
import os
import json
import logging
from datetime import datetime, timedelta, timezone, date as date_type

import company_profile

logger = logging.getLogger(__name__)


def _sheet_id() -> str:
    return (os.getenv("NEW_HIRES_SHEET_ID") or "").strip()


def _office_tabs() -> dict:
    return {k: o["sheet_tab"] for k, o in company_profile.offices().items() if o.get("sheet_tab")}


def _office_columns() -> dict:
    return {k: list(o.get("sheet_columns") or ["we_date", "name", "email", "phone"])
            for k, o in company_profile.offices().items()}


def _attended_aliases() -> dict:
    out = {}
    for k, o in company_profile.offices().items():
        aliases = o.get("sheet_attended_headers") or {}
        if aliases:
            out[k] = {int(day): list(names) for day, names in aliases.items()}
    return out


# Tab name per office key, column layout per office (values written left→right)
# and per-office attendance header aliases ("MONDAY ATTEND" instead of
# "ATTENDED DAY 1"). All from the company profile.
OFFICE_TABS = _office_tabs()
OFFICE_COLUMNS = _office_columns()
ATTENDED_HEADER_ALIASES = _attended_aliases()

# Headers the candidate-name column goes by on the live tabs.
NAME_HEADERS = {"NEW HIRE NAME", "NAME"}


def _find_name_col(all_rows, office_key: str) -> int | None:
    """Locate the NAME column by header cell, so an inserted column can't
    silently shift name matching; fall back to the static layout."""
    for row in all_rows:
        for j, cell in enumerate(row):
            if str(cell).upper().strip() in NAME_HEADERS:
                return j
    try:
        return OFFICE_COLUMNS[office_key].index("name")
    except (KeyError, ValueError):
        return None


def _build_service():
    if not _sheet_id():
        raise RuntimeError("NEW_HIRES_SHEET_ID not set — new-hires sheet sync is off")
    sa_json = os.getenv("GOOGLE_SA_JSON_CONTENT", "")
    sa_path = os.getenv("GOOGLE_SA_JSON_PATH", "")
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    if sa_json:
        info = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    elif sa_path and os.path.exists(sa_path):
        creds = service_account.Credentials.from_service_account_file(sa_path, scopes=scopes)
    else:
        raise RuntimeError("Google SA credentials not configured (GOOGLE_SA_JSON_CONTENT or GOOGLE_SA_JSON_PATH)")
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _week_ending_sunday(d: date_type) -> date_type:
    """Return the Sunday that ends the ISO week containing d."""
    days_until_sunday = (6 - d.weekday()) % 7
    return d + timedelta(days=days_until_sunday)


def _format_we_date(we: date_type, office: str) -> str:
    s = f"{we.month}/{we.day:02d}/{str(we.year)[2:]}"
    prefix = company_profile.office(office).get("sheet_week_prefix") or ""
    return f"{prefix}{s}"


def _parse_we_cell(value) -> date_type | None:
    """Read whatever a week-ending cell actually holds into a date.

    The live tabs carry at least four spellings of the same week, because
    people type them by hand and we changed our own format over time:
    "WE 9/13/26", "9/13/26", "09/06/26", "11/2/2025". Matching those as
    STRINGS is what spawned duplicate cohort blocks — a week written one way
    never matched the same week written another, so every booking opened a
    fresh block at the bottom of the tab. Compare dates, never text.
    """
    if value is None:
        return None
    if isinstance(value, date_type):
        return value
    s = str(value).strip()
    if not s:
        return None
    if s.upper().startswith("WE"):
        s = s[2:].strip()
    s = s.replace("-", "/").strip()
    parts = [p for p in s.split("/") if p != ""]
    if len(parts) != 3:
        return None
    try:
        a, b, c = (int(p) for p in parts)
    except ValueError:
        return None
    # ISO-ish (2026/09/13) vs US (9/13/26 or 9/13/2026)
    if a > 31:
        year, month, day = a, b, c
    else:
        month, day, year = a, b, c
    if year < 100:
        year += 2000
    try:
        return date_type(year, month, day)
    except ValueError:
        return None


# Checkbox columns (ATTENDED DAY 1/2, ONBOARDING COMPLETE …) have validation
# applied down the whole tab, so an inserted separator row comes back from the
# API as ['', '', '', '', '', 'FALSE', 'FALSE', 'FALSE'] rather than []. The
# sheet's own existing separators look exactly like that. Treating them as
# data would make every insert lay down a second separator.
_CHECKBOX_VALUES = {"", "FALSE", "TRUE"}


def _row_is_blank(row) -> bool:
    """True for a cohort separator: nothing but empty cells and unticked
    checkboxes."""
    return all(str(c).strip().upper() in _CHECKBOX_VALUES for c in (row or []))


def _dated_rows(all_rows, header_idx: int) -> list:
    """[(row_index_0based, week_ending_date)] for every data row below the
    header that carries a parseable week-ending date."""
    out = []
    for i in range(header_idx + 1, len(all_rows)):
        d = _parse_we_cell((all_rows[i] or [None])[0] if all_rows[i] else None)
        if d:
            out.append((i, d))
    return out


def _header_index(all_rows) -> int:
    """Row holding the column headers, so history above it is never touched."""
    for i, row in enumerate(all_rows):
        for cell in (row or []):
            if str(cell).upper().strip() in NAME_HEADERS:
                return i
    return 0


def _last_content_row(all_rows) -> int:
    last = -1
    for i, row in enumerate(all_rows):
        if not _row_is_blank(row):
            last = i
    return last


def _target_index(all_rows, we: date_type, header_idx: int, exclude_row: int | None = None) -> tuple:
    """Where a row for week `we` belongs.

    Returns (insert_before_0based, needs_blank_before, needs_blank_after).

    Cohorts are kept in DATE order, which is the whole point: a booking for
    an earlier week must land above a later one, not at the bottom of the tab
    just because it was entered second.
    """
    dated = [(i, d) for i, d in _dated_rows(all_rows, header_idx) if i != exclude_row]

    same = [i for i, d in dated if d == we]
    if same:
        # Existing block for this week — slot in directly after its last row.
        return max(same) + 1, False, False

    later = [i for i, d in dated if d > we]
    if later:
        first_later = min(later)
        # A new block needs a blank on each side. There is normally already a
        # blank above the block we're landing in front of; only add one if not.
        above = all_rows[first_later - 1] if first_later - 1 > header_idx else None
        needs_before = above is not None and not _row_is_blank(above)
        return first_later, needs_before, True

    # Latest week so far — goes at the bottom, after one blank separator.
    last = _last_content_row(all_rows)
    return max(last + 1, header_idx + 1), True, False


def _find_person_row(all_rows, header_idx: int, email: str, name: str) -> int | None:
    """The row for this candidate, 0-indexed, or None.

    Email wins when present; a name match needs the full name to appear, and
    is only trusted when it is long enough not to collide (the bare-first-name
    hazard that has bitten this sheet before).
    """
    email_l = (email or "").lower().strip()
    name_l = (name or "").lower().strip()
    found = None
    for i in range(header_idx + 1, len(all_rows)):
        row_text = " ".join(str(c) for c in (all_rows[i] or [])).lower()
        if email_l and email_l in row_text:
            found = i
        elif name_l and len(name_l) > 3 and name_l in row_text:
            found = i
    return found


def _insert_blank_rows(sheets, gid: int, at_0based: int, count: int) -> None:
    if count <= 0:
        return
    sheets.batchUpdate(
        spreadsheetId=_sheet_id(),
        body={"requests": [{
            "insertDimension": {
                "range": {"sheetId": gid, "dimension": "ROWS",
                          "startIndex": at_0based, "endIndex": at_0based + count},
                "inheritFromBefore": False,
            }
        }]},
    ).execute()


def _move_row(sheets, gid: int, from_0based: int, to_0based: int) -> None:
    """Physically move one row. moveDimension is used rather than
    copy-then-clear so checkbox state, notes and per-row formatting travel
    with the person instead of being left behind on the old row.

    Google resolves destinationIndex against the coordinates BEFORE the row
    is lifted out, so moving down needs no adjustment and moving up is a
    plain insert-before.
    """
    if from_0based == to_0based or to_0based == from_0based + 1:
        return
    sheets.batchUpdate(
        spreadsheetId=_sheet_id(),
        body={"requests": [{
            "moveDimension": {
                "source": {"sheetId": gid, "dimension": "ROWS",
                           "startIndex": from_0based, "endIndex": from_0based + 1},
                "destinationIndex": to_0based,
            }
        }]},
    ).execute()


def append_new_hire(
    office_key: str,
    name: str,
    email: str,
    phone: str,
    training_start_at: str,
    interviewer: str = "",
) -> bool:
    """Put a new-hire row in the right week's block on the right office tab.

    If the candidate already has a row (rebooked, or the webhook fired twice),
    that row is MOVED to the new week rather than a second row being added —
    duplicate people in different cohort blocks was the other half of the
    "jumbled sheet" problem, and a duplicate drags its attendance ticks and
    incentive columns out of sync with the real one.

    Returns True on success, False on any error (non-fatal — caller logs and
    continues so a Sheets failure never blocks the training move)."""
    if not _sheet_id():
        return False  # sheet sync not configured (NEW_HIRES_SHEET_ID)
    tab = OFFICE_TABS.get(office_key)
    if not tab:
        logger.warning(f"sheets_service: unknown office '{office_key}' — skipping")
        return False

    try:
        start_date = _resolve_start_date(training_start_at)
        we = _week_ending_sunday(start_date)
        we_str = _format_we_date(we, office_key)

        svc = _build_service()
        sheets = svc.spreadsheets()
        gid = _get_sheet_gid(svc, tab)

        all_rows = sheets.values().get(
            spreadsheetId=_sheet_id(), range=f"'{tab}'!A1:Z",
            valueRenderOption="FORMATTED_VALUE",
        ).execute().get("values", [])
        header_idx = _header_index(all_rows)

        existing = _find_person_row(all_rows, header_idx, email, name)
        if existing is not None:
            logger.info(f"sheets_service: {name} already on {tab} row {existing + 1} — moving to {we_str}")
            return _reseat_row(sheets, gid, tab, all_rows, header_idx, existing, we, we_str)

        fields = {
            "we_date": we_str, "interviewer": interviewer,
            "name": name, "email": email, "phone": phone,
        }
        row = [fields.get(col, "") for col in OFFICE_COLUMNS.get(office_key, [])]

        target, blank_before, blank_after = _target_index(all_rows, we, header_idx)
        _insert_blank_rows(sheets, gid, target, 1 + (1 if blank_before else 0) + (1 if blank_after else 0))
        write_idx = target + (1 if blank_before else 0)

        sheets.values().update(
            spreadsheetId=_sheet_id(),
            range=f"'{tab}'!A{write_idx + 1}",
            valueInputOption="USER_ENTERED",
            body={"values": [row]},
        ).execute()

        logger.info(f"sheets_service: wrote {name} to {tab} row {write_idx + 1} (WE={we_str})")
        return True

    except Exception as e:
        logger.error(f"sheets_service: failed to append {name} to {tab}: {e}")
        return False


def _resolve_start_date(training_start_at: str) -> date_type:
    """Training start as an ET calendar date, defaulting to today."""
    if training_start_at:
        try:
            from server import _parse_training_ts
            return _parse_training_ts(training_start_at).date()
        except Exception:
            return date_type.today()
    return date_type.today()


def _reseat_row(sheets, gid: int, tab: str, all_rows, header_idx: int,
                row_idx: int, we: date_type, we_str: str) -> bool:
    """Write the new week onto an existing row and move it into that week's
    block. Rewriting the date in place was the original bug: the person's
    week changed but their row stayed sitting inside the old cohort, which is
    exactly what "jumbled up" looked like on the tab."""
    sheets.values().update(
        spreadsheetId=_sheet_id(),
        range=f"'{tab}'!A{row_idx + 1}",
        valueInputOption="USER_ENTERED",
        body={"values": [[we_str]]},
    ).execute()

    # Recompute placement with the row's NEW date in place, but ignore the row
    # itself so it can't anchor its own block.
    if row_idx < len(all_rows) and all_rows[row_idx]:
        all_rows[row_idx][0] = we_str
    target, blank_before, blank_after = _target_index(all_rows, we, header_idx, exclude_row=row_idx)

    if blank_before or blank_after:
        # Starting a brand-new block: lay the separators down first, then move
        # the row between them. Inserting above the row shifts it down.
        _insert_blank_rows(sheets, gid, target, (1 if blank_before else 0) + (1 if blank_after else 0))
        if target <= row_idx:
            row_idx += (1 if blank_before else 0) + (1 if blank_after else 0)
        target += (1 if blank_before else 0)

    _move_row(sheets, gid, row_idx, target)
    logger.info(f"sheets_service: moved row {row_idx + 1} → {target + 1} on {tab} (WE={we_str})")
    return True


def update_hire_start_date(
    office_key: str,
    email: str,
    name: str,
    training_start_at: str,
) -> bool:
    """Reschedule: set the new week-ending date AND move the row into that
    week's block, so the tab stays grouped by cohort. Non-fatal."""
    if not _sheet_id():
        return False  # sheet sync not configured (NEW_HIRES_SHEET_ID)
    tab = OFFICE_TABS.get(office_key)
    if not tab:
        logger.warning(f"sheets_service.update_hire_start_date: unknown office '{office_key}'")
        return False
    try:
        start_date = _resolve_start_date(training_start_at)
        we = _week_ending_sunday(start_date)
        we_str = _format_we_date(we, office_key)

        svc = _build_service()
        sheets = svc.spreadsheets()
        gid = _get_sheet_gid(svc, tab)

        all_rows = sheets.values().get(
            spreadsheetId=_sheet_id(), range=f"'{tab}'!A1:Z",
            valueRenderOption="FORMATTED_VALUE",
        ).execute().get("values", [])
        header_idx = _header_index(all_rows)

        row_idx = _find_person_row(all_rows, header_idx, email, name)
        if row_idx is None:
            logger.warning(f"sheets_service.update_hire_start_date: {name} ({email}) not found in {tab}")
            return False

        return _reseat_row(sheets, gid, tab, all_rows, header_idx, row_idx, we, we_str)

    except Exception as e:
        logger.error(f"sheets_service.update_hire_start_date failed for {name}: {e}")
        return False


def _get_sheet_gid(svc, tab_name: str) -> int:
    """Look up the numeric sheetId for a tab by name."""
    meta = svc.spreadsheets().get(spreadsheetId=_sheet_id()).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == tab_name:
            return s["properties"]["sheetId"]
    raise ValueError(f"Tab '{tab_name}' not found in sheet")


def _col_letter(idx: int) -> str:
    """Convert 0-indexed column number to A1 letter (0→A, 25→Z, 26→AA …)."""
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def update_hire_email(office_key: str, old_email: str, name: str, new_email: str) -> bool:
    """Overwrite the email cell on a new hire's row (used when an Indeed relay
    address is replaced with the person's real email in CG1).

    Finds the row by old email or name (last match, like mark_attended), then
    rewrites the cell that actually holds the old address — falling back to
    the office's fixed email column if no cell matches. Non-fatal.
    """
    if not _sheet_id():
        return False  # sheet sync not configured (NEW_HIRES_SHEET_ID)
    tab = OFFICE_TABS.get(office_key)
    if not tab:
        logger.warning(f"sheets_service.update_hire_email: unknown office '{office_key}'")
        return False
    try:
        svc = _build_service()
        sheets = svc.spreadsheets()

        result = sheets.values().get(
            spreadsheetId=_sheet_id(),
            range=f"'{tab}'!A1:Z",
            valueRenderOption="FORMATTED_VALUE",
        ).execute()
        all_rows = result.get("values", [])

        old_l = (old_email or "").lower().strip()
        name_l = (name or "").lower().strip()
        match_row: int | None = None
        for i, row in enumerate(all_rows):
            row_text = " ".join(str(c) for c in row).lower()
            if (old_l and old_l in row_text) or (name_l and len(name_l) > 3 and name_l in row_text):
                match_row = i + 1  # 1-indexed, prefer last match
        if match_row is None:
            logger.warning(f"sheets_service.update_hire_email: {name} ({old_email}) not found in {tab}")
            return False

        # Prefer the cell that literally contains the old address — robust to
        # tabs whose column order has drifted from OFFICE_COLUMNS.
        row_cells = all_rows[match_row - 1]
        email_col_idx: int | None = None
        if old_l:
            for j, cell in enumerate(row_cells):
                if old_l in str(cell).lower():
                    email_col_idx = j
                    break
        if email_col_idx is None:
            cols = OFFICE_COLUMNS.get(office_key, [])
            email_col_idx = cols.index("email") if "email" in cols else None
        if email_col_idx is None:
            logger.warning(f"sheets_service.update_hire_email: no email column resolvable in {tab}")
            return False

        cell_ref = f"'{tab}'!{_col_letter(email_col_idx)}{match_row}"
        sheets.values().update(
            spreadsheetId=_sheet_id(),
            range=cell_ref,
            valueInputOption="USER_ENTERED",
            body={"values": [[new_email]]},
        ).execute()
        logger.info(f"sheets_service: email for {name} in {tab} updated at {cell_ref} -> {new_email}")
        return True

    except Exception as e:
        logger.error(f"sheets_service.update_hire_email failed for {name}: {e}")
        return False


def mark_attended(office_key: str, email: str, name: str, day: int = 1) -> bool:
    """Tick the ATTENDED DAY {day} checkbox for a candidate.

    Searches the office tab for a row whose email or name matches, then
    finds the column whose header matches "ATTENDED DAY {day}" (or an
    office-specific alias, e.g. "MONDAY ATTEND") and sets
    that cell to TRUE.  Non-fatal: returns False on any error.
    """
    if not _sheet_id():
        return False  # sheet sync not configured (NEW_HIRES_SHEET_ID)
    tab = OFFICE_TABS.get(office_key)
    if not tab:
        logger.warning(f"sheets_service.mark_attended: unknown office '{office_key}'")
        return False
    try:
        svc = _build_service()
        sheets = svc.spreadsheets()

        # Read the full tab, columns A–Z (unbounded rows — a busy tab is
        # well past 500 rows, which the old A1:Z500 range silently missed)
        result = sheets.values().get(
            spreadsheetId=_sheet_id(),
            range=f"'{tab}'!A1:Z",
            valueRenderOption="FORMATTED_VALUE",
        ).execute()
        all_rows = result.get("values", [])

        # Find the attendance column by scanning all rows for a header match.
        target_headers = {f"ATTENDED DAY {day}"}
        target_headers.update(ATTENDED_HEADER_ALIASES.get(office_key, {}).get(day, []))
        attended_col_idx: int | None = None
        for row in all_rows:
            for j, cell in enumerate(row):
                if str(cell).upper().strip() in target_headers:
                    attended_col_idx = j
                    break
            if attended_col_idx is not None:
                break

        if attended_col_idx is None:
            logger.warning(f"sheets_service.mark_attended: no {'/'.join(sorted(target_headers))} column in {tab}")
            return False

        col_letter = _col_letter(attended_col_idx)

        # Find the candidate row. An email match anywhere in the row is
        # authoritative. The name fallback only looks at the NAME column —
        # located by its header ("NEW HIRE NAME" on both live tabs), falling
        # back to the static layout — because a bare first name can equal an
        # interviewer's name, which appears on every row that interviewer
        # handled; whole-row matching once picked a different candidate's row
        # because of exactly that. Within the NAME column an exact cell match
        # beats a substring hit, so hire "Rowan" can't land on a later
        # "Rowan Sample". Prefer the LAST match in each class so repeat hires
        # get the most recent row ticked.
        email_l = (email or "").lower().strip()
        name_l = (name or "").lower().strip()
        name_col = _find_name_col(all_rows, office_key)
        email_row: int | None = None
        exact_name_row: int | None = None
        sub_name_row: int | None = None
        for i, row in enumerate(all_rows):
            row_text = " ".join(str(c) for c in row).lower()
            if email_l and email_l in row_text:
                email_row = i + 1  # 1-indexed
            if (name_l and len(name_l) > 3 and name_col is not None
                    and len(row) > name_col):
                cell = str(row[name_col]).lower().strip()
                if cell == name_l:
                    exact_name_row = i + 1
                elif name_l in cell:
                    sub_name_row = i + 1
        match_row = email_row or exact_name_row or sub_name_row

        if match_row is None:
            logger.warning(f"sheets_service.mark_attended: {name} ({email}) not found in {tab}")
            return False

        cell_ref = f"'{tab}'!{col_letter}{match_row}"
        sheets.values().update(
            spreadsheetId=_sheet_id(),
            range=cell_ref,
            valueInputOption="USER_ENTERED",
            body={"values": [[True]]},
        ).execute()
        logger.info(f"sheets_service: ticked day {day} for {name} in {tab} at {cell_ref}")
        return True

    except Exception as e:
        logger.error(f"sheets_service.mark_attended failed for {name}: {e}")
        return False


def remove_new_hire(office_key: str, email: str, name: str) -> bool:
    """Blank a withdrawn starter's row on the NEW HIRES tab.

    Cleared, not deleted: deleting shifts every row below it, which would move
    other people's attendance checkboxes out from under them if the office has
    the tab open. A cleared row reads as a gap inside the cohort and the next
    booking for that week fills it or sits after it.

    Non-fatal — returns False and logs if the person isn't found."""
    if not _sheet_id():
        return False  # sheet sync not configured (NEW_HIRES_SHEET_ID)
    tab = OFFICE_TABS.get(office_key)
    if not tab:
        logger.warning(f"sheets_service.remove_new_hire: unknown office '{office_key}'")
        return False
    try:
        svc = _build_service()
        sheets = svc.spreadsheets()
        all_rows = sheets.values().get(
            spreadsheetId=_sheet_id(), range=f"'{tab}'!A1:Z",
            valueRenderOption="FORMATTED_VALUE",
        ).execute().get("values", [])
        header_idx = _header_index(all_rows)
        row_idx = _find_person_row(all_rows, header_idx, email, name)
        if row_idx is None:
            logger.info(f"sheets_service.remove_new_hire: {name} not on {tab} — nothing to clear")
            return False
        width = max(len(all_rows[row_idx]), len(OFFICE_COLUMNS.get(office_key, [])))
        sheets.values().update(
            spreadsheetId=_sheet_id(),
            range=f"'{tab}'!A{row_idx + 1}:{_col_letter(width - 1)}{row_idx + 1}",
            valueInputOption="USER_ENTERED",
            body={"values": [[""] * width]},
        ).execute()
        logger.info(f"sheets_service: cleared withdrawn starter {name} from {tab} row {row_idx + 1}")
        return True
    except Exception as e:
        logger.error(f"sheets_service.remove_new_hire failed for {name}: {e}")
        return False
