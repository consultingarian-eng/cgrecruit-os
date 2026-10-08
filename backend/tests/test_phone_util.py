"""phone_util: how a typed number becomes E.164, per PHONE_DEFAULT_COUNTRY."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phone_util import default_country, is_e164, normalize_phone_e164, same_phone  # noqa: E402


@pytest.mark.parametrize("typed,want", [
    ("(617) 555-0134", "+16175550134"),
    ("1 617 555 0134", "+16175550134"),
    ("+16175550134", "+16175550134"),
    ("+44 7700 900123", "+447700900123"),
    ("0044 7700 900123", "+447700900123"),
    ("07700 900123", "07700 900123"),      # a UK local number is not guessed in US mode
    ("12345", "12345"),
    ("", ""),
])
def test_us_default(monkeypatch, typed, want):
    monkeypatch.delenv("PHONE_DEFAULT_COUNTRY", raising=False)
    assert default_country() == "US"
    assert normalize_phone_e164(typed) == want


@pytest.mark.parametrize("typed,want", [
    ("07700 900123", "+447700900123"),
    ("020 7946 0958", "+442079460958"),
    ("447700900123", "+447700900123"),
    ("7700 900123", "+447700900123"),       # leading 0 dropped
    ("+44 (0)7700 900123", "+447700900123"),  # the bracketed trunk 0 is dropped
    ("+1 617 555 0134", "+16175550134"),    # overseas numbers still work
    ("0770090012", "0770090012"),           # too short: left for a human
])
def test_uk_mode(monkeypatch, typed, want):
    monkeypatch.setenv("PHONE_DEFAULT_COUNTRY", "UK")
    assert default_country() == "GB"
    assert normalize_phone_e164(typed) == want


def test_e164_check():
    assert is_e164("+447700900123") and is_e164("+16175550134")
    assert not is_e164("07700900123") and not is_e164("+316123456") and not is_e164("")


def test_same_phone(monkeypatch):
    monkeypatch.setenv("PHONE_DEFAULT_COUNTRY", "GB")
    assert same_phone("+447700900123", "07700 900123")
    assert same_phone("447700900123", "+447700900123")
    assert not same_phone("+447700900123", "+447700900124")
    monkeypatch.delenv("PHONE_DEFAULT_COUNTRY")
    assert same_phone("12035551234", "(203) 555-1234")
    assert not same_phone("", "+12035551234")
