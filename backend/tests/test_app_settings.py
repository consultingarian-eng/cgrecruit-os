"""Owner-facing settings: APP_TIMEZONE and the Claude model settings."""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/x")
os.environ.setdefault("DB_NAME", "x")

import app_tz  # noqa: E402
import llm_config  # noqa: E402


def test_timezone_defaults_to_new_york(monkeypatch):
    monkeypatch.delenv("APP_TIMEZONE", raising=False)
    assert app_tz.default_tz_name() == "America/New_York"


def test_uk_owners_set_europe_london(monkeypatch):
    monkeypatch.setenv("APP_TIMEZONE", "Europe/London")
    assert app_tz.default_tz_name() == "Europe/London"
    summer = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc).astimezone(app_tz.app_zone())
    assert summer.hour == 13 and app_tz.tz_label(summer) == "BST"
    # New offices' Region & Language default follows it.
    from models import RegionLanguageSettings
    assert RegionLanguageSettings().timezone == "Europe/London"


def test_an_unknown_zone_falls_back_safely(monkeypatch):
    monkeypatch.setenv("APP_TIMEZONE", "Mars/Olympus_Mons")
    assert app_tz.default_tz_name() == "America/New_York"


# Folders that hold other people's code (a venv inside backend/, installed
# packages, the built web app) are not ours to scan.
_SKIP_PARTS = {".venv", "venv", "env", "site-packages", "node_modules", "static", "__pycache__"}


def _own_py_files(root):
    for p in root.rglob("*.py"):
        if _SKIP_PARTS.intersection(p.relative_to(root).parts):
            continue
        yield p

def test_slot_labels_use_the_app_zone_by_default(monkeypatch):
    monkeypatch.setenv("APP_TIMEZONE", "Europe/London")
    from models import local_slot_label
    assert local_slot_label("2026-01-15T09:00:00Z", fmt="%H:%M") == "09:00"
    monkeypatch.setenv("APP_TIMEZONE", "America/New_York")
    assert local_slot_label("2026-01-15T09:00:00Z", fmt="%H:%M") == "04:00"


def test_no_hard_coded_eastern_time_left_in_the_backend():
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for p in _own_py_files(root):
        rel = p.relative_to(root).as_posix()
        if rel.startswith("tests/") or rel == "app_tz.py":
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if '"America/New_York"' in line or "'America/New_York'" in line:
                offenders.append(f"{rel}:{n}")
    assert not offenders, offenders


def test_model_settings(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.delenv("ANTHROPIC_FAST_MODEL", raising=False)
    assert llm_config.primary_model() == "claude-sonnet-5-5"
    assert llm_config.chat_models() == ("claude-sonnet-5-5", "claude-haiku-4-5")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("ANTHROPIC_FAST_MODEL", "claude-opus-5-5")
    assert llm_config.chat_models() == ("claude-opus-5-5",)


def test_no_model_ids_hard_coded_outside_llm_config():
    root = Path(__file__).resolve().parents[1]
    import re
    pat = re.compile(r"""["']claude-(sonnet|haiku|opus|fable)-[\w.-]*["']""")
    offenders = []
    for p in _own_py_files(root):
        rel = p.relative_to(root).as_posix()
        if rel.startswith("tests/") or rel == "llm_config.py":
            continue
        src = p.read_text(encoding="utf-8")
        for n, line in enumerate(src.splitlines(), 1):
            # The ElevenLabs voice-agent LLM picker lists ElevenLabs' own ids.
            if pat.search(line) and "elevenlabs" not in rel and rel != "server.py":
                offenders.append(f"{rel}:{n}")
    assert not offenders, offenders


def test_response_text_skips_thinking_blocks():
    sdk = SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking=""),
                                   SimpleNamespace(type="text", text="Hello"),
                                   SimpleNamespace(type="text", text=" there")])
    assert llm_config.response_text(sdk) == "Hello there"
    raw = {"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "Hi"}]}
    assert llm_config.response_text(raw) == "Hi"
    assert llm_config.response_text(None) == ""


def test_low_latency_params_only_where_supported():
    assert llm_config.low_latency_params("claude-sonnet-5-5") == {"extra_body": {"output_config": {"effort": "low"}}}
    assert llm_config.low_latency_params("claude-haiku-4-5") == {}
