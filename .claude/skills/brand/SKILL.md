---
name: brand
description: Make CGRecruit the owner's own - company and office details in backend/company_profile.json, the web app's name, logo, icons and colours, Olivia's name, persona and script, the screening questions, and the job, pay, location, hours and first-day content in the app's Settings, then a sweep for leftover example or Cube Group wording and a look at the result. Writes candidate-facing text in plain British (or US) English, keeps texts GSM-7, and never sends anything to a candidate. Use when they ask to rebrand, change the name, logo, colours or icons, rename Olivia, change her script or questions, or edit job, pay, location, schedule or company details.
argument-hint: "[optional: 'look' (name, logo, colours), 'content' (Olivia and the job), or 'sweep']"
---

# Make it yours: branding and content

The source of truth is `docs/BRANDING.md`: read it now. It explains the three places content lives (the app's Settings in the database, `backend/company_profile.json`, and the web app's files) and which wins. If this skill and the guide disagree, trust the guide (and the code over both).

With `look`, do only part 2; with `content`, parts 1 and 3; with `sweep`, part 4. Otherwise do all four in order.

## Rules

- **Only true statements about their business.** Pay, hours, location, perks, company history: ask, never invent. Olivia repeats what she's given to every candidate.
- **No made-up people or records.** No sample candidates, reviews or testimonials.
- **Texts stay GSM-7**: no emoji, curly quotes, long dashes or `…` in any SMS body (one such character multiplies the cost of every text). Check each SMS body with `python3 .claude/skills/brand/gsm7_check.py` (paste or pipe the text in).
- **Settings are changed by the owner in the app.** You prepare the wording; they paste it and press Save (saving Screen Call Agent also updates ElevenLabs). Don't write to their database.
- **Never send a test text, email or call** without their explicit yes, and then only to themselves.
- Show them every file diff before committing; commit only with their OK, and push only to their own `origin`.

## 1. Gather (one short round of questions)

Ask what you don't already know from `backend/company_profile.json` and the app:

- Company name as candidates should see it; the app's name (for example "Acme Recruitment"); website; recruitment email and phone.
- Country (UK or US: spelling, currency, right-to-work wording, date and time style).
- Offices: name, full address, Google/Apple Maps links, which Twilio number each texts from.
- The role: title, what the work is, hours and days, how it's paid (in their words), training, progression, any perks they can stand behind.
- The interview: format (video or in person), length, who runs it, the meeting link.
- First days: times for day one and two, dress code, what to bring, food, parking, the regular schedule.
- Olivia: keep the name or choose another; the voice they like (from `/connect-elevenlabs`).
- A logo file (PNG or SVG, ideally square, at least 512 px) and their brand colours (hex codes, or a logo to pick them from).

## 2. The look (web app files)

1. **Name:** `BRAND.name` and `BRAND.companyName` in `frontend/src/lib/brand.js` (or the build variables `REACT_APP_BRAND_NAME` / `REACT_APP_COMPANY_NAME` in Railway), `frontend/public/index.html` (`<title>`, meta `description`, `apple-mobile-web-app-title`, `theme-color`) and `frontend/public/manifest.json` (`name`, `short_name`, `description`, `background_color`, `theme_color`).
2. **Icons from one logo** (Pillow is in the backend venv): make `favicon-16.png`, `favicon-32.png`, `apple-touch-icon.png` (180), `icon-192.png`, `icon-512.png`, and `icon-maskable-512.png` (logo within the central 80% on a solid background) in `frontend/public/`. Replace `frontend/public/logo.png` (the header and sign-in logo). Every page reads it through `BRAND.logo` in `frontend/src/lib/brand.js`; if you use another file name, change it there once, not page by page.
3. **Colours:** the recruiter app's `--primary` / `--ring` variables in `frontend/src/index.css` (HSL triplets) and the `brand` colours in `frontend/tailwind.config.js`; the candidate pages' palette in `tailwind.config.js` and its variables in `index.css`. Keep text readable: check contrast of text on the new primary colour.
4. If `frontend/public/index.html` has a third-party analytics snippet, ask whether to remove it or use their own key.
5. **Look at it:** with the local rig from `docs/SETUP.md` step 5, `npm start` and have them check the sign-in page, the board and an apply page (`/apply/<slug>`) on desktop and phone width. Or build (`REACT_APP_BACKEND_URL= npm run build`) and confirm `build/index.html` carries the new title.

