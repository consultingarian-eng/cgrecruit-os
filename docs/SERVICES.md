# Outside services

Every service CGRecruit talks to: what it powers, whether you need it, what stops working without it, its settings, and where to sign up and check prices. Prices change, so this page links to each provider's own pricing page rather than quoting figures.

Secrets go in Railway → Variables (and, for a local run, only the ones you need in `backend/.env`). [SETUP.md](SETUP.md#every-setting) lists every variable.

## At a glance

| Service | Needed? | Powers |
| --- | --- | --- |
| [MongoDB Atlas](#mongodb-atlas) | Required | The database |
| [Railway](#railway) | Required (or another host) | Running the app on the internet |
| [Anthropic (Claude)](#anthropic-claude) | Required for the AI | Text screening, CV reading, call summaries and verdicts, email replies, AI insights |
| [Twilio](#twilio) | Required for texts and calls | SMS both ways, the phone line for calls |
| [ElevenLabs](#elevenlabs) | Required for voice | Olivia's phone calls, the inbound receptionist, no-show revival |
| [SendGrid](#sendgrid) | Strongly recommended | Outgoing email; emailed applications and replies |
| [Google Sheets](#google-sheets) | Optional | A new-hires spreadsheet |
| [Field-app hand-off](#field-app-hand-off-cg1-or-your-own) | Optional | Passing new starters to your field app |
| [Claude connector](#claude-connector) | Optional (on by default) | Asking Claude about your pipeline, read-only |
| [Intake webhook](#intake-webhook-zapier-or-similar) | Optional | Candidates from Zapier or any other tool |
| [Partner reporting feed](#partner-reporting-feed) | Optional | A stage-event export for a reporting partner |

---

## MongoDB Atlas

- **Powers:** everything the app stores: candidates, offices, settings, templates, calls, messages, notifications, logins.
- **Needed?** Required. The server won't start without it.
- **Settings:** `MONGO_URL` (secret), `DB_NAME`.
- **Sign up:** [mongodb.com/cloud/atlas](https://www.mongodb.com/cloud/atlas/register) · **Pricing:** [mongodb.com/pricing](https://www.mongodb.com/pricing)
- **Notes:** the free tier is enough to start. It has no automatic backups; move to a paid tier (or export regularly) once you hold real candidates. Choose a region near your candidates (London for the UK).

## Railway

- **Powers:** runs the server and the web app, gives you an `https://` address, redeploys when you push.
- **Needed?** Required, or another host that can build the web app and run a Python server with WebSocket support.
- **Settings:** your variables live here. Railway sets `PORT` and `RAILWAY_GIT_COMMIT_SHA` itself.
- **Sign up:** [railway.com](https://railway.com) · **Pricing:** [railway.com/pricing](https://railway.com/pricing)
- **Notes:** run **one** replica. The scheduler that calls and texts lives inside the server; two copies would contact people twice.

## Anthropic (Claude)

- **Powers:** the text-screening conversation and replies to candidates' texts; reading uploaded and emailed CVs; the summary, score and verdict after each call; replies to candidates' emails; AI insights on the Intelligence pages.
- **Needed?** Required for any of the above. Without it, CVs aren't parsed, texts aren't answered by the AI and calls get no summary; the board, calendar and manual work still function.
- **Settings:** `ANTHROPIC_API_KEY` (secret). The older name `EMERGENT_LLM_KEY` is still accepted. `ANTHROPIC_MODEL` picks the model (default `claude-sonnet-5-5`); `ANTHROPIC_FAST_MODEL` is the fallback the text and web-chat replies switch to when it's slow or rate-limited (default `claude-haiku-4-5`).
- **Sign up:** [console.anthropic.com](https://console.anthropic.com) · **Pricing:** [anthropic.com/pricing](https://www.anthropic.com/pricing#api)
- **Notes:** set a monthly spend limit in the Console. Olivia's *phone* conversations run on the LLM you pick inside ElevenLabs, not on this key.

## Twilio

- **Powers:** every text the app sends and receives, and the phone numbers Olivia's calls run on.
- **Needed?** Required for texts and calls. Without it, only email and the web pages reach candidates.
- **Settings:** `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` (secret), optionally `TWILIO_PHONE_NUMBER` (fallback sender) and `TWILIO_DEFAULT_LEAD_HOURS`. Each office's texting number is `sms_number` in `backend/company_profile.json`; its calling number is set in **Settings → Offices & Variants**.
- **Webhook to set in Twilio:** `POST /api/webhooks/twilio/inbound-sms` on each texting number (or its Messaging Service). Every Twilio webhook is signature-checked with `TWILIO_AUTH_TOKEN` and refused without it.
- **Sign up:** [twilio.com](https://www.twilio.com/try-twilio) · **Pricing:** [twilio.com/en-us/pricing](https://www.twilio.com/en-us/pricing)
- **Guide:** [TWILIO.md](TWILIO.md).

## ElevenLabs

- **Powers:** Olivia's voice calls (screening), the optional inbound receptionist and no-show revival agents, call recordings and transcripts.
- **Needed?** Required for voice. Without it, set offices to **Chat only** and screening happens by text and web chat.
- **Settings:** `ELEVENLABS_API_KEY` (secret), `ELEVENLABS_TOOL_SECRET` (secret, you choose it), `ELEVENLABS_WEBHOOK_SECRET` (secret, from ElevenLabs' post-call webhook). Agent ids, voice and phone-number ids are stored in the app's settings, mostly filled in for you.
- **Addresses ElevenLabs calls:** the booking tools under `/api/public/…` and `/api/webhooks/elevenlabs/conversation-init` (both require the `X-CGR-Tool-Secret` header, which the sync configures), and `/api/webhooks/elevenlabs/post-call` (HMAC-signed). See [ELEVENLABS.md](ELEVENLABS.md#webhook-security).
- **Sign up:** [elevenlabs.io](https://elevenlabs.io) · **Pricing:** [elevenlabs.io/pricing](https://elevenlabs.io/pricing)
- **Guide:** [ELEVENLABS.md](ELEVENLABS.md).

## SendGrid

- **Powers:**
  - *Outgoing email:* application acknowledgements, interview confirmations (with a calendar invite), reminders, reschedule links, starter emails, password resets.
  - *Inbound Parse (optional):* emailed applications become candidates; candidates' replies to the app's emails get answered.
- **Needed?** Strongly recommended. Without it no email is sent (the app logs a warning instead), and **Forgot password** can't work.
- **Settings:** `SENDGRID_API_KEY` (secret), `SENDGRID_FROM_EMAIL`, `SENDGRID_FROM_NAME`; for inbound, `INBOUND_PARSE_DOMAIN`, `EMAIL_REPLY_DOMAIN` and `SENDGRID_INBOUND_SECRET` (secret, you choose it).
- **Inbound Parse destinations:** applications → `/api/webhooks/sendgrid/inbound?key=<SENDGRID_INBOUND_SECRET>`; replies → `/api/webhooks/sendgrid/inbound-email?key=<SENDGRID_INBOUND_SECRET>`.
- **Sign up:** [sendgrid.com](https://sendgrid.com) · **Pricing:** [sendgrid.com/en-us/pricing](https://sendgrid.com/en-us/pricing)
- **Guide:** [SETUP.md step 10](SETUP.md#10-optional-services).
- **Notes:** authenticate your sending domain in SendGrid so emails don't land in spam. Inbound Parse can't sign its posts, so the secret in the destination address is what proves a post came from your SendGrid account; without `SENDGRID_INBOUND_SECRET` both inbound addresses refuse everything. Email replies are only answered (and only acted on) by the AI when they come from the address on the candidate's record; others are saved and a recruiter is notified. A reply that claims to come from a candidate's address but fails both of the sender checks mail servers use (SPF and DKIM) is held for a person, with no AI reply or action.

## Google Sheets

- **Powers:** when a candidate moves to Training, a row is added to your new-hires spreadsheet (one tab per office), and attendance is ticked there when your field app reports it.
- **Needed?** Optional. Off unless `NEW_HIRES_SHEET_ID` is set.
- **Settings:** `NEW_HIRES_SHEET_ID` (the long id in the sheet's address), and a service-account key in `GOOGLE_SA_JSON_CONTENT` (secret) or a file path in `GOOGLE_SA_JSON_PATH`. Tab names and column layout per office: `sheet_tab`, `sheet_columns`, `sheet_week_prefix` and `sheet_attended_headers` in `backend/company_profile.json`.
- **Set up:** in [Google Cloud console](https://console.cloud.google.com), create a project, enable the **Google Sheets API**, create a **service account** and a JSON key for it, then share your sheet with the service account's email as an **Editor**.
- **Pricing:** the Sheets API has no charge for normal use; see [Google's quotas](https://developers.google.com/sheets/api/limits).

## Field-app hand-off (CG1 or your own)

Two independent options:

**1. CG1-style hand-off (two-way).** Built for CG1, Cube Group's field app (a UK edition is open source: [vertex-hub-uk](https://github.com/consultingarian-eng/vertex-hub-uk)). Any app that implements the same small contract works.
- **What happens:** moving a candidate to Training sends the new starter (name, contact details, start date and time, office, first-day details) to the field app; rescheduled start dates follow; starter-critical alerts (for example a starter texting that they're lost) reach the office's admins there; and the field app reports attendance back.
- **Settings:** `CG1_BACKEND_URL` (the field app's address; blank switches all of this off), `CG1_WEBHOOK_SECRET` (secret, sent and expected as the `x-webhook-secret` header), `CGRECRUIT_WEBHOOK_SECRET` (secret, required from the field app on `/api/internal/…`). Each office's key from `backend/company_profile.json` (chosen with the office picker in **Settings → Offices & Variants**) is the office key sent to the field app, so use the same keys on both sides.
- **Calls this app makes** (to `CG1_BACKEND_URL`): `POST /api/webhooks/cgrecruit/new-starter`, `/api/webhooks/cgrecruit/starter/<id>/reschedule`, `/api/webhooks/cgrecruit/admin-alert`, `/api/webhooks/cgrecruit/funnel-stats`.
- **Calls it accepts** (with the secret): `POST /api/webhooks/cg1/attended`, `POST /api/webhooks/cg1/email-updated`, and the `/api/internal/…` routes.

**2. Simple "hired" webhook (one-way, any app).** **Settings → Integrations → Outbound Integrations:** a URL and an optional secret. When a candidate is marked hired, the app POSTs their name, email, phone, appointment and hire time and your company name, with the secret in `X-Webhook-Secret`. There's a test button that sends a dummy record.

Secrets you type into **Settings → Integrations** are write-only: once saved the box shows **Saved** instead of the value. Leave it blank to keep the stored secret, or type a new one to replace it; the test button uses the stored one.

Without either, new starters simply stay on the Training column.

## Claude connector

- **Powers:** ask Claude (claude.ai, the desktop and mobile apps, or Claude Code) about your pipeline: counts, funnels, candidates and transcripts, signed in as yourself. Each person sees only the offices they're allowed to; demo accounts are refused.
- **Needed?** Optional; on by default.
- **Settings:** `CLAUDE_CONNECTOR=off` switches it off. It uses `APP_PUBLIC_URL` as its address.
- **Connect:** in Claude, add a custom connector with `https://<your address>/mcp` and sign in with your CGRecruit email and password.
- **Notes:** read-only by design. Its tokens can't call the app's API, and secrets are stripped from what it returns. Changing your password ends existing connections.

## Intake webhook (Zapier or similar)

- **Powers:** candidates created by any tool that can POST JSON: a form builder, a job board integration, Zapier.
- **Needed?** Optional.
- **Settings:** none in Railway. **Settings → Integrations** shows the owner your private address (`/api/webhooks/zapier/<token>`) and can regenerate it.
- **Fields it reads:** `name` (or `first_name` and `last_name`), `email`, `phone`, and optionally `job_title` (matched against your job ads to pick the office), `resume_text`, `pipeline_id` (must be one of your own offices) and `source` (where the applicant came from, for example `totaljobs`; stored on the candidate, `webhook` if you leave it out).
- **Phone numbers:** send them in international format (`+44 7…`, `+1 …`); those are always kept. Anything else is read by `PHONE_DEFAULT_COUNTRY`: `US` (default) gives 10 digits, or 11 starting with 1, a `+1`, so a UK number sent without its leading 0 would become a wrong US number; `GB` turns `07…`, `447…` and 10-digit UK numbers into `+44…` ([UK.md](UK.md#phone-numbers)). A number that can't be read is stored as sent for a person to fix.
- **Notes:** the token in the address is the password: anyone with it can add candidates, and new candidates are contacted automatically. Regenerate it if it leaks: the old address stops working everywhere at once.

## Partner reporting feed

- **Powers:** `GET /api/partner/stage-events?date_from=…&date_to=…` returns one row per stage change per candidate, for a reporting partner that asked for this format.
- **Needed?** Optional; skip it unless a reporting partner asks for it.
- **Settings:** `PARTNER_FEED_API_KEY` (secret; callers send it as `X-Partner-Api-Key`). Without it the address refuses every request (503). Labels come from `partner_feed` and each office's `partner_pin` in `backend/company_profile.json`.
