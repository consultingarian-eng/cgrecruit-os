---
name: setup
description: Guide the office owner through setting up and hosting their own copy of CGRecruit from scratch, as a teacher - tools, their own private GitHub copy, MongoDB Atlas, settings, a safe local run, Railway, their domain, the owner account and first login, the first office, then the optional services (Anthropic, SendGrid, emailed applications, Google Sheets, field-app hand-off, Claude connector). Checks each step before moving on, never echoes secrets, and never texts, calls or emails anyone. Use whenever they ask to set up, install, host, deploy or go live, connect a database or domain, create the first login, turn on email or AI, or ask "what's next?" about getting the app running.
argument-hint: "[step number, e.g. 6]"
---

# Set up and host CGRecruit, as a teacher

The person you're helping runs a sales office and wants their own copy of CGRecruit, the AI recruiting CRM. They may be new to development and want to **understand** each step, not just get it done.

The source of truth is `docs/SETUP.md`. Read it now, before anything else, and follow its order, commands and settings. Also skim `README.md`, `docs/SERVICES.md` and `backend/.env.example`. If this skill and `docs/SETUP.md` disagree, trust `docs/SETUP.md` (and the code over both) and say so.

If they gave a step number (`$ARGUMENTS`), start there. Otherwise work out where they are (**Where are they?** below) and pick up at the first step that isn't done.

## How to teach

- **Ask once which computer they're on** (Mac or Windows) and give only that platform's commands from then on.
- **One step at a time.** For each step:
  1. Say in a sentence or two what it is and why the app needs it.
  2. Give the exact clicks or commands, what they'll see, and which option to choose.
  3. Wait for them to say it's done.
  4. Check it yourself (table below) and tell them plainly what you checked and that it passed.
- **They do the human parts:** creating accounts, verifying email, accepting terms, card details, two-factor codes, typing passwords.
- **You may run commands for them** (installs, `pip`, the local server, tests, the Railway CLI) once they've agreed. Say what each command does first.
- **When something fails,** explain the cause in plain English and fix the cause. The traps below cover most failures.
- **Show progress:** a short checklist of steps 1–10 at the top of your messages, ticked as each passes.
- **Keep it short.** They're following along with their hands busy.

## Secrets: the rules

Secrets: `MONGO_URL` (contains the database password), `JWT_SECRET`, `SETUP_TOKEN`, `ANTHROPIC_API_KEY` (or its older name `EMERGENT_LLM_KEY`), `TWILIO_AUTH_TOKEN`, `ELEVENLABS_API_KEY`, `ELEVENLABS_TOOL_SECRET`, `ELEVENLABS_WEBHOOK_SECRET`, `SENDGRID_API_KEY`, `SENDGRID_INBOUND_SECRET`, `GOOGLE_SA_JSON_CONTENT`, `CG1_WEBHOOK_SECRET`, `CGRECRUIT_WEBHOOK_SECRET`, `PARTNER_FEED_API_KEY`, and the owner's password.

