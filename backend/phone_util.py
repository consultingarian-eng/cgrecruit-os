"""Phone numbers: the one place that knows which country a bare number is in.

Candidates type numbers the way they say them ("(617) 555-0134",
"07700 900123"), but Twilio and ElevenLabs need E.164 ("+16175550134",
"+447700900123"). `PHONE_DEFAULT_COUNTRY` says how to read a number typed
without a country code:

  * ``US`` (the default; ``CA`` behaves the same) - 10 digits get +1, and so
    do 11 digits starting with 1.
  * ``GB`` (``UK`` is accepted too) - 0 + 10 digits ("07700 900123") becomes
    +44 7700 900123, 44 + 10 digits gets its +, and 10 digits that look like a
    UK number with the leading 0 dropped ("7700 900123") get +44.

A number that starts with + (or the international prefix 00) is kept as typed
in either mode, so overseas candidates can always give a full number.

Anything that can't be read confidently is returned unchanged: intake must
never destroy what a candidate typed, and a person can still fix it.
"""
from __future__ import annotations

import os
import re

# 10-15 digits: shorter numbers exist only in a handful of tiny territories,
# and a short "+" number here is far more often a typo.
_E164 = re.compile(r"\+[1-9]\d{9,14}")


def default_country() -> str:
    """'US' or 'GB', from PHONE_DEFAULT_COUNTRY (anything else reads as US)."""
    c = (os.environ.get("PHONE_DEFAULT_COUNTRY") or "US").strip().upper()
    if c == "UK":
        c = "GB"
    if c == "CA":
        c = "US"
    return c if c in ("US", "GB") else "US"


def normalize_phone_e164(phone: str, country: str | None = None) -> str:
    raw = (phone or "").strip()
    if not raw:
        return raw
    # "+44 (0)7700 900123": the bracketed 0 is dialled only from inside the country.
    cleaned = re.sub(r"\(\s*0\s*\)", "", raw) if raw.startswith(("+", "00")) else raw
    digits = re.sub(r"\D", "", cleaned)
    c = (country or default_country()).upper()

    # A leading + (or the 00 international prefix) means the country code is
    # already there, whatever the default country.
    if raw.startswith("+"):
        return f"+{digits}" if 10 <= len(digits) <= 15 else raw
    if raw.startswith("00") and 10 <= len(digits) - 2 <= 15:
        return f"+{digits[2:]}"

    if c == "GB":
        if len(digits) == 11 and digits.startswith("0"):
            return f"+44{digits[1:]}"
        if len(digits) == 12 and digits.startswith("44"):
            return f"+{digits}"
        if len(digits) == 10 and digits[0] in "123578":
            return f"+44{digits}"
        return raw

    # US / Canada (NANP).
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    return raw


def is_e164(phone: str) -> bool:
    """True for a full international number: + then 10 to 15 digits."""
    return bool(_E164.fullmatch(phone or ""))


def same_phone(a: str, b: str) -> bool:
    """Do two stored/typed numbers reach the same phone? True when the E.164
    forms match, or when both have at least 10 digits and the last 10 agree
    ("+447700900123" and "07700 900123", "+12035551234" and "203.555.1234")."""
    na, nb = normalize_phone_e164(a or ""), normalize_phone_e164(b or "")
    if is_e164(na) and na == nb:
        return True
    da, db_ = re.sub(r"\D", "", a or ""), re.sub(r"\D", "", b or "")
    if not da or not db_:
        return False
    if len(da) >= 10 and len(db_) >= 10:
        return da[-10:] == db_[-10:]
    return da == db_
