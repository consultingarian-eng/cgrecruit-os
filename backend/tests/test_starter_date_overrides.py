"""Dated exceptions to the starter-email hours.

A one-off change to a single intake's training hours must reach exactly the
cohort starting on that date — not the pipeline template (which would leak into
every later intake booked before someone remembered to put it back), and not
only the cohort already booked (which would leave anyone booked afterwards
being told the normal hours).

These pin the resolution rules `_starter_date_override` exists to guarantee:
it is keyed on the ET calendar date of the start timestamp however that
timestamp was stored, and it outranks the per-person values the Training modal
posts back — the modal fills those from the pipeline template before the date
has been picked, so anything below them would be overwritten on every booking.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import _starter_date_override  # noqa: E402


SETTINGS = {
    "starter_date_overrides": {
        "2026-09-21": {"tuesday_start": "9:30 AM", "tuesday_end": "3:00 PM"},
    }
}


class TestDateMatching:
    def test_naive_local_timestamp_matches(self):
        # What the kanban modal and CG1's newer builds store.
        assert _starter_date_override(SETTINGS, "2026-09-21T13:00:00") == {
            "tuesday_start": "9:30 AM", "tuesday_end": "3:00 PM",
        }

    def test_utc_aware_timestamp_matches_the_same_day(self):
        # Older doors stored UTC. 13:00 ET on the 21st is 17:00Z on the 21st.
        assert _starter_date_override(SETTINGS, "2026-09-21T17:00:00Z")

    def test_utc_timestamp_that_rolls_back_a_day_in_et(self):
        # 00:30Z on the 22nd is still 8:30 PM ET on the 21st. Slicing the
        # string would file this under the 22nd and miss the exception.
        assert _starter_date_override(SETTINGS, "2026-09-22T00:30:00Z")

    def test_other_dates_are_untouched(self):
        assert _starter_date_override(SETTINGS, "2026-09-28T13:00:00") == {}

    def test_no_overrides_configured(self):
        assert _starter_date_override({}, "2026-09-21T13:00:00") == {}
        assert _starter_date_override({"starter_date_overrides": None}, "2026-09-21T13:00:00") == {}

    def test_missing_start_date(self):
        assert _starter_date_override(SETTINGS, "") == {}

    def test_unparseable_timestamp_falls_back_to_the_date_prefix(self):
        assert _starter_date_override(SETTINGS, "2026-09-21 not-a-time")

    def test_blank_override_values_are_dropped(self):
        # An emptied field must fall through to the template, not blank the row.
        s = {"starter_date_overrides": {"2026-09-21": {"tuesday_start": "", "tuesday_end": "3:00 PM"}}}
        assert _starter_date_override(s, "2026-09-21T13:00:00") == {"tuesday_end": "3:00 PM"}


class TestPrecedence:
    """The merge order the two send paths apply, asserted on its result."""

    DEFAULTS = {"monday_start": "1:00 PM", "tuesday_start": "11:00 AM", "tuesday_end": "3:00 PM"}

    def _resolve(self, stored, extra, start):
        tpl = {**self.DEFAULTS, **stored, **extra}
        tpl.update(_starter_date_override(SETTINGS, start))
        return tpl

    def test_dated_exception_beats_the_pipeline_template(self):
        assert self._resolve({"tuesday_start": "11:00 AM"}, {}, "2026-09-21T13:00:00")["tuesday_start"] == "9:30 AM"

    def test_dated_exception_beats_the_per_person_modal_values(self):
        # The modal pre-fills 11:00 AM from the template and posts it back.
        assert self._resolve({}, {"tuesday_start": "11:00 AM"}, "2026-09-21T13:00:00")["tuesday_start"] == "9:30 AM"

    def test_untouched_fields_survive(self):
        tpl = self._resolve({}, {"monday_start": "12:30 PM"}, "2026-09-21T13:00:00")
        assert tpl["monday_start"] == "12:30 PM"

    def test_a_later_intake_keeps_the_normal_hours(self):
        assert self._resolve({}, {}, "2026-09-28T13:00:00")["tuesday_start"] == "11:00 AM"
