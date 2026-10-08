"""Every name the codebase imports from `auto_dialer` must actually exist.

The gate-check sweep shipped fully written, fully wired at startup — and never
ran once in production. `server.py` did `from auto_dialer import
schedule_gate_check_sweep` inside a try/except, the facade didn't re-export the
name, and the ImportError was logged as a warning nobody read. A feature that
fails like that looks exactly like a feature that works, minus the messages.

So this test scans the backend for every `from auto_dialer import ...` and
asserts each imported name resolves on the facade. A new dialer module that
forgets the re-export now fails CI instead of silently never running.

Run directly (never via pytest collection):
    MONGO_URL=mongodb://127.0.0.1:1/x DB_NAME=x .venv/bin/python tests/test_auto_dialer_reexports.py
"""
import ast
import pathlib
import sys
import unittest

BACKEND = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))


def imported_names():
    """(file, name) for every `from auto_dialer import X` in backend/*.py."""
    out = []
    for path in BACKEND.rglob("*.py"):
        if "tests" in path.parts or ".venv" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "auto_dialer":
                for alias in node.names:
                    out.append((str(path.relative_to(BACKEND)), alias.name))
    return out


class TestAutoDialerReexports(unittest.TestCase):
    def test_every_imported_name_resolves(self):
        import auto_dialer

        names = imported_names()
        self.assertTrue(names, "scan found no auto_dialer imports — scanner broken?")
        missing = []
        for f, name in names:
            try:
                getattr(auto_dialer, name)
            except AttributeError:
                missing.append(f"{f}: {name}")
        self.assertEqual(
            missing, [],
            "names imported from auto_dialer that the facade does not export "
            "(the import will raise at runtime, likely inside a try/except "
            f"that hides it): {missing}",
        )


if __name__ == "__main__":
    unittest.main()
