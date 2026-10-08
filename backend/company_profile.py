"""Your company, in one place.

Everything candidates and recruiters see that is specific to YOUR business —
the company name, the role, hours and pay wording, office addresses and map
links, the SMS number each office texts from, the starter-email defaults, the
new-hires spreadsheet layout — is read from here rather than written into the
code.

How to change it
----------------
Edit ``backend/company_profile.json`` (or set ``COMPANY_PROFILE_PATH`` to a
JSON file of your own, outside the repo). Restart the backend afterwards: the
file is read once at startup. Anything you leave out falls back to the
placeholder defaults below, which are deliberately generic.

Many of these are only *starting* values. The Settings screens (Recruiter
Profile, Screen Call Agent, Starter Email, Pipelines) save per-office values to
the database, and a saved value always wins over the profile.

Offices
-------
An "office" is a physical location your pipelines recruit for. Each pipeline is
tied to one office by its ``cg1_office_key`` field (the office picker under
Settings → Offices & Variants); if that is blank the office is guessed from the pipeline's name or
public slug using each office's ``match`` words. The office key is also the
key sent to the optional CG1 field-app hand-off, so keep the two in step.
"""
from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_DEFAULT_PATH = _HERE / "company_profile.json"

# Built-in fallbacks. Every value is a neutral placeholder: replace them in
# company_profile.json rather than here.
_STARTER_DEFAULTS: Dict[str, str] = {
    "monday_start": "9:30 AM", "monday_end": "1:00 PM",
    "tuesday_start": "9:30 AM", "tuesday_end": "1:00 PM",
    "regular_schedule": "10:00 AM – 6:00 PM",
    "dress_code": "Business smart",
    "id_text": "Please bring a form of photo ID.",
    "research_text": "",
    "food_text": "",
    "parking_text": "",
    "custom_notes": "",
}

_BUILTIN: Dict[str, Any] = {
    "company_name": "Your Company",
    "website": "",
    "contact_phone": "",
    "contact_email": "",
    "logo_url": "",
    "role_title": "Sales Representative",
    "work_schedule": "10:00 AM – 6:00 PM, Monday to Friday",
    "pay_summary": "",
    "about_company": "",
    "default_office": "",
    "field_app": {"name": "", "install_url": ""},
    "offices": {},
    "starter_template_defaults": _STARTER_DEFAULTS,
    "partner_feed": {"country": "", "campaign_prefix": "", "default_pin": ""},
}


def _load() -> Dict[str, Any]:
    path = Path(os.environ.get("COMPANY_PROFILE_PATH") or _DEFAULT_PATH)
    data: Dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:  # a broken file must not take the API down
            logger.error("company_profile: could not read %s (%s) — using built-in defaults", path, e)
            data = {}
    merged = copy.deepcopy(_BUILTIN)
    for k, v in data.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(merged.get(k), dict) and k != "offices":
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    # Normalise office keys and their match words to lower case.
    offices = {}
    for key, office in (merged.get("offices") or {}).items():
        office = dict(office or {})
        office["match"] = [str(m).lower() for m in (office.get("match") or [key])]
        offices[str(key).lower().strip()] = office
    merged["offices"] = offices
    return merged


PROFILE: Dict[str, Any] = _load()


def reload() -> Dict[str, Any]:
    """Re-read the profile (tests, or after editing the file in a shell)."""
    global PROFILE
    PROFILE = _load()
    return PROFILE


def get(key: str, default: Any = "") -> Any:
    v = PROFILE.get(key)
    return default if v in (None, "") else v


def company_name() -> str:
    """The name candidates see in texts, emails and on calls whenever a
    pipeline's own Recruiter Profile hasn't set one."""
    return PROFILE.get("company_name") or "Your Company"


# ── offices ───────────────────────────────────────────────────────────────────

def offices() -> Dict[str, Dict[str, Any]]:
    return PROFILE.get("offices") or {}


def office(office_key: Optional[str]) -> Dict[str, Any]:
    return offices().get((office_key or "").lower().strip()) or {}


def office_key_for_text(text: str) -> str:
    """Office whose `match` words appear in a pipeline name/slug, or ''."""
    t = (text or "").lower()
    if not t:
        return ""
    for key, o in offices().items():
        for word in o.get("match") or []:
            if word and word in t:
                return key
    return ""


def office_key_for_pipeline(pipe: Optional[Dict[str, Any]], fallback: str = "") -> str:
    """The office a pipeline recruits for.

    Its explicit ``cg1_office_key`` first, then the office whose match words
    appear in the public slug or name, then ``fallback``.
    """
    pipe = pipe or {}
    okey = (pipe.get("cg1_office_key") or "").lower().strip()
    if okey:
        return okey
    slug = (pipe.get("public_slug") or pipe.get("name") or "")
    return office_key_for_text(slug) or fallback


def default_office_key() -> str:
    """The office to assume when a pipeline matches none (may be '')."""
    d = (PROFILE.get("default_office") or "").lower().strip()
    if d:
        return d
    return next(iter(offices()), "")


def office_label(office_key: Optional[str]) -> str:
    o = office(office_key)
    return o.get("label") or (office_key or "unknown office")


def office_address(office_key: Optional[str]) -> str:
    return office(office_key).get("address") or ""


def office_maps_link(office_key: Optional[str]) -> str:
    return office(office_key).get("google_maps_link") or ""


def office_apple_maps_link(office_key: Optional[str]) -> str:
    return office(office_key).get("apple_maps_link") or ""


def office_sms_number(office_key: Optional[str]) -> str:
    return office(office_key).get("sms_number") or ""


def default_twilio_number() -> str:
    """The Twilio number used when neither the pipeline nor the office has one."""
    return (os.environ.get("TWILIO_PHONE_NUMBER") or "").strip()


def starter_template_defaults(office_key: Optional[str]) -> Dict[str, str]:
    base = dict(_STARTER_DEFAULTS)
    base.update(PROFILE.get("starter_template_defaults") or {})
    key = office_key if office(office_key) else default_office_key()
    base.update(office(key).get("starter_template") or {})
    return base


def public_offices() -> list:
    """[{key, label}] for the Settings UI's office pickers."""
    return [{"key": k, "label": o.get("label") or k} for k, o in offices().items()]


# ── deployment URLs (env only) ───────────────────────────────────────────────

def public_app_url() -> str:
    """Where the app is served, e.g. https://recruit.example.com (no trailing /).
    APP_PUBLIC_URL, with FRONTEND_BASE_URL accepted as an older name."""
    return (os.environ.get("APP_PUBLIC_URL") or os.environ.get("FRONTEND_BASE_URL") or "").strip().rstrip("/")


def backend_api_url() -> str:
    """The API base including /api, used in callback URLs handed to other
    systems. BACKEND_URL if set, else APP_PUBLIC_URL + /api, else ''."""
    explicit = (os.environ.get("BACKEND_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit if explicit.endswith("/api") else f"{explicit}/api"
    base = public_app_url()
    return f"{base}/api" if base else ""


def email_reply_domain() -> str:
    """Domain for per-candidate reply addresses (reply+<id>@<domain>), routed
    back in through SendGrid Inbound Parse. '' turns reply routing off."""
    return (os.environ.get("EMAIL_REPLY_DOMAIN") or "").strip()


def inbound_parse_domain() -> str:
    """Default email-intake domain (Settings → Email Intake can override)."""
    return (os.environ.get("INBOUND_PARSE_DOMAIN") or "inbox.example.com").strip()
