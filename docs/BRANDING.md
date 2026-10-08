# Branding and content: make it yours

Everything a candidate reads, hears or sees should be **your** company: the name, the logo, Olivia's name and voice, the job, the pay, the place, the hours. In Claude Code, `/brand` walks you through this page, makes the file changes with you and shows you the result.

Your content lives in three places. Knowing which is which saves a lot of confusion:

| Where | What lives there | How you change it |
| --- | --- | --- |
| **The app's Settings** (stored in the database) | Olivia's script and questions, templates, company profile, slots, per-office overrides | In the app. Takes effect straight away. This is where most of your content goes |
| **`backend/company_profile.json`** | Company basics and **offices**: addresses, map links, texting numbers, first-day details, spreadsheet tabs | Edit the file, commit, push. Read when the server starts |
| **The web app's files** (`frontend/`) | App name, icons, logo, colours, the browser tab | Edit the files, commit, push. Railway rebuilds the web app |

When the same thing is set in two places, **a value saved in Settings wins** over `company_profile.json`, and a value saved for one office wins over the all-offices value.

---

## 1. Company and offices: `backend/company_profile.json`

The file ships with example values (an "Example Field Sales Co." with two made-up offices). Replace them with yours:

| Field | What it's for |
| --- | --- |
| `company_name`, `website`, `contact_email`, `contact_phone`, `logo_url` | Used wherever an office hasn't set its own in **Company & Branding** |
| `role_title` | The job, for example `Sales Representative` |
| `work_schedule` | The hours, in words |
| `pay_summary` | How the role is paid, in words |
| `about_company` | Two or three sentences about you |
| `default_office` | The office to assume when one can't be worked out |
| `field_app` | Optional: the name and install page of your team app, for the starter email |
| `offices` | One entry per office (below) |
| `starter_template_defaults` | First-day times, dress code, ID, food and parking text, used unless an office or Settings says otherwise |

Each **office** entry:

| Field | What it's for |
| --- | --- |
| `label` | The office's display name |
| `match` | Words in an office's (pipeline's) name or slug that mean "this office", for example `["manchester"]` |
| `address` | The street address in emails and given by the inbound agent |
| `google_maps_link`, `apple_maps_link` | Directions links in starter and booking emails (optional) |
| `sms_number` | The Twilio number this office texts from, in `+44…` form (optional; falls back to `TWILIO_PHONE_NUMBER`) |
| `starter_template` | First-day details for this office, overriding the defaults |
| `sheet_tab`, `sheet_columns`, `sheet_week_prefix`, `sheet_attended_headers` | Only if you use the Google Sheets new-hires export |
| `partner_pin` | Only for the optional partner reporting feed |

Office keys (`downtown`, `riverside` in the example) are short lower-case names. Link each office in the app to its key with **Settings → Offices & Variants**; if you don't, the app matches on the office's name using the `match` words.

Instead of editing the file in the repo you can keep it elsewhere and point `COMPANY_PROFILE_PATH` at it. After any change, deploy (or restart locally): the file is read once at start-up.

## 2. In the app: Settings

**Do the global default first, before you create any office.** A new office starts with its own copy of the settings as they are at that moment, and later changes to the global default don't reach it. With no office yet, everything you save here is the global default. Once offices exist, the banner at the top of each section shows which office you're editing; its **Edit Global default** button switches to the global default (the banner turns amber: "Editing the global default for …"). To bring an existing office back to the global default, edit that office too, or use **Reset to global default** in its banner, which discards all of that office's own settings. See [SETUP.md step 9](SETUP.md#9-your-content-then-your-first-office).

**Company & Branding.** Company name (also the email sender name), city, default job role, recruiter email and phone, website, logo URL (an `https://` image, shown in the starter email), Instagram and LinkedIn, and interview etiquette tips added to the interview confirmation email.

