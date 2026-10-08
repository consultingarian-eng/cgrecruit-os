# Security policy

## Reporting a vulnerability

Please report security problems **privately**, using GitHub's private vulnerability reporting for this repository:

1. Go to the repository's **Security** tab.
2. Click **Report a vulnerability**.
3. Describe the problem, how to reproduce it, and what an attacker could do with it.

**Never open a public issue, pull request or discussion for a vulnerability**, and don't post details anywhere public until a fix is available. This app holds job applicants' names, phone numbers, CVs and call recordings, and it can text and phone people. A public report puts every office running a copy at risk.

We'll acknowledge your report, keep you updated while we look into it, and credit you in the fix if you'd like.

## Scope

In scope: the code in this repository (the FastAPI backend, the React web app, the deploy configuration, the Claude Code skills).

Out of scope:

- Individual deployments run by other people. Each office owner runs their own copy on their own hosting; report problems with a specific site to its owner.
- The outside services the app talks to (Twilio, ElevenLabs, SendGrid, Anthropic, MongoDB Atlas, Railway, Google). Report those to the provider.

## If you run a copy of this app

- **Secrets live only in your host's variables** (Railway → Variables) and in a local `backend/.env` that git ignores. Never commit them, paste them into an issue or a chat, or show them in a screenshot.
- This repository contains **no** keys, passwords or connection strings. If you ever find one in it, report it as above.
- **If a secret leaks, rotate it at the provider straight away**: the database user's password (`MONGO_URL`), `JWT_SECRET`, `TWILIO_AUTH_TOKEN`, `ELEVENLABS_API_KEY`, `SENDGRID_API_KEY`, `ANTHROPIC_API_KEY`, the webhook secrets (`ELEVENLABS_TOOL_SECRET`, `ELEVENLABS_WEBHOOK_SECRET`, `SENDGRID_INBOUND_SECRET`, `CGRECRUIT_WEBHOOK_SECRET`, `CG1_WEBHOOK_SECRET`, `PARTNER_FEED_API_KEY`) and the Google service-account key. Then update Railway's variables and redeploy. Changing `JWT_SECRET` signs everyone out, which is what you want after a leak.
- **Every webhook is off until you give it a secret** (Twilio's answer 403; the others answer 503). Twilio's need `TWILIO_AUTH_TOKEN` (signature check); Olivia's tools and the call-start webhook need `ELEVENLABS_TOOL_SECRET`; the post-call webhook needs `ELEVENLABS_WEBHOOK_SECRET` (HMAC check); SendGrid Inbound Parse needs `SENDGRID_INBOUND_SECRET` in its address; `/api/internal/…` needs `CGRECRUIT_WEBHOOK_SECRET` and the field-app callbacks `CG1_WEBHOOK_SECRET`. The full table is in [docs/SETUP.md](docs/SETUP.md#webhook-secrets). Use long random values.
- **The first account needs `SETUP_TOKEN`.** On an empty database, only someone holding it can create the owner account, so a fresh deployment can't be claimed by whoever finds its address first.
- **`JWT_SECRET` must be at least 32 characters**; the server refuses to start otherwise. Resetting a password signs out every session issued before the reset.
- Keep `AUTH_COOKIE_SECURE` on (the default) in production. Turn it off only on your own computer.
- The candidate pages (`/apply`, `/retry`, `/applicant`, `/reschedule`) work without a login, by design: a candidate can't sign in. They are protected by long random per-candidate links, and the apply page never hands back an existing applicant's link. Don't publish candidates' personal links, and treat the intake webhook address (Settings → Integrations) as a password: regenerate it if it leaks.
- Set a monthly spend limit on your Anthropic, Twilio and ElevenLabs accounts.
- Run **one** copy of the server per database. The scheduler that places calls lives inside it; two copies would call and text people twice.

## Rules the code keeps (for contributors)

- **Webhooks prove who sent them, and fail closed.** `backend/webhook_auth.py` holds the checks: Twilio's `X-Twilio-Signature`, ElevenLabs' HMAC and tool header, SendGrid's `?key=`, and `x-webhook-secret` for server-to-server calls. A missing secret means the route refuses (503), never that it trusts. New webhooks use the same helpers.
- **Recruiters see only their own offices.** Every route that takes a candidate or office id checks access (`assert_candidate_access`, `assert_pipeline_access`, `assert_pipeline_in_tenant`, `pipeline_scope_filter` in `backend/deps.py`) unless the caller is the owner (super-admin). Values from a request never become Mongo field names or operators (see the Intelligence `date_field` allowlist).
- **Candidate-typed text is escaped** in every HTML email (`email_service.build_email_html`).
- **Demo accounts never write.** `backend/demo_guard.py` refuses every write from an `is_demo` user and blanks credentials (candidate links, tokens, secrets) out of what they read. A read-only route that hands back a credential belongs in its blocked list.
- **The Claude connector is read-only by construction.** Its tokens can't call the app's API; every tool it registers is a read, and secrets are stripped from what it returns.
- **Error responses don't carry internal details.** Log the exception on the server; return a short message.
- Pull updates from upstream regularly; security fixes arrive that way ([docs/SETUP.md](docs/SETUP.md#updating-from-upstream)).