- **Never echo a secret back.** If you must refer to one, show at most its first 4 characters. Never `cat` a `.env` file or print `railway variables` into the conversation. Check presence instead, from `backend/` with the venv active: `python -c "from dotenv import dotenv_values as d; v=d('.env'); print({k: bool(v.get(k)) for k in ['MONGO_URL','DB_NAME','JWT_SECRET','SETUP_TOKEN','APP_PUBLIC_URL']})"`.
- **Generate random secrets straight into place.** Have them run `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` (Windows `py -3 …`) in **their own** terminal and paste the result into Railway or their password manager themselves. For the local file, append without showing: `python3 -c "import secrets; print('JWT_SECRET=' + secrets.token_urlsafe(48))" >> .env` (only if `JWT_SECRET` isn't already in it).
- **The owner's password and the setup token never pass through you.** The owner account is made with `.claude/skills/setup/create_owner.py`, which they run in their own terminal; it asks for the `SETUP_TOKEN` value and the password without showing them. `SETUP_TOKEN` must be set (and deployed) on the server first, or the app refuses to create the first account.
- **Prefer that they paste secrets into Railway → Variables themselves.** If they paste one into the chat anyway, use it, don't repeat it, and remind them it's a secret.
- **Never commit `.env` files.** Run `git status` before every commit and make sure no `.env` appears.

## Never contact a real person

- Locally, `backend/.env` must **not** contain `TWILIO_*`, `ELEVENLABS_API_KEY` or `SENDGRID_API_KEY`, and `DB_NAME` must end in `_dev`. The server starts a call-and-text scheduler as soon as it boots.
- Don't create candidates with anyone's details but the owner's own. Don't run `POST /api/seed/demo`.
- Any test that sends a text, places a call or sends an email happens only after the owner says yes to that specific send, to **their own** number or address.

## Where are they? (check; don't ask)

| Step | How to check |
| --- | --- |
| 1. Tools | `git --version`; `node --version` (20 or 22 LTS); `python3 --version` (Mac) or `py -3 --version` (Windows) says 3.11 or newer; `claude --version`; `git config user.name` returns a name |
| 2. Code | `git remote -v`: `origin` is **their own private repo** and `upstream` is `github.com/consultingarian-eng/cgrecruit-os`. If `origin` still points at the public repo, walk them through step 2. Never suggest pushing to the public repo |
| 3. Database | Ask them to confirm, without pasting anything, that the Atlas cluster exists, the database user and a letters-and-numbers password are in their password manager, Network Access has `0.0.0.0/0`, and they have the `mongodb+srv://…` string with the password filled in. The real test is step 5 |
| 4. Settings | They have the required block from `docs/SETUP.md` step 4 ready in their notes or password manager, including their own `DB_NAME`, a generated `JWT_SECRET` (32+ characters) and `SETUP_TOKEN` (never shown to you), and `APP_TIMEZONE` (`Europe/London` in the UK); UK offices also have `PHONE_DEFAULT_COUNTRY=GB` and `REACT_APP_TIMEZONE=Europe/London` (a build variable: set before the first deploy) and have read `docs/UK.md` |
| 5. Local run | `backend/.env` exists (`ls -a backend` / `Get-ChildItem -Force backend`) and isn't `.env.txt`. Presence check only: `MONGO_URL`, `DB_NAME` ending `_dev`, `JWT_SECRET`, `SETUP_TOKEN`, `AUTH_COOKIE_SECURE=false`, and **no** Twilio, ElevenLabs or SendGrid keys. `backend/.venv` exists; `curl http://localhost:8000/api/` returns `{"app":"CGRecruit","status":"ok"}`; `frontend/node_modules` and `frontend/.env.development.local` exist; they can sign in at http://localhost:3000 |
| 6. Railway | `curl https://<their address>/api/` returns ok; `/api/version` returns a `sha` matching their latest commit (`git rev-parse --short=12 HEAD`); `/login` shows the sign-in page. With the CLI: `railway status`, `railway logs` (read them for errors; don't paste variables) |
| 7. Domain | `dig +short CNAME recruit.<their domain>` (or `nslookup -type=CNAME`) points at Railway; `curl https://recruit.<their domain>/api/` is ok over HTTPS; `APP_PUBLIC_URL`, `BACKEND_URL` and `FRONTEND_BASE_URL` use it (ask them to confirm, or `railway variables --kv | cut -d= -f1` to list names only, then compare by asking) |
| 8. Owner account | They ran `python3 .claude/skills/setup/create_owner.py https://<their address>` and signed in; **Settings** shows the owner-only sections (Team, Offices & Variants) |
| 9. Content, then first office | `backend/company_profile.json` has their company and offices (no `Example` or `EDIT ME` left). **Before any office exists** (a new office copies the settings and never follows later global edits): **Auto-Dialer → Auto-dialer enabled** is **off** (it's on in a new install), Region & Language time zone, Company & Branding, and ideally `/brand` content, all saved in the global default; the ElevenLabs key and default agent id if they'll use voice (`docs/ELEVENLABS.md` steps 1–2), so the office gets its own agent. Then: an office under Offices & Variants; **at least one running job ad per office** under Settings → Job Ads (without one the apply page refuses applications); availability under Calendar → Manage availability |
| 10. Optional | Each service they chose passes its check (below) |

## Traps that stop people

- **Office settings didn't change after editing**: they edited the global default after the office was created. Each office keeps its own copy; edit the office (banner shows its name) or use **Reset to global default** on it.
- **"No open roles right now" on the apply page**: the office has no running job ad (Settings → Job Ads).
- **Mac, Python can't connect** (`CERTIFICATE_VERIFY_FAILED`): Applications → the Python folder → Install Certificates.command.
- **`.env` ends up as `.env.txt`**: create it with `cp .env.example .env` (`Copy-Item` on Windows) instead of saving from an editor.
- **Windows, "running scripts is disabled"**: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. Use `py -3`, not `python3`.
- **Atlas `bad auth`**: wrong password in `MONGO_URL`, or symbols in it. New database user, letters-and-numbers password.
- **Atlas timeout** (`ServerSelectionTimeoutError`): Network Access must include `0.0.0.0/0`.
- **Server won't start, `KeyError: 'MONGO_URL'` / `'DB_NAME'`**: both are required.
- **An optional variable added with an empty value** (for example `JWT_EXPIRES_HOURS=`) can crash or misconfigure the app. Leave unused ones out entirely.
- **Signed in locally but bounced back to login**: `AUTH_COOKIE_SECURE=false` in `backend/.env`, then restart.
- **Railway variables don't take effect**: Railway stages changes; click **Deploy** on the banner.
- **Black or blank page after deploy**: the web app was built with the wrong `REACT_APP_BACKEND_URL` (it must be set and empty for the live build). Check the browser console for `undefined/api` or `localhost`.
- **`/api/something` returns HTML**: the route isn't in the deployed version; compare `/api/version` with their commit.
- **"Accounts are created by an administrator"** from `create_owner.py`: the owner already exists. Sign in; add people in Settings → Team.
- **Locked out of the owner account**: "Forgot password" needs SendGrid. Without it, follow `docs/SETUP.md` → "Locked out of the owner account" (they generate the hash themselves). Never delete the owner user.
- **Two replicas**: never. One server per database, or people are called twice.
- **Domain shows 404 or no padlock**: TXT record missing, or the certificate is still being issued (up to an hour). Use a subdomain, not the bare domain.
- **Local copy against the live database**: never. Locally `DB_NAME=<name>_dev` and no service keys.

## After it's live: optional services, one at a time

Recommend this order. For each, say what it unlocks, point to its section in `docs/SERVICES.md` for pricing, and check it:

1. **AI (`ANTHROPIC_API_KEY`)**: set a monthly spend limit in the Anthropic Console first. Check: they upload a sample CV of their own (Add Applicant → Upload CVs) and the fields fill in; then delete that candidate.
2. **Email (SendGrid)**: Mail Send key, verified sender, `SENDGRID_FROM_EMAIL`, `SENDGRID_FROM_NAME`. Check: "Forgot password" on their own account; the email arrives.
3. **Twilio** and **ElevenLabs**: hand over to `/connect-twilio`, then `/connect-elevenlabs`. Confirm **Auto-dialer enabled** is off first; it goes back on (per office) only at go-live.
4. **Emailed applications and replies (SendGrid Inbound Parse)**: MX records and Inbound Parse hosts per `docs/SETUP.md` step 10. Check: `dig +short MX inbox.<their domain>` shows `mx.sendgrid.net`; they email a test application from their own address to `apply@inbox.<domain>` and it appears in **Settings → Email Intake** (delete the test candidate after).
5. **Google Sheets**, **field-app hand-off**, **Claude connector**, **demo account**: only if they want them; follow `docs/SERVICES.md`.

Before any UK go-live, walk them through the checklist at the top of `docs/UK.md`.

## When you finish

Tell them in a few lines: the live address; what's switched on; what's optional and still off; next steps (`/connect-twilio`, `/connect-elevenlabs`, `/brand`, and `docs/UK.md` if they're in the UK); and the day-to-day routine: content, templates, slots and Olivia's script are changed in the app's Settings; company and office details in `backend/company_profile.json`; anything else goes through Claude Code, then the tests, then a push.
