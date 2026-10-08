# CGRecruit — an AI recruiting CRM for field-sales offices

**CGRecruit** is the recruiting system Cube Group USA runs its offices on. Someone applies, and within minutes **Olivia**, the AI recruiter, calls or texts them, asks the screening questions, books them into an interview, sends the reminders and keeps the conversation going until they start. Your recruiters work the whole pipeline on one live board, and new starters hand over to your field app.

It is open source (MIT). Clone it, open it in Claude Code, and you can have your own copy running on your own database, phone numbers, domain and branding.

**See it:** a guided tour is on the showcase site: **[tech-stack.cubemarketing.us/#/cgr](https://tech-stack.cubemarketing.us/#/cgr)**.

## How a candidate moves through it

1. **They apply.** From your apply page, a careers-page widget, an emailed CV, an intake webhook (Zapier or similar), or a recruiter adding them by hand. Job-board "new application" emails without a CV attached (Indeed's, for example) are recognised and listed for a recruiter to add.
2. **Olivia gets in touch within minutes.** By phone (an ElevenLabs voice agent on your Twilio number) or by text first, depending on how you set each office up.
3. **Screening.** A short set of questions you write: the must-haves first (age, right to work, hours, commute), then a couple of conversational ones.
4. **Booking.** Olivia offers real interview slots from your calendar and books one during the call or text conversation. If nothing suits, the candidate gets a link to pick their own time.
5. **Reminders and rescheduling.** Confirmation by email and text, reminders before the interview, a reschedule page, and follow-ups for anyone who misses it.
6. **Your recruiters take over.** Interview outcome, post-interview form, offer, training date.
7. **Hand-off.** New starters are passed to your field app (optional), with their start date and first-day details.

## What's inside

**For recruiters**
- A live **board** for each office (Screening → Appointment → Form → To Close → Training), with drag-and-drop moves, bulk actions, filters and duplicate detection.
- A **candidate drawer** with the CV, call recordings and transcripts, AI call summaries, the full text and email thread, and a reply box.
- An **Inbox** for every SMS conversation, including numbers that aren't candidates yet.
- A **Calendar** of interviews and training, and an **outcome-due** list so nobody is left unmarked after their interview.
- A **Call Queue** showing who Olivia is calling now, who's queued and who's paused.
- **Needs attention** and a notification bell for the things a human must act on.

**Olivia, the AI recruiter**
- **Voice screening** with an ElevenLabs conversational agent: natural turn-taking, voicemail detection, and booking tools that read and write your real calendar mid-call.
- **Text screening** powered by Claude: the same questions by SMS, with replies paced like a person and a booking link when it helps.
- **Email replies**: candidates can answer any email and get a sensible reply, including reschedules.
- **Inbound calls**: an optional receptionist agent that answers your office number, recognises booked candidates, gives directions and reschedules.
- **No-show revival** (off by default): a courtesy call to people who missed an interview, offering a new time.
- Per-office **voice, name, opening line and local knowledge**, with a test-call button that rings your own phone.

**Running the offices**
- **Offices** (called pipelines in the code), each with its own apply link, phone number, slots, prompts and templates; settings can be set once for everyone or overridden per office.
- **Booking & Slots**: weekly availability, blackouts, how many slots to offer, and booking windows.
- **Applicant Comms**: every email and text template in one place, with per-channel switches and custom reminders.
- **Auto-Dialer**: call window and days, retry spacing, and a cap on simultaneous calls.
- **Intelligence**: funnel, cohort and activity views, speed-to-contact, a weekly funnel report, CSV export and AI-written insights.
- **Team**: recruiter and read-only accounts scoped to their offices, plus analyst access for figures only.

**Integrations**
- **Field-app hand-off** (optional): new starters, attendance and admin alerts exchanged with CG1, Cube Group's field app, or a simple "hired" webhook to any other app.
- **Google Sheets** (optional): new starters appended to a new-hires sheet.
- **Claude connector**: a read-only connection so you can ask Claude about your pipeline, signed in as yourself.
- **Demo accounts**: a view-only login for showing the system to someone without letting them change anything.

## Build your own with Claude Code

1. **Get the tools:** Git, Python 3.11 or newer, Node.js LTS and [Claude Code](https://code.claude.com/docs/en/setup). Step 1 of [docs/SETUP.md](docs/SETUP.md) shows how.
2. **Clone it and open it in Claude Code:**

   ```
   git clone https://github.com/consultingarian-eng/cgrecruit-os.git my-recruiting
   cd my-recruiting
   claude
   ```

   Keep your own copy in a **private** repository. It will hold your settings and, once you change them, your prompts and templates. [docs/SETUP.md](docs/SETUP.md) step 2 shows how.

3. **In Claude Code, run these in order:**

   | Command | What it does |
   | --- | --- |
   | `/setup` | Database, settings, a safe local run, Railway hosting, your domain and your first login, one step at a time, checking each one |
   | `/connect-twilio` | Your phone numbers, the text-message webhook, and a check that Twilio can reach the app |
   | `/connect-elevenlabs` | Olivia's voice agent, its voice and booking tools, and a test call to **your own** phone |
   | `/brand` | Your company name, logo, colours, Olivia's name and script, and your job, pay, location and schedule details |

You can stop at any point and run a command again; it picks up where you left off. Nothing texts or calls a real candidate during setup: every test goes to your own phone, and only when you say so.

## What you'll need

| Service | Needed? | What it does here |
| --- | --- | --- |
| Claude Code (a Claude subscription or Anthropic Console account) | Required for the guided setup | Runs `/setup`, `/connect-twilio`, `/connect-elevenlabs` and `/brand`, and makes your changes with you. You can follow the docs by hand without it |
| GitHub | Required | Your private copy of the code |
| MongoDB Atlas | Required | The database (a free tier is enough to start) |
| Railway | Required (or another host) | Runs the app on the internet |
| Anthropic API key | Required for the AI | Text screening, CV reading, call summaries, email replies |
| Twilio | Required for texts and calls | Your phone numbers, SMS, and the phone line Olivia's calls run on |
| ElevenLabs | Required for voice | Olivia's voice agent. Not needed if you run text-only |
| SendGrid | Strongly recommended | Confirmation and reminder emails, password resets, emailed CVs and email replies |
| A domain | Recommended | `recruit.yourcompany.co.uk` instead of a Railway address |
| A video-meeting service (Zoom, Microsoft Teams or Google Meet) | Recommended | The interview link: **Calendar → Manage availability** asks for a default meeting link, and it goes into confirmation emails and texts |
| Google Cloud service account | Optional | Writes new starters to a Google Sheet |
| A field app (such as CG1) | Optional | Hand-off of new starters |
| Claude (claude.ai) | Optional | The read-only Claude connector |

Everything optional stays off until you add its settings. What each one powers, what breaks without it, and pricing links: [docs/SERVICES.md](docs/SERVICES.md).

## The guides

| Guide | Read it for |
| --- | --- |
| [docs/SETUP.md](docs/SETUP.md) | The full manual: database, hosting, domain, first login, every setting, updates, troubleshooting |
| [docs/TWILIO.md](docs/TWILIO.md) | Numbers (UK and US), SMS and voice webhooks, signature checks, registration, testing safely |
| [docs/ELEVENLABS.md](docs/ELEVENLABS.md) | Olivia's agents, voices, the booking tools and webhooks, testing safely |
| [docs/SERVICES.md](docs/SERVICES.md) | Every outside service, what it powers and what happens without it |
| [docs/BRANDING.md](docs/BRANDING.md) | Your name, logo, colours, Olivia's persona and all the job content |
| [docs/UK.md](docs/UK.md) | Running it in the UK: time zone, numbers, texting and calling rules, GDPR |

## How it's built

```
Candidates: phone · SMS · email · web          Recruiters: browser / phone
        │          │       │       │                     │
     Twilio ─── ElevenLabs │       │                     │
        │          │    SendGrid   │                     │
        ▼          ▼       ▼       ▼                     ▼
   ┌──────────────────────────────────────────────────────────┐
   │  One Railway service                                     │
   │   ├─ the web app: React, built into backend/static       │
   │   └─ the server: Python, FastAPI, background scheduler   │
   │        (calls, retries, reminders, sweeps)               │
   └──────────────────────────────────────────────────────────┘
        │              │                 │                │
   MongoDB Atlas   Anthropic (Claude)   Google Sheets   Field-app webhooks
                                         (optional)       (optional)
```

- `backend/`: the FastAPI server (`server.py` plus `routes/`, `dialer/` and the services beside them). It also serves the built web app.
- `frontend/`: the React web app (Create React App with CRACO, Tailwind).
- `docs/`: the guides above. `.claude/skills/`: the Claude Code commands.

The app runs as **one** server process. The scheduler that places calls and sends reminders lives inside it, so never run two copies against the same database (no extra replicas, no second worker).

## Security

Report vulnerabilities privately; see [SECURITY.md](SECURITY.md). The repository contains no keys or passwords. Yours belong in your host's variables and a local `backend/.env` that git ignores.

## Credits and licence

Built and run in production by **Cube Group USA**. Released under the [MIT licence](LICENSE), Copyright (c) 2026 Cube Group USA.

This is not legal advice: you are responsible for how you contact candidates and handle their data where you operate. [docs/UK.md](docs/UK.md) covers the practical points for the UK.
