#!/usr/bin/env python3
"""Create the owner (super-admin) account on a fresh CGRecruit database.

Usage (in your own terminal, not pasted into a chat):

    python3 .claude/skills/setup/create_owner.py https://recruit.yourcompany.co.uk
    python3 .claude/skills/setup/create_owner.py http://localhost:8000

The app only allows this while its database has no users at all, and only
with the server's SETUP_TOKEN (Railway -> Variables); the first account becomes
the owner. After that, the owner adds everyone else in Settings -> Team. The
setup token and the password are typed without being shown and never printed.
Standard library only, so it runs before anything is installed.

For scripts and CI (no terminal to type into), set these instead and the
prompts are skipped:

    CGR_SETUP_TOKEN, CGR_OWNER_EMAIL, CGR_OWNER_PASSWORD
    (optional) CGR_OWNER_NAME, CGR_COMPANY_NAME
"""
import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def fail(msg: str) -> None:
    print(f"\n{msg}")
    sys.exit(1)


def main() -> None:
    if len(sys.argv) != 2:
        fail("Usage: create_owner.py <your app's address, e.g. https://recruit.yourcompany.co.uk>")
    base = sys.argv[1].strip().rstrip("/")
    if base.endswith("/api"):
        base = base[:-4]
    parsed = urllib.parse.urlparse(base)
    local = parsed.hostname in ("localhost", "127.0.0.1")
    if parsed.scheme != "https" and not local:
        fail("Use the https:// address of your app (plain http is only for localhost).")

    # 1. Is the app there?
    try:
        with urllib.request.urlopen(f"{base}/api/", timeout=20) as r:
            body = json.loads(r.read().decode("utf-8") or "{}")
    except Exception as e:  # noqa: BLE001 - any failure means "not reachable"
        fail(f"Couldn't reach {base}/api/ ({e.__class__.__name__}). Is the app running at that address?")
    if body.get("status") != "ok":
        fail(f"{base}/api/ answered, but not like CGRecruit. Check the address.")
    print(f"Found CGRecruit at {base}.\n")

    # 2. Details (from CGR_* variables when set, otherwise typed)
    env_token = os.environ.get("CGR_SETUP_TOKEN", "").strip()
    env_email = os.environ.get("CGR_OWNER_EMAIL", "").strip().lower()
    env_pw = os.environ.get("CGR_OWNER_PASSWORD", "")
    if env_token and env_email and env_pw:
        setup_token, email, pw = env_token, env_email, env_pw
        name = os.environ.get("CGR_OWNER_NAME", "").strip() or email.split("@")[0]
        company = os.environ.get("CGR_COMPANY_NAME", "").strip()
        if "@" not in email:
            fail("CGR_OWNER_EMAIL isn't a valid email.")
        if len(pw) < 12:
            fail("CGR_OWNER_PASSWORD must be at least 12 characters.")
        print("Using CGR_SETUP_TOKEN / CGR_OWNER_EMAIL / CGR_OWNER_PASSWORD from the environment.")
    elif not sys.stdin.isatty():
        fail("No terminal to type into. Run this in your own terminal, or set CGR_SETUP_TOKEN, "
             "CGR_OWNER_EMAIL and CGR_OWNER_PASSWORD.")
    else:
        setup_token = getpass.getpass("Setup token (the SETUP_TOKEN value from your server settings; not shown): ").strip()
        if not setup_token:
            fail("The setup token is needed. Set SETUP_TOKEN on the server (docs/SETUP.md step 4), then run this again.")
        name = input("Your name: ").strip()
        company = input("Company name: ").strip()
        email = input("Your email (this is your login): ").strip().lower()
        if not (name and email and "@" in email):
            fail("A name and a valid email are needed.")
        while True:
            pw = getpass.getpass("Choose a password (at least 12 characters; not shown): ")
            if len(pw) < 12:
                print("Too short, try again.")
                continue
            if pw != getpass.getpass("Type it again: "):
                print("They didn't match, try again.")
                continue
            break

    # 3. Create it
    data = json.dumps({"email": email, "password": pw, "name": name, "company": company}).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/api/auth/register", data=data, method="POST",
        headers={"Content-Type": "application/json", "X-Setup-Token": setup_token},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            created = json.loads(r.read().decode("utf-8") or "{}").get("user") or {}
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8") or "{}").get("detail")
        except Exception:  # noqa: BLE001
            detail = None
        if e.code == 503:
            fail("The server has no SETUP_TOKEN yet. Add it in Railway -> Variables (or backend/.env), "
                 "deploy, and run this again.")
        if e.code == 403 and detail and "setup token" in str(detail).lower():
            fail("That setup token doesn't match the server's SETUP_TOKEN. Check it and try again.")
        if e.code == 403:
            fail("This database already has an owner. Sign in at /login instead "
                 "(or see docs/SETUP.md, 'Locked out of the owner account').")
        fail(f"The app refused it ({e.code}): {detail or 'no details'}")
    except Exception as e:  # noqa: BLE001
        fail(f"Request failed ({e.__class__.__name__}).")

    print(f"\nDone. Owner account created for {created.get('email', email)} "
          f"(role: {created.get('role', 'super_admin')}).")
    print(f"Sign in at {base}/login")


if __name__ == "__main__":
    main()
