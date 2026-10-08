"""The deployment's home time zone (APP_TIMEZONE).

Every place that used to assume US Eastern reads it from here: the default for
an office's Region & Language time zone, the schedulers, call windows, quiet
hours, reminders and the dates and times written into texts and emails.

    APP_TIMEZONE=America/New_York   (default)
    APP_TIMEZONE=Europe/London      (UK offices)

An office's own Settings -> Region & Language time zone still wins wherever
the code has the office's settings to hand; APP_TIMEZONE is the fallback and
the zone for the background jobs that run for the whole deployment.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:  # read backend/.env when this is imported before deps.py
    from dotenv import load_dotenv

    import sys as _sys

    # Same rule as deps.py: tests never read the owner's backend/.env.
    if "pytest" not in _sys.modules and os.environ.get("CGRECRUIT_DOTENV", "1") != "0":
        load_dotenv(Path(__file__).parent / ".env")
except Exception:  # pragma: no cover - dotenv is a hard dependency elsewhere
    pass

logger = logging.getLogger("cgrecruit")

DEFAULT_TIMEZONE = "America/New_York"


def default_tz_name() -> str:
    """The IANA name from APP_TIMEZONE, or America/New_York when unset or
    unknown (an unknown name is logged, never fatal)."""
    name = (os.getenv("APP_TIMEZONE") or "").strip() or DEFAULT_TIMEZONE
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("APP_TIMEZONE=%r is not a known time zone; using %s", name, DEFAULT_TIMEZONE)
        return DEFAULT_TIMEZONE
    return name


def app_zone() -> ZoneInfo:
    return ZoneInfo(default_tz_name())


def tz_label(dt: datetime) -> str:
    """Short zone label for a time shown to people ("EST", "BST", "GMT")."""
    try:
        return dt.strftime("%Z") or default_tz_name()
    except Exception:
        return default_tz_name()
