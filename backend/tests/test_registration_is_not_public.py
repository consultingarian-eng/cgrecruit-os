"""Account creation is not self-serve.

A user with no parent_user_id is treated as a tenant owner and normalised to
super_admin, so an open /api/auth/register handed anyone on the internet a
super-admin account. It now requires an existing super-admin — except on a
deployment with no users, which still needs to be bootstrappable.
"""
import inspect
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/x")
os.environ.setdefault("DB_NAME", "x")
os.environ.setdefault("JWT_SECRET", "unit-test-secret-with-32-plus-chars!!")


def _register_source():
    import server
    return inspect.getsource(server.register)


def test_register_checks_for_an_existing_super_admin():
    src = _register_source()
    assert "is_super_admin(caller)" in src, "register no longer checks the caller"
    assert "status_code=403" in src


def test_register_still_allows_the_very_first_account():
    src = _register_source()
    assert "count_documents" in src, "the zero-users bootstrap escape is gone"


def test_register_is_not_on_the_demo_allowlist():
    import demo_guard
    assert "/api/auth/register" not in demo_guard.ALLOWED_PATHS
    assert "/api/auth/register" not in demo_guard.ALLOWED_READ_POSTS