## 3. Content

**`backend/company_profile.json`** (you edit it, with their OK): `company_name`, `website`, `contact_email`, `contact_phone`, `logo_url`, `role_title`, `work_schedule`, `pay_summary`, `about_company`, `field_app`, every office (`label`, `match`, `address`, maps links, `sms_number`, `starter_template`), `starter_template_defaults`. Remove the example offices and every `EDIT ME`. Validate with `python3 -m json.tool backend/company_profile.json`.

**In the app** (you draft, they paste and save), in this order, in the **global default** first. A new office copies the settings when it's created and never follows later global edits, so ideally this happens before any office exists. If offices already exist, they either use **Edit Global default** (the banner button) and then **Reset to global default** on each office (which discards that office's own settings), or save the content in each office as well. Check **Auto-Dialer → Auto-dialer enabled** is off until go-live (it's on in a new install).

1. **Company & Branding:** company name (also the email sender name), city, role, recruiter email and phone, website, logo URL (an `https://` image), social links, interview etiquette tips.
2. **Screen Call Agent:** Agent Name; **Purpose** (persona and pace, for their role); **Opening Message** (says she's an AI assistant and, especially in the UK, that the call is recorded); **screening questions** (rewrite the defaults: the right-to-work question asks about a "student visa", the hours one quotes `work_schedule`, the commute one asks for 4+ days a week; keep must-haves first and short); **Booking Instructions**; **Closing Message**; **Additional Context** (company, role, pay, hours, location, training, interview format: true and current); **Voicemail message**. Use only the `{{…}}` variables listed under "What the script can use" in `docs/ELEVENLABS.md` (no last name, date, time or meeting link: none is sent when a call starts).
3. **Office AI Voice & Prompts:** per office, the name, opening and office-specific context (directions, transport, who it suits).
4. **Applicant Comms:** go through every template; keep `[Placeholders]` from the list in `docs/BRANDING.md`; GSM-7 check every SMS body.
5. **Job Ads** (per office, required: at least one with **Ad is running** on, or the apply page refuses applications; its title fills `{{role}}` and `[Role]`), **Starter Email**, **Post-Interview Form** (check the "postcode or ZIP" address, the citizen / work-visa / sponsorship answers and "rideshare" for their country), **Text Screening**, **AI Stage Prompts**, **No-Show Revival** and **Inbound Call Agent** scripts if used.
6. **Calendar → Manage availability:** meeting link and the recruiter's name per office.

Offer to also update the defaults in `backend/models.py` (`RecruiterProfile`, `ScreenCallAgentSettings`, `CustomFormSettings`, `get_default_templates()`) so future offices start from their wording; explain that this doesn't change the running app. If they do, run the backend tests afterwards (`docs/SETUP.md` → Running the tests); `test_screening_shortened.py` checks the arrival text stays GSM-7.

## 4. Sweep for leftovers

Search the code and docs they'll ship (not `node_modules`, `build` or `backend/static`):

```
grep -rniE "cube ?group|cubemarketing|cgrecruit|example field sales|edit me|downtown|riverside|boston|new haven" \
  backend frontend/src frontend/public --include='*.py' --include='*.js' --include='*.jsx' --include='*.json' --include='*.html' --include='*.css' -l
```

Also ask them to skim the live Settings screens for old wording, and decide together what to change; some matches are internal names (for example `cg1_office_key`, `cube_token`) that should stay. Mention that the code still uses the name CGRecruit internally and in a few system emails, and offer to change what candidates or staff can see.

## When you finish

List what changed (files, and the Settings sections they saved), what's still to do (for example "Post-Interview Form not reviewed"), and suggest a full test journey on themselves: apply on their own apply link with their own number and email, get the text or call, book, receive the confirmation and reminder, then delete the test candidate.
