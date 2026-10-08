"""The SPA catch-all must not serve anything outside the frontend build.

Today the ASGI layer collapses ".." before the path reaches the handler, so
the traversal is not reachable over HTTP. This pins the handler's own
behaviour so it stays safe if that ever stops being true — the file one
directory up is backend/.env.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _serve(build_root: Path, full_path: str):
    """The containment rule from server.py's _serve_spa, in isolation."""
    try:
        file = (build_root / full_path).resolve()
        inside = file.is_relative_to(build_root.resolve())
    except (OSError, ValueError):
        inside = False
    return file if inside and file.is_file() else None


def test_traversal_out_of_the_build_dir_is_refused(tmp_path):
    build = tmp_path / "static"
    build.mkdir()
    (build / "index.html").write_text("<html></html>")
    (build / "app.js").write_text("console.log(1)")
    secret = tmp_path / ".env"
    secret.write_text("JWT_SECRET=do-not-serve-me")

    # Real assets still resolve.
    assert _serve(build, "app.js") == (build / "app.js").resolve()
    assert _serve(build, "index.html") == (build / "index.html").resolve()

    # Every way up and out lands on None (the caller then serves the shell).
    for attempt in ("../.env", "..//.env", "a/../../.env", "./../.env"):
        assert _serve(build, attempt) is None, attempt

    # A path that simply does not exist is also None, not an error.
    assert _serve(build, "nope/missing.js") is None


def test_the_real_handler_carries_the_same_rule():
    source = (Path(__file__).resolve().parents[1] / "server.py").read_text()
    assert "is_relative_to" in source, "server.py lost its traversal guard"
