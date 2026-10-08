# Setting up CGRecruit: the full manual

This manual takes you from nothing to your own copy of CGRecruit running on the internet, on your own database and domain, with you signed in as the owner. In Claude Code, `/setup` walks you through the same steps and checks each one; this page is what it follows.

Phone numbers, Olivia's voice and your branding come after, in their own guides: [TWILIO.md](TWILIO.md), [ELEVENLABS.md](ELEVENLABS.md), [BRANDING.md](BRANDING.md). Running in the UK? Read [UK.md](UK.md) before you go live.

**The steps**

1. [Tools](#1-tools)
2. [Your own private copy](#2-your-own-private-copy)
3. [The database (MongoDB Atlas)](#3-the-database-mongodb-atlas)
4. [Your settings](#4-your-settings)
5. [A safe local run](#5-a-safe-local-run)
6. [Hosting (Railway)](#6-hosting-railway)
7. [Your domain](#7-your-domain)
8. [The owner account and first login](#8-the-owner-account-and-first-login)
9. [Your content, then your first office](#9-your-content-then-your-first-office)
10. [Optional services](#10-optional-services)

Then: [every setting](#every-setting), [updating from upstream](#updating-from-upstream), [running the tests](#running-the-tests), [troubleshooting](#troubleshooting).

---

## Words you will see

- **Webhook**: an address in your app that another service (Twilio, ElevenLabs, SendGrid) calls to tell it something happened, such as a text arriving.
- **DNS records (MX, CNAME, TXT)**: entries at your domain registrar that point email or a web address at a service, or prove you own the domain.
- **HMAC / signature**: a code the calling service adds to each webhook, made with a shared secret, so your app can tell the call is genuine.
- **E.164**: the international phone format, `+44 7700 900123`.
- **Replica**: one running copy of the app on Railway. This app must run exactly one.

## Three rules before you start

- **Secrets never go in the code, a chat or a screenshot.** They live in Railway's variables and in a local `backend/.env` file that git ignores.
- **Your computer never uses the live database.** Locally you use a separate, empty database (its name ends in `_dev`). The server starts a scheduler that calls and texts candidates, so a local copy pointed at live data would contact real people.
- **Nothing texts or calls a real person while you test.** Leave the Twilio, ElevenLabs and SendGrid keys out of your local settings. When you test on the live app, use your own phone number.

---

## 1. Tools

You need four things on your computer, and a fifth is optional.

| Tool | Why | Check it with |
| --- | --- | --- |
| Git | Copies the code and keeps your changes | `git --version` |
| Python 3.11 or newer (3.13 works) | Runs the server | Mac: `python3 --version` · Windows: `py -3 --version` |
| Node.js LTS (20 or 22) | Builds the web app | `node --version` |
| Claude Code | Sets up and changes the app with you | `claude --version` |
| Railway CLI (optional) | Lets the `/connect-twilio` and `/connect-elevenlabs` checks and `/setup` read your live settings and logs without showing them | `railway --version` |

- **Mac:** install the [Xcode command-line tools](https://developer.apple.com/xcode/resources/) (`xcode-select --install`) for Git, Python from [python.org](https://www.python.org/downloads/), Node.js from [nodejs.org](https://nodejs.org/) (the LTS button). After installing Python, open Applications → Python 3.x → **Install Certificates.command** once.
- **Windows:** [Git for Windows](https://git-scm.com/download/win), Python from [python.org](https://www.python.org/downloads/) (tick **Add python.exe to PATH**), Node.js LTS from [nodejs.org](https://nodejs.org/). Use PowerShell.
- **Claude Code:** follow [code.claude.com/docs/en/setup](https://code.claude.com/docs/en/setup).
- **Railway CLI (optional):** `npm install -g @railway/cli` (Mac can also use `brew install railway`), then `railway login`, and later, once the Railway project exists (step 6), `railway link` from the repository folder to connect this folder to it. The checks run as `railway run python3 …`, which hands Railway's variables to that one command without printing them. Without the CLI, run the same command with the variables set in your terminal for that session only (Mac `export NAME=value`, Windows PowerShell `$env:NAME="value"`), and close the terminal afterwards; don't put live keys in `backend/.env`.
- **npm** comes with Node.js and installs the web app's packages. Use `npm ci` (not `yarn`): it installs exactly the versions in `frontend/package-lock.json`, which is what the live build uses.

Tell Git who you are, once: `git config --global user.name "Your Name"` and `git config --global user.email "you@yourcompany.co.uk"`.

## 2. Your own private copy

Your copy will hold your settings and, once you change them, your prompts, templates and branding. Keep it **private**.

1. On GitHub, create a new **private** repository with nothing in it (no README, no licence).
2. Clone this project and point it at your repository:

   ```
   git clone https://github.com/consultingarian-eng/cgrecruit-os.git my-recruiting
   cd my-recruiting
   git remote rename origin upstream
   git remote add origin https://github.com/<you>/<your-private-repo>.git
   git push -u origin main
   ```

`origin` is now your private copy (where you push), and `upstream` is this project (where updates come from). Never push to `upstream`.

## 3. The database (MongoDB Atlas)

MongoDB Atlas is the online database where the app keeps candidates, offices, settings, call records and messages.

1. Sign up at [mongodb.com/cloud/atlas](https://www.mongodb.com/cloud/atlas/register) and create a project.
2. **Create a cluster.** The free tier is enough to start. Pick a region close to you and your candidates (in the UK, London).
3. **Database Access → Add New Database User.** Use a password made of **letters and numbers only** (symbols need escaping in the connection string and cause most "bad auth" errors). Save it in your password manager.
4. **Network Access → Add IP Address → Allow access from anywhere** (`0.0.0.0/0`). Railway doesn't give the app a fixed address, so Atlas has to accept connections from anywhere; the password is what protects it.
5. **Connect → Drivers** and copy the `mongodb+srv://…` connection string. Put your password in place of `<db_password>`. This whole string is `MONGO_URL`, and it is a secret.
6. Choose two database names: one for the live app (for example `recruit_prod`) and one for your computer (`recruit_dev`). You don't create them; the app does on first start, along with its indexes.

## 4. Your settings

The app reads its settings from environment variables. `backend/.env.example` lists every one, grouped and explained; [Every setting](#every-setting) below has the same list as a table.

To go live you need at least this **required block** (keep it in your password manager or notes for now, not in a file in the repo):

```
MONGO_URL=<your mongodb+srv string>
DB_NAME=recruit_prod
JWT_SECRET=<a long random string, 32+ characters>
SETUP_TOKEN=<another long random string, used once in step 8>
APP_TIMEZONE=Europe/London        # or America/New_York, the default
APP_PUBLIC_URL=https://<your app's address>
BACKEND_URL=https://<your app's address>/api
FRONTEND_BASE_URL=https://<your app's address>
```

**UK offices add these two** (and read [UK.md](UK.md) before step 6):

```
PHONE_DEFAULT_COUNTRY=GB             # numbers typed as 07… are stored as +44…
REACT_APP_TIMEZONE=Europe/London     # the web app's default time zone
```

`REACT_APP_TIMEZONE` is baked into the web app when Railway builds it, so set it before the first deploy (changing it later needs a redeploy). `BACKEND_URL` must always be `APP_PUBLIC_URL` with `/api` on the end.

- **`JWT_SECRET`** signs everyone's login. Generate it without it ever appearing on screen in a chat, in your own terminal: Mac `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`, Windows `py -3 -c "import secrets; print(secrets.token_urlsafe(48))"`. Paste it straight into Railway or your password manager. It must be at least 32 characters; the server refuses to start with a shorter one.
- **`SETUP_TOKEN`** lets you, and only you, create the first (owner) account in step 8. Generate it the same way. Without it the first account can't be created at all, so nobody who finds your new address before you can claim it.
- **`APP_TIMEZONE`** is your home time zone: schedules, call windows, quiet hours, reminders and the times in texts and emails use it. UK offices use `Europe/London`. Each office's **Settings → Region & Language** zone still wins where it's set.
- **The field-app secrets** (`CGRECRUIT_WEBHOOK_SECRET`, `CG1_WEBHOOK_SECRET`) are only needed if you connect a field app. Without them, those server-to-server addresses stay switched off.
- **The app's address** isn't known until step 6 (Railway) or step 7 (your domain). Fill it in then.

The services come later, one at a time: the Anthropic key (`ANTHROPIC_API_KEY`), Twilio, ElevenLabs, SendGrid.

## 5. A safe local run

Running it on your computer first proves the code and the database work, without touching anyone.

**The server**

```
cd backend
cp .env.example .env            # Windows: Copy-Item .env.example .env
```

Open `backend/.env` in your editor and fill in only:

- `MONGO_URL`: your Atlas string.
- `DB_NAME=recruit_dev`: **never** the live name.
- `JWT_SECRET`: any long random string, 32+ characters (generate one as above).
- `SETUP_TOKEN`: another random string, for creating your local owner account.
- `AUTH_COOKIE_SECURE=false`: so the login cookie works over plain `http://localhost`.
- `APP_PUBLIC_URL=http://localhost:8000`, `BACKEND_URL=http://localhost:8000/api`, `FRONTEND_BASE_URL=http://localhost:3000`.
- Optionally `ANTHROPIC_API_KEY` (your Anthropic key), if you want to try the text-screening chat locally.

Leave every Twilio, ElevenLabs and SendGrid line **blank**. With them blank, no text, call or email can go out: the app logs a warning instead. Every webhook also refuses requests while its secret is blank (see [Webhook secrets](#webhook-secrets)), so nothing outside can drive the app either.

Then install and start it:

```
python3 -m venv .venv           # Windows: py -3 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn server:app --reload --port 8000
```

Check it: open `http://localhost:8000/api/` and you should see `{"app":"CGRecruit","status":"ok"}`.

**The web app** (a second terminal)

```
cd frontend
cp .env.example .env.development.local   # Windows: Copy-Item .env.example .env.development.local
npm ci
npm start
```

It opens `http://localhost:3000` and talks to your local server (`REACT_APP_BACKEND_URL=http://localhost:8000` in that file, which only `npm start` reads). Make your local owner account with step 8's script, pointed at `http://localhost:8000`, and sign in.

What works locally: the board, settings, calendar, the candidate pages and the **web-chat screening** (`/retry/<candidate link>`), which covers the whole screen-and-book journey in a browser. What doesn't: real texts and calls, because Twilio and ElevenLabs can't reach your computer, and that's deliberate.

## 6. Hosting (Railway)

Railway runs the app on the internet and redeploys it each time you push to your private repository.

1. Sign up at [railway.com](https://railway.com) and connect your GitHub account.
2. **New Project → Deploy from GitHub repo →** your private repository.
3. **Variables:** paste your required block (the **Raw Editor** takes it all at once). Leave out `AUTH_COOKIE_SECURE` (it defaults to on) and any local-only values.
4. Railway reads how to build and start the app from the repository's own configuration: `railway.toml` tells it to build the `Dockerfile`, which builds the web app (`frontend/`) and copies it to where the server serves it from (`backend/static`), then starts one server process. You don't type build commands. Keep the service at **one replica**: the call scheduler runs inside the server, and two copies would dial everyone twice.
5. **Settings → Networking → Generate Domain** gives you an address like `something.up.railway.app`. Put it in `APP_PUBLIC_URL`, `BACKEND_URL` (with `/api` on the end) and `FRONTEND_BASE_URL`.
6. Railway **stages** variable changes. Click **Deploy** on the banner to apply them.
7. Keep **one replica** (the default). The call scheduler runs inside the server, and two copies would contact people twice. In the UK, choose the EU West region (Amsterdam) in the service's **Settings → Region** picker ([Railway's regions page](https://docs.railway.com/deployments/regions); it doesn't name a plan requirement, so if the picker isn't offered on your plan, check Railway's pricing page).

Check it:

- `https://<your address>/api/` returns `{"app":"CGRecruit","status":"ok"}`.
- `https://<your address>/api/version` returns the code version you just pushed.
- `https://<your address>/login` shows the sign-in page.

If the page is black or blank, see [Troubleshooting](#troubleshooting).

## 7. Your domain

A domain such as `recruit.yourcompany.co.uk` looks better in candidates' texts and emails, and it doesn't change if you ever move host.

1. Railway → your service → **Settings → Networking → Custom Domain**, enter `recruit.yourcompany.co.uk`.
2. Railway shows a **CNAME** record (and a **TXT** record to prove you own the domain). Add both at your domain provider's DNS settings.
3. Wait for the padlock. It can take up to an hour.
4. Change `APP_PUBLIC_URL`, `BACKEND_URL` and `FRONTEND_BASE_URL` to the new address and deploy.

Use a subdomain (`recruit.`), not the bare domain: most DNS providers can't point a bare domain at Railway.

**If you change the address later**, Twilio's text webhook and Olivia's agents still point at the old one. Re-run `/connect-twilio` and `/connect-elevenlabs` (or follow those guides) to move them.

## 8. The owner account and first login

There is no public sign-up. The **very first account** made on an empty database becomes the **owner** (super-admin). After that, only the owner can create accounts.

It needs the `SETUP_TOKEN` you set in step 4: the app refuses to create the first account without it. Create it from your own terminal, so neither the token nor your password appears in a chat:

```
python3 .claude/skills/setup/create_owner.py https://recruit.yourcompany.co.uk
```

(Windows: `py -3 …`.) It asks for the setup token, your name, company, email and a password (at least 12 characters; neither is shown as you type), checks the app is reachable, and creates the account. If the database already has a user, the app refuses with "Accounts are created by an administrator", which means the owner already exists: sign in instead.

Then sign in at `https://<your address>/login`.

- **Add your team** in **Settings → Team**: recruiters (work the board for the offices you give them) and viewers (see only candidates they added; they can add candidates by upload or direct booking, but can't change settings or move anyone). **Settings → Analyst Access** gives someone figures only: the Intelligence dashboards (funnel, cohorts, weekly activity) and AI insights, with no candidate names, contact details, transcripts or CVs; every other page refuses them. (AI insights is a written report, so it can occasionally quote a line from a call.) Only you, the owner, change the global settings and templates; recruiters change their own offices' settings; viewers and analysts change nothing.
- **Forgot password** emails a reset link, so it needs SendGrid (step 10). Without email, see [Locked out](#locked-out-of-the-owner-account).

## 9. Your content, then your first office

An **office** is called a **pipeline** in the code and in some screens. Each one has its own board, apply link, phone number, interview slots, job ads, prompts and templates.

**The order matters.** When you create an office, it gets its **own copy** of the settings as they are at that moment: company details, Olivia's script, templates, Region & Language, Auto-Dialer and the rest. Later changes to the global default **don't reach an office that already exists**. So set the global default first, then create offices.

1. **Company file.** Put your company and offices (addresses, map links, texting numbers, first-day details) in `backend/company_profile.json`, commit and push ([BRANDING.md](BRANDING.md#1-company-and-offices-backendcompany_profilejson)). `/brand` helps with this. The sample file ships with `EDIT ME` placeholder text in `pay_summary` and `about_company`; both go into the starting text of Olivia's instructions and the receptionist's prompt, so replace them before you go live (searching the file for `EDIT ME` finds them).
2. **Global default, before any office exists.** With no office yet, everything you save in **Settings** is the global default. Set:
   - **Settings → Auto-Dialer:** switch **Auto-dialer enabled** **off**. It is **on** in a new install, and once Twilio and ElevenLabs are connected it would call real applicants within minutes of applying. You switch it on when you go live (step 6 below).
   - **Settings → Region & Language:** the **time zone** (in the UK, `Europe/London`) and date format.
   - **Settings → Company & Branding:** company name, city, default job role, recruiter email, website and logo.
   - Olivia's script and questions, the email and text templates and the Post-Interview Form: run `/brand` now ([BRANDING.md](BRANDING.md#2-in-the-app-settings)).
3. **Olivia's default agent (only if you'll use voice).** Do steps 1 and 2 of [ELEVENLABS.md](ELEVENLABS.md): the API key and the default agent id. A new office then gets its own copy of the agent ("Olivia — <office>") automatically. An office created before this has no agent of its own; see [ELEVENLABS.md](ELEVENLABS.md#2-the-screening-agent) for that case.
4. **Create the office.** **Settings → Offices & Variants → New pipeline.** Give it a name (for example `Manchester`) and check the public slug: it becomes the apply link `https://<your address>/apply/<slug>`. Link it to its office from `company_profile.json` with the office picker on the same screen (otherwise the app matches the office by name).
5. **For each office:**
   - **Settings → Job Ads → + New Job Ad:** the **Job Title**, the description, and **Ad is running** switched on. **The apply link needs at least one running job ad**: without one the apply page says there are no open roles and won't accept applications. The job title is what Olivia says for `{{role}}` on calls and what `[Role]` becomes in emails and texts.
   - **Calendar → Manage availability:** when interviews can happen, slot length, the default meeting link (Zoom, Teams or Meet) and the slots Olivia offers. **Settings → Booking & Slots** sets how many slots she offers and the reschedule rules.
6. **Then, in this order:** `/connect-twilio` (phone numbers and texting, [TWILIO.md](TWILIO.md)), `/connect-elevenlabs` (link each office's number to its agent and make a test call, [ELEVENLABS.md](ELEVENLABS.md)). When your test journey on yourself works, **go live**: switch **Auto-dialer enabled** back on in **Settings → Auto-Dialer** for each office (and in the global default).

**Global default or one office?** Once an office exists, the Settings page opens on the office chosen in the app's office switcher, and a banner at the top of each section says which office you're editing. Its **Edit Global default** button switches to the global default (the banner turns amber: "Editing the global default for …"); **Back to <office> only** switches back. Saving the global default changes only offices that don't have their own copy, and every office has its own copy from the moment it was created. To make an existing office follow a new global default, either edit that office too, or use **Reset to global default** in its banner, which discards **all** of that office's own settings. **Settings → Screen Call Agent → How candidates are first contacted** decides whether Olivia calls first, texts first, or only texts.

The auto-dialer only stops calls. With it off, new applicants (from the apply page, emailed applications or the intake webhook) still get the arrival email, and a text too when Olivia texts first. Don't share an apply link until you're ready for that.

### Put an apply button on your careers page

Each office has its own apply link (`https://<your app's address>/apply/<office slug>`). To open the form in a pop-up on your own website instead, paste a snippet like this just before `</body>`:

```html
<script src="https://<your app's address>/api/widget.js" data-pipeline-slug="<office slug>"></script>
```

The easiest way is **Settings → Embed Widget**: pick the office and copy the ready-made snippet (it also offers a version for your own button).

## 10. Optional services

Add them one at a time. [SERVICES.md](SERVICES.md) has what each powers and its pricing link.

**The AI (Anthropic).** Set `ANTHROPIC_API_KEY` to your Anthropic API key. It powers text screening, CV reading, call summaries and email replies. Set a monthly spend limit in the Anthropic Console first. Check: upload a CV with **Add Applicant → Upload CVs** and see the fields fill in.

**Email (SendGrid).**
1. Create an API key with **Mail Send** permission → `SENDGRID_API_KEY`.
2. Verify your sender (Settings → Sender Authentication: authenticate your domain, or verify a single sender) → `SENDGRID_FROM_EMAIL`, and the name candidates see → `SENDGRID_FROM_NAME`.
3. Check: **Forgot password** on your own account; the email arrives.

**Emailed applications (SendGrid Inbound Parse).** Lets you forward or BCC application emails to an address that creates candidates. Emails with a CV attached become candidates automatically. Job-board "new application" notifications that only link to the CV (Indeed's, for example) are recognised and listed under **Needs attention** for a recruiter to add by hand: this project doesn't download CVs from job boards.
1. Pick a subdomain such as `inbox.yourcompany.co.uk` → `INBOUND_PARSE_DOMAIN`.
2. At your DNS provider, add an **MX** record for that subdomain pointing to `mx.sendgrid.net` (priority 10).
3. Choose a long random `SENDGRID_INBOUND_SECRET` (Inbound Parse can't sign its requests, so this secret in the address is what proves they came from SendGrid). SendGrid → Settings → **Inbound Parse → Add Host & URL**: the subdomain, and the destination `https://<your address>/api/webhooks/sendgrid/inbound?key=<SENDGRID_INBOUND_SECRET>`. Leave "POST the raw, full MIME message" **off**. Without the secret (or with the wrong one) the app refuses every inbound email.
4. Mail to `apply@<subdomain>` is matched to an office automatically; `<office-slug>@<subdomain>` goes straight to that office. **Settings → Email Intake** shows the addresses and a log of what arrived.

**Email replies.** Candidates can reply to the app's emails and get an answer. Pick another subdomain such as `replies.yourcompany.co.uk` → `EMAIL_REPLY_DOMAIN`, add its MX record to `mx.sendgrid.net`, and add an Inbound Parse host for it with the destination `https://<your address>/api/webhooks/sendgrid/inbound-email?key=<SENDGRID_INBOUND_SECRET>`. The AI only answers, and only acts (cancel, reschedule, call-back), when the reply comes from the email address on the candidate's record; anything else is saved on the candidate and a recruiter is notified.

**Intake webhook (Zapier or similar).** **Settings → Integrations** shows a private webhook address. Anything that can POST JSON (`name` or `first_name`/`last_name`, `email`, `phone`, optionally `job_title`, `resume_text`, `pipeline_id`, `source`) can create candidates through it. Send phone numbers in international format (`+44 7…`). Anything else is read using `PHONE_DEFAULT_COUNTRY` (see the settings table): with the default `US`, 10 digits, or 11 starting with 1, get `+1`, so a UK number sent without its leading 0 (`7700900123`) would become a wrong US number; with `GB`, `07…`, `447…` and 10-digit UK numbers get `+44`. Treat the address as a password; regenerate it there if it leaks. Only the owner can see or regenerate it, and a `pipeline_id` must be one of your own offices.

**Google Sheets (new starters).** A Google Cloud service account with the Sheets API enabled, its JSON key in `GOOGLE_SA_JSON_CONTENT` (the whole file's contents) or a file path in `GOOGLE_SA_JSON_PATH`, your sheet's id in `NEW_HIRES_SHEET_ID`, and the sheet shared with the service account's email as an editor. Each office's tab name and columns are set in `backend/company_profile.json` ([SERVICES.md](SERVICES.md#google-sheets)).

**Field-app hand-off.** See [SERVICES.md](SERVICES.md#field-app-hand-off-cg1-or-your-own).

**Claude connector.** On by default. In Claude, add a custom connector with the address `https://<your address>/mcp` and sign in with your CGRecruit email and password. It can only read, and each person sees only the offices they're allowed to. Set `CLAUDE_CONNECTOR=off` to switch it off.

**A demo account.** Create an ordinary account in **Settings → Team**, then in Atlas (Browse Collections → `users`) add the field `is_demo: true` to that user. It can see everything its role allows and can't change anything; credentials and candidates' private links are blanked out of what it reads.

---

## Every setting

All of these are environment variables: Railway → Variables in production, `backend/.env` locally. `backend/.env.example` has the same list with comments. Leave out any optional variable you aren't using rather than adding it with an empty value: an empty value can override a default.

| Variable | Needed? | What it does |
| --- | --- | --- |
| `MONGO_URL` | Required · secret | Your Atlas connection string |
| `DB_NAME` | Required | Database name. A `_dev` name locally, the live name on Railway |
| `JWT_SECRET` | Required · secret | Signs logins and the call-bridge stream links. At least 32 characters or the server won't start. Changing it signs everyone out |
| `SETUP_TOKEN` | Required to create the owner · secret | Sent by `create_owner.py`. Without it the first account can't be created. Does nothing once an owner exists |
| `APP_TIMEZONE` | Recommended | Home time zone (IANA name). Default `America/New_York`; UK `Europe/London`. An office's Region & Language zone wins where set |
| `PHONE_DEFAULT_COUNTRY` | Recommended outside the US | How a number typed without `+` is read: `US` (default; 10 digits get `+1`) or `GB` (`07700 900123` becomes `+447700900123`). Numbers starting with `+` are always kept. See [UK.md](UK.md#phone-numbers) |
| `ELEVENLABS_AGENT_AUTH` | Optional | Default on: agent syncs switch on ElevenLabs agent authentication, so nobody can start a browser conversation with your agents. `false` only to debug ([ELEVENLABS.md](ELEVENLABS.md#webhook-security)) |
| `APP_PUBLIC_URL` | Required | The app's public address, no trailing slash. Used in candidates' links, Twilio and ElevenLabs callbacks, emails and the Claude connector |
| `BACKEND_URL` | Recommended | The same address plus `/api`. Used for webhook and intake addresses |
| `FRONTEND_BASE_URL` | Recommended | The same address as `APP_PUBLIC_URL` (locally, `http://localhost:3000`) |
| `CORS_ORIGINS` | Optional | Extra web addresses allowed to call the API, comma-separated. `APP_PUBLIC_URL` is always allowed; `http://localhost:3000` only while `APP_PUBLIC_URL` isn't an `https://` address |
| `CORS_ALLOW_LOCALHOST` | Optional | `true` also allows `http://localhost:3000` on an https deployment |
| `JWT_ALGORITHM` | Optional | `HS256` (default), `HS384` or `HS512` |
| `JWT_EXPIRES_HOURS` | Optional | How long a login lasts. Default `720` (30 days) |
| `AUTH_COOKIE_SECURE` | Optional | Default `true`. Set `false` only locally |
| `ANTHROPIC_API_KEY` | Required for the AI · secret | Your Anthropic API key. The older name `EMERGENT_LLM_KEY` is still accepted |
| `ANTHROPIC_MODEL` | Optional | The Claude model for every AI call. Default `claude-sonnet-5-5` |
| `ANTHROPIC_FAST_MODEL` | Optional | The faster fallback for the SMS and web-chat replies when the main model is slow or rate-limited. Default `claude-haiku-4-5` |
| `TWILIO_ACCOUNT_SID` | Required for texts and calls | Twilio account SID |
| `TWILIO_AUTH_TOKEN` | Required for texts and calls · secret | Twilio auth token. Also verifies Twilio's webhooks: without it every Twilio webhook is refused |
| `ALLOW_UNSIGNED_TWILIO_WEBHOOKS` | Local only | `true` lets unsigned Twilio webhooks through when you have no Twilio account locally. Never on a live app |
| `TWILIO_PHONE_NUMBER` | Optional | A fallback number to send from when no office number applies |
| `TWILIO_DEFAULT_LEAD_HOURS` | Optional | Hours before a training start that the confirmation text goes. Default `3` |
| `NO_COHORT_MONDAYS` | Optional | Comma-separated Mondays (`2026-12-28,2027-01-04`) with no training cohort. Offers to move a start date skip them to the following Monday |
| `ELEVENLABS_API_KEY` | Required for voice · secret | ElevenLabs API key |
| `ELEVENLABS_TOOL_SECRET` | Required for voice · secret | You choose it. ElevenLabs sends it on Olivia's tools and the call-start webhook ([ELEVENLABS.md](ELEVENLABS.md#webhook-security)). Blank = those are refused |
| `ELEVENLABS_WEBHOOK_SECRET` | Recommended for voice · secret | The post-call webhook's signing secret, from ElevenLabs. Blank = the webhook is refused and the 60-second sweep records outbound calls instead |
| `SENDGRID_API_KEY` | Recommended · secret | SendGrid API key |
| `SENDGRID_FROM_EMAIL` | With SendGrid | The verified address emails come from |
| `SENDGRID_FROM_NAME` | With SendGrid | The sender name, when the office's company name isn't set |
| `INBOUND_PARSE_DOMAIN` | With emailed applications | The subdomain that receives applications |
| `EMAIL_REPLY_DOMAIN` | With email replies | The subdomain candidates' replies go to. Blank switches reply handling off |
| `SENDGRID_INBOUND_SECRET` | With emailed applications or replies · secret | You choose it; it goes in the Inbound Parse destination as `?key=…`. Blank = inbound email is refused |
| `COMPANY_PROFILE_PATH` | Optional | Where your company and office details file is. Default `backend/company_profile.json` ([BRANDING.md](BRANDING.md)) |
| `NEW_HIRES_SHEET_ID` | Optional | The Google Sheet new starters are added to. Blank keeps the export off |
| `GOOGLE_SA_JSON_CONTENT` | Optional · secret | Google service-account key (the JSON itself) |
| `GOOGLE_SA_JSON_PATH` | Optional | Or a path to that JSON file |
| `CGRECRUIT_WEBHOOK_SECRET` | With a field app · secret | Required from your field app on `/api/internal/…`. Blank keeps those addresses off |
| `CG1_WEBHOOK_SECRET` | With a field app · secret | Sent to, and expected from, the field app. Blank keeps its callbacks off |
| `CG1_BACKEND_URL` | Optional | Your field app's address. Blank switches the hand-off off |
| `CLAUDE_CONNECTOR` | Optional | `off` switches the Claude connector off. Default on |
| `PARTNER_FEED_API_KEY` | Optional · secret | Key for the optional partner reporting feed (`/api/partner/stage-events`). Blank keeps it off (503) |
| `RAILWAY_GIT_COMMIT_SHA` | Set by Railway | Shown by `/api/version`. Don't set it yourself |

The web app's build settings live in `frontend/.env.example`. `REACT_APP_BACKEND_URL` is **empty** for the live build (the `Dockerfile` sets it so), so the app calls its own address, and `http://localhost:8000` for `npm start` on your computer. `REACT_APP_TIMEZONE` (optional) should match `APP_TIMEZONE`; on Railway add it as a service variable and the Docker build picks it up, like `REACT_APP_BRAND_NAME`, `REACT_APP_COMPANY_NAME` and `REACT_APP_PRESENTATION_URL` (an optional company presentation link on the candidate status page; see [BRANDING.md](BRANDING.md)).

### Webhook secrets

Every address that outside services call proves who is calling, and **refuses everything while its secret isn't set**: Twilio's webhooks answer 403, the others answer 503 ("disabled until … is set"). That is deliberate: these addresses create candidates, book interviews and send texts.

| Caller | Address | Proof | Setting |
| --- | --- | --- | --- |
| Twilio | `/api/webhooks/twilio/inbound-sms`, `/api/twiml/*` | Twilio's signature | `TWILIO_AUTH_TOKEN` |
| Twilio (call bridge) | `/api/twiml/stream/<call>/<token>` | a signed link that expires after 5 minutes, only handed to Twilio | `JWT_SECRET` |
| ElevenLabs (Olivia's tools, call start) | `/api/public/book-by-phone` and the other tools, `/api/webhooks/elevenlabs/conversation-init` | `X-CGR-Tool-Secret` header | `ELEVENLABS_TOOL_SECRET` |
| ElevenLabs (after a call) | `/api/webhooks/elevenlabs/post-call` | ElevenLabs' HMAC signature | `ELEVENLABS_WEBHOOK_SECRET` |
| SendGrid Inbound Parse | `/api/webhooks/sendgrid/inbound`, `/inbound-email` | `?key=` in the address | `SENDGRID_INBOUND_SECRET` |
| Zapier or similar | `/api/webhooks/zapier/<token>` | the token in the address | Settings → Integrations |
| Your field app | `/api/internal/*`, `/api/webhooks/cg1/*` | `x-webhook-secret` header | `CGRECRUIT_WEBHOOK_SECRET`, `CG1_WEBHOOK_SECRET` |
| Partner feed | `/api/partner/stage-events` | `X-Partner-Api-Key` header | `PARTNER_FEED_API_KEY` |

Settings that belong to an office (time zone, call window, templates, Olivia's script, slots, phone numbers) are **not** environment variables. They live in the database and you change them in the app under **Settings**.

## Updating from upstream

Improvements and security fixes arrive in this project. To bring them into your copy:

```
git fetch upstream
git merge upstream/main
```

Most of your customisation lives in the database (Settings), so merges are usually clean. Files you changed for your company and branding (`backend/company_profile.json`, the logo, icons, `frontend/public/index.html`, `frontend/public/manifest.json`, colours) can conflict: keep your version of those. If the update changed `backend/requirements.txt` or `frontend/package-lock.json`, reinstall locally first (`pip install -r backend/requirements.txt`, then `npm ci` in `frontend`). Then run the tests, check the app locally, and `git push`. Railway deploys it.

Two things happen on every deploy (and every restart):

- **Calls in progress are dropped.** Open **Call Queue** first and deploy when no call is live.
- **Every ElevenLabs agent is rewritten from the app.** At start-up the server re-syncs all of Olivia's agents (prompt, first message, voice, tools) from your Settings, as saving does. An update that changes how the prompt is built changes what Olivia says from that deploy on, so read what changed and make a test call afterwards. Anything you edited directly in ElevenLabs is overwritten: make changes in the app's Settings instead.

In Claude Code: "Bring in the latest from upstream, keep my branding, run the tests and tell me what changed."

## Running the tests

The backend tests are self-contained: they don't need a database or the internet. Run each file on its own, from `backend/`, with a deliberately dead database address so nothing can be touched:

```
cd backend
pip install -r requirements-dev.txt   # in-memory database for a few tests
for t in tests/test_*.py; do
  MONGO_URL=mongodb://127.0.0.1:1/x DB_NAME=x .venv/bin/python -m pytest -q --noconftest -p no:cacheprovider "$t"
done
```

Always pass `--noconftest`. Two older files (`test_backend.py`, `test_v5_features.py`) test a running server over the network: they skip themselves unless you set `CGRECRUIT_TEST_URL` (to a local test server, run without `--noconftest`). Never point them at your live app.

The tests never read `backend/.env` (the app skips it while pytest is running), so your own settings can't change the results and your real `MONGO_URL` is never used. Every other file should pass. On Windows PowerShell, run the files one at a time the same way: `$env:MONGO_URL='mongodb://127.0.0.1:1/x'; $env:DB_NAME='x'; .venv\Scripts\python -m pytest -q --noconftest -p no:cacheprovider tests\<file>`.

## Troubleshooting

**The page is black or blank after a deploy.** The web app was built with the wrong `REACT_APP_BACKEND_URL`. For the live build it must be empty, so the app calls its own address. Check the browser console: calls to `undefined/api/…` or to `localhost` confirm it.

**An `/api/…` address returns the web page instead of data.** The app sends any address it doesn't recognise to the web page. Usually that route doesn't exist in the deployed version: compare `/api/version` with your latest commit.

**I can sign in locally but get thrown back to the login page.** Set `AUTH_COOKIE_SECURE=false` in `backend/.env` (local only) and restart the server.

**"Accounts are created by an administrator."** The database already has an owner. Sign in as the owner and add people in **Settings → Team**.

**"Set SETUP_TOKEN on the server…" or "Wrong or missing setup token."** Creating the first account needs `SETUP_TOKEN` in Railway → Variables (deployed) and the same value typed into `create_owner.py`.

**The server won't start: "JWT_SECRET must be at least 32 characters".** Generate a longer one (step 4). Changing it signs everyone out.

**Applicants see "Too many applications from this connection" on the apply page.** The public apply form accepts up to 20 applications from one internet address in 15 minutes (it answers 429 after that, before anyone is texted or called). A busy shared connection, such as a careers fair on one Wi-Fi, can hit it; wait 15 minutes.

**A webhook answers 503 "disabled until … is set" (or 403 for Twilio).** That service's secret isn't set (for Twilio: `TWILIO_AUTH_TOKEN`, and the address in Twilio must match `APP_PUBLIC_URL` exactly); see [Webhook secrets](#webhook-secrets).

**"Too many failed sign-in attempts."** Ten wrong passwords for one account within 15 minutes pauses that account for 15 minutes. A redeploy also clears it.

**Atlas: `bad auth`.** Wrong password in `MONGO_URL`, or symbols in it. Make a new database user with a letters-and-numbers password.

**Atlas: timeouts (`ServerSelectionTimeoutError`).** Network Access must include `0.0.0.0/0`. On a Mac, also run **Install Certificates.command** (step 1).

**The server won't start: `KeyError: 'MONGO_URL'` or `'DB_NAME'`.** Those two are required. On Railway, check the variables were deployed (the staged-changes banner).

**Railway variables don't take effect.** Railway stages changes; click **Deploy** on the banner.

**Times are out by some hours.** Check `APP_TIMEZONE` and **Settings → Region & Language → Time zone** for the office. See also [UK.md](UK.md#time-zone).

**Texts don't arrive in the app, or Twilio shows errors.** See [TWILIO.md](TWILIO.md#when-it-doesnt-work).

**Calls don't happen, or transcripts don't appear.** See [ELEVENLABS.md](ELEVENLABS.md#when-it-doesnt-work).

**Emails don't arrive.** `SENDGRID_API_KEY` must have Mail Send permission and `SENDGRID_FROM_EMAIL` must be a verified sender. SendGrid → Activity shows what happened to each email.

### Locked out of the owner account

With SendGrid set up, use **Forgot password** on the login page. Without email:

1. In your own terminal, make a new password hash (it asks for the password without showing it):

   ```
   cd backend && .venv/bin/python -c "import bcrypt,getpass; print(bcrypt.hashpw(getpass.getpass('New password: ').encode(), bcrypt.gensalt(12)).decode())"
   ```

2. In Atlas → Browse Collections → your database → `users`, find your account and replace the `password_hash` value with the line it printed.
3. Sign in with the new password.

Don't delete the owner account to start again: every office, candidate and setting belongs to it.