**Screen Call Agent: Olivia's persona and script.** This is the heart of it.
- **Agent Name**: Olivia by default. Use any name you like.
- **Purpose**: who she is, how she talks, the pace. The default describes a field-sales screening; rewrite it for your role.
- **Opening Message**: the first thing she says.
- **Screening questions**: the defaults are four must-haves (age, right to work, hours, commute) and two open questions. **Rewrite them for your job and country**: the right-to-work question checks for a "student visa" (in the UK you'd ask about the right to work in the UK, and what a candidate must show), the hours question quotes `work_schedule` from `company_profile.json`, and the commute question asks for "4+ days a week" at the office.
- **Booking Instructions** and **Closing Message**: how she moves to booking and signs off.
- **Additional Context (FAQs, etc.)**: everything she may be asked: what the company does, the role, **pay**, **hours**, **location**, training, the interview format. Write only what's true and current; she will repeat it to candidates.
- **Voicemail message**, **voice**, **language**: see [ELEVENLABS.md](ELEVENLABS.md#3-choosing-the-voice).

In the call script you can use `{{first_name}}`, `{{full_name}}`, `{{agent_name}}`, `{{company}}`, `{{role}}` (the job ad's title), `{{city}}`, `{{phone}}`, `{{email}}` and `{{pipeline_slug}}`, and nothing else: no last name, interview date, time or meeting link is known when a call starts, and a variable the app doesn't send can stop calls connecting ([the full list](ELEVENLABS.md#what-the-script-can-use)). The interview is booked during the call, so the closing message has Olivia read the chosen day and time back. Saving pushes the script to ElevenLabs.

**Office AI Voice & Prompts.** Per office: Olivia's name, opening message, office-specific context (directions, local transport, who the office suits) and voice, plus the test-call button.

**Text Screening and AI Stage Prompts.** How Olivia behaves by text, and extra instructions for the AI that answers texts and emails at each stage.

**Applicant Comms.** Every email and text template: subject, email body, text body, and which channels each one uses. Placeholders in square brackets are filled in per candidate, for example `[First Name]`, `[Company]`, `[Role]`, `[City]`, `[Date]`, `[Time]`, `[Agent Name]`, `[Zoom Link]` (the meeting link), `[Reschedule URL]`, `[Retry Link]`, `[Pick Time Link]`, `[Start Date]`, `[Start Time]`, `[Recruiter Email]`, `[Recruiter Phone]`, `[Website Line]`, `[Instagram Line]`. A placeholder that has no value disappears with its line.

Keep **text** templates to plain characters: no emoji, curly quotes or long dashes. One such character makes every text cost several times more ([TWILIO.md](TWILIO.md#costs)).

**Starter Email.** The first-day email: times for day one and day two, the regular schedule, dress code, what to bring, food, parking, notes.

**Post-Interview Form.** The questions candidates answer after the interview. Read them for your country: the defaults ask for an address with "postcode or ZIP", offer "Yes — citizen / permanent resident", "Yes — current work visa" and "No / sponsorship needed" as right-to-work answers (UK wording would be about the right to work in the UK and share codes), and offer "rideshare" as transport.

**Job Ads (one per office, required).** Each office needs at least one job ad with **Ad is running** on: **Settings → Job Ads → + New Job Ad**, then the **Job Title**, the description (Claude compares CVs against it) and the location. Without a running ad the office's apply page says there are no open roles and won't accept applications. The title is what Olivia says for `{{role}}` and what `[Role]` becomes in templates (when a candidate has no job ad, templates fall back to the default job role in **Company & Branding**). Job ads belong to the office picked in the app's office switcher.

**Booking & Slots, Auto-Dialer, No-Show Revival, Inbound Call Agent.** Slot rules, call window and days, and the scripts of the optional agents. **Auto-dialer enabled** is on in a new install: keep it off until you go live.

**Calendar → Manage availability.** Interview times, slot length, the default meeting link (Zoom, Teams or Meet) and the recruiter's name (candidates see "Interview with <name>").

### Defaults for new installs

The values a brand-new database starts with are in `backend/models.py` (`RecruiterProfile`, `ScreenCallAgentSettings`, `CustomFormSettings`, `get_default_templates()`). Changing them doesn't change an app that is already running: its saved settings win. Edit them only if you want every future office or install to start from your wording.

## 3. The web app: name, icons, logo, colours

All in `frontend/`. After changing them, push; the deploy rebuilds the web app.

| What | Where |
| --- | --- |
| Browser tab title, description, home-screen name | `frontend/public/index.html` (`<title>`, `description`, `apple-mobile-web-app-title`, `theme-color`) |
| Installed-app name and colours | `frontend/public/manifest.json` (`name`, `short_name`, `description`, `background_color`, `theme_color`) |
| Icons | `frontend/public/`: `logo.png` (above), `favicon-16.png` (16×16), `favicon-32.png` (32×32), `apple-touch-icon.png` (180×180), `icon-192.png`, `icon-512.png`, `icon-maskable-512.png` (512×512 with the logo inside the central 80%) |
| The logo in the app's header and sign-in pages | `frontend/public/logo.png`: replace the file (a square PNG). Every page uses it through `BRAND.logo` in `frontend/src/lib/brand.js`, so to use another file name change it there once, not page by page |
| App name in the header and sign-in pages, and the fallback company name on candidate pages | `BRAND` in `frontend/src/lib/brand.js`, or without editing code the build variables `REACT_APP_BRAND_NAME` and `REACT_APP_COMPANY_NAME` (Railway service variables; the Docker build passes them in). Candidate pages normally show each office's **Company Name** from Settings |
| Recruiter app colours | `frontend/src/index.css` (the `--primary`, `--ring` and other variables, written as hue, saturation, lightness) and the `brand` colours in `frontend/tailwind.config.js` |
| Candidate pages' colours (apply, status, retry, reschedule and referral pages) | The `--cube-*` colour variables on the `.cube-brand` class in `frontend/src/index.css` (`--cube-magenta`, `--cube-purple`, `--cube-grad`, `--cube-ink` and so on; the pages wrap themselves in `.cube-brand`), and the `cube` colours in `frontend/tailwind.config.js` that the `text-cube-*` / `bg-cube-*` classes use. Change the values, not the names. The animated background and loading spinner are `frontend/src/components/CubeBg.jsx` and `CubeLoader.jsx` |
| Embed widget button and pop-up | `backend/routes/widget.py` (its colours are written out in the inline CSS: the gradient on `.cgr-apply-btn` and the dark modal colours) |

**The shipped look is a sample.** The logo, icons and the magenta-and-purple candidate palette are the original company's style, included so the app works out of the box. Replace the logo and icons and set your own colours before candidates see the pages.

Square PNGs with a transparent or solid background work best. Claude Code can make every size from one large logo.

If `frontend/public/index.html` contains a third-party analytics snippet, remove it or replace its key with your own: otherwise your visitors' activity is sent to someone else's account.

### The candidate status page's presentation link

The page candidates see after booking (`/applicant/...`) can show a link to a presentation about your company. Set `REACT_APP_PRESENTATION_URL` in Railway → Variables to its address before a deploy (it's baked in when the web app is built); leave it blank and the section is hidden.

## 4. Email sender

Emails come from `SENDGRID_FROM_EMAIL` (an address SendGrid has verified), with the office's **Company Name** from **Company & Branding** as the sender name (`SENDGRID_FROM_NAME` if that's empty). Password-reset emails use the app's own wording: ask Claude Code to change it if you want your name in it.

## Checklist

- [ ] `backend/company_profile.json`: company basics, role, pay, schedule, about, every office (address, maps link, texting number, first-day details)
- [ ] Settings → **Auto-Dialer**: **Auto-dialer enabled** off until go-live (global default, before creating offices)
- [ ] Settings → **Company & Branding** (global default first, then any office that differs)
- [ ] Settings → **Screen Call Agent**: name, purpose, opening, **questions rewritten for your country and job**, booking, closing, Additional Context (pay, hours, location, interview format), voicemail message, voice
- [ ] Settings → **Office AI Voice & Prompts** for each office, then a test call to your own phone
- [ ] Settings → **Applicant Comms**: read every template once; plain characters in texts
- [ ] Settings → **Starter Email** and **Post-Interview Form** (check the address, right-to-work and transport questions for your country)
- [ ] Settings → **Job Ads**: at least one running job ad per office (the apply page needs it)
- [ ] Calendar → **Manage availability**: slots and meeting link for each office
- [ ] `frontend/public`: title, manifest, icons, `logo.png`; `frontend/src/lib/brand.js` name; colours in `index.css` and `tailwind.config.js`
- [ ] No leftover example or Cube Group wording: ask Claude Code to search for it
- [ ] A full test journey on yourself: apply, text and call, book, confirmation email, reminder
