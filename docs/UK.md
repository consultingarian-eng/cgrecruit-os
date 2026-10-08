# Running CGRecruit in the UK

CGRecruit was built for US offices and runs in production there. It works in the UK, but a few defaults assume the US, and contacting candidates by AI phone call and text brings UK rules with it. This page covers both, practically.

**This is not legal advice.** It's a checklist of what to think about, written for office owners. For anything you're unsure about, ask a solicitor or data-protection adviser, and read the ICO's and Ofcom's own guidance (linked below).

---

## Before you go live: the short list

- [ ] `APP_TIMEZONE=Europe/London` (and `REACT_APP_TIMEZONE=Europe/London`) on Railway, and every office's **time zone** set to `Europe/London` ([Time zone](#time-zone))
- [ ] `PHONE_DEFAULT_COUNTRY=GB` on Railway, so numbers typed the UK way are stored as **+44** ([Phone numbers](#phone-numbers))
- [ ] **UK mobile** Twilio numbers with an approved Regulatory Bundle ([TWILIO.md](TWILIO.md#2-buying-numbers))
- [ ] Screening questions, Additional Context and the Post-Interview Form **rewritten for the UK** (right to work in the UK, not "the US"; no state or ZIP) ([BRANDING.md](BRANDING.md#2-in-the-app-settings))
- [ ] Olivia says she's an AI assistant and that the call is **recorded and transcribed**
- [ ] A **privacy notice** for candidates, linked from your apply page and first message
- [ ] Your **retention period** decided and a routine for deleting old records
- [ ] **ICO data protection fee** paid, unless the ICO's self-assessment says you're exempt ([fee page](https://ico.org.uk/for-organisations/data-protection-fee/))
- [ ] Call window and days set to sensible UK hours

---

## Time zone

Two settings, both `Europe/London`:

1. **`APP_TIMEZONE`** in Railway → Variables (and `backend/.env`). It is the home time zone for everything that runs for the whole deployment: the background schedulers, the training confirmation text's 08:00–21:00 sending window, the gate-check sweep, start dates and times in the starter email and the field-app hand-off, the "current date and time" the AI is given, and the default zone for any office that hasn't set one. Add **`REACT_APP_TIMEZONE`** with the same value so the web app's calendar defaults match (the Docker build picks it up).
2. **Settings → Region & Language → Time zone**, for all offices and for any office with its own settings. Slot times, reminders, the call window and the follow-up nudges' quiet hours follow it. New offices start with `APP_TIMEZONE`.

Times shown to candidates carry the zone's own abbreviation (GMT or BST), not "ET".

**Still US-shaped (change them for your offices if they don't fit):**

- The **two-day training block** (Monday and Tuesday) and the "next cohort starts the following Monday" reschedule rule in `training_sms.py` and the starter email defaults (`backend/company_profile.json`).
- The call window defaults (09:00–19:00, Monday to Saturday) are UK-friendly but check them in **Settings → Auto-Dialer**.

Then test a full journey (apply, book, reminder, move to Training) on yourself.

## Phone numbers

Twilio and ElevenLabs need numbers in international form: `+447700900123`. Set **`PHONE_DEFAULT_COUNTRY=GB`** in Railway → Variables (and `backend/.env`) and the app reads numbers typed the UK way:

| Typed | Stored |
| --- | --- |
| `07700 900123` | `+447700900123` |
| `447700900123` | `+447700900123` |
| `7700 900123` (leading 0 dropped) | `+447700900123` |
| `+44 (0)7700 900123` | `+447700900123` |
| `+1 617 555 0134` (any number starting with `+` or `00`) | kept, as an international number |

It applies everywhere a number comes in or is matched: the apply and referral forms, manual add, the intake webhook, email intake, the candidate portal's "confirm your 1-on-1 number" card, replies to texts and the inbound receptionist. The candidate pages show a UK placeholder and accept a UK number in that card. All the rules are in `backend/phone_util.py` (tests: `tests/test_phone_util.py`).

With the default (`US`), a bare 10-digit number gets `+1`, so `7700 900123` would become a wrong US number. Set the variable **before** candidates arrive; numbers already stored aren't rewritten.

## Your Twilio numbers

- Use **UK mobile numbers (+44 7…)** for anything that texts: Twilio lists two-way SMS as supported in the UK ([Twilio's UK SMS guidelines](https://www.twilio.com/en-us/guidelines/gb/sms)). When you buy a number, check that its capabilities include SMS; UK local (geographic) numbers are usually voice-only. The UK has no US-style 10DLC registration for ordinary numbers; pre-registration applies only to protected alphanumeric sender IDs (same page).
- Twilio asks for identity and address details before it activates a UK number (a Regulatory Bundle in the console). For UK mobile numbers the address may be anywhere; for local numbers it must be a UK address ([Twilio's UK regulatory requirements](https://www.twilio.com/en-us/guidelines/gb/regulatory)). Start early: review can take days.
- **Caller ID:** Ofcom expects the number you call from to be a real, valid number that people can call back. Each office's Twilio number is shown as Olivia's caller ID; make sure calls back to it are answered (the inbound receptionist, or forwarding) and never withhold it. Ofcom's guidance on calling line identification (CLI) is on [ofcom.org.uk](https://www.ofcom.org.uk) (search for "calling line identification").

## Texting candidates

- Texts about someone's **own application** (confirmations, reminders, screening questions, reschedules) are service messages, not marketing. The marketing-consent rules in the **Privacy and Electronic Communications Regulations (PECR)** bite when you text people about **other** things, such as new vacancies, to candidates who haven't agreed to that. Don't, without consent.
- Only text people who gave you their number for this application, say who you are, and give a way to stop. The app adds "Reply STOP to opt out." to the first text each person gets, and stops texting anyone who replies STOP.
- **Quiet hours.** Follow-up nudges that would land at night are held until the morning, in the office's time zone; the training confirmation text uses `APP_TIMEZONE`. Replies to a candidate who is texting you go straight away, at any hour, because they started the conversation.
- Keep text templates plain (no emoji or curly quotes), short and expected.
- ICO guidance: [electronic and telephone marketing](https://ico.org.uk/for-organisations/direct-marketing-and-privacy-and-electronic-communications/).

## Calling candidates with an AI agent

- Calls about someone's own application aren't marketing calls, so the TPS and the marketing-call rules don't apply to them in the same way. Act in the same spirit anyway: if someone asks not to be called, pause them (**Pause** in the **Call Queue** stops automated calls to that person until you resume them) and switch them to text.
- **Be open about it.** Tell candidates in the arrival message that an AI assistant will call (the default arrival template does), and have Olivia say near the start that she's an AI assistant and that the call is recorded and transcribed. Put that in her **Opening Message** in **Settings → Screen Call Agent**.
- **No silent or abandoned calls.** Ofcom treats repeated silent and abandoned calls as persistent misuse. Olivia speaks as soon as the call connects, and voicemail detection leaves a short message naming your company rather than hanging up silently. Keep both on. Ofcom's statement of policy on persistent misuse is on [ofcom.org.uk](https://www.ofcom.org.uk) (search for "persistent misuse").
- **Hours.** Set **Settings → Auto-Dialer → call window and days** to sensible UK times (the default is 09:00 to 19:00, Monday to Saturday, in the office's time zone), and keep retry attempts low.

## Candidates' data (UK GDPR)

CGRecruit holds personal data: names, contact details, CVs, answers to screening questions, call recordings and transcripts, texts and emails, and an AI assessment of each call. You are the **controller** of that data; the services the app uses are your **processors**.

**Register with the ICO.** Organisations processing personal data must pay the [data protection fee](https://ico.org.uk/for-organisations/data-protection-fee/) unless they're exempt; the ICO's self-assessment on that page tells you which applies.

**Lawful basis.** Processing an application someone has made is usually justified as taking steps they asked for before a possible contract, or as your legitimate interests. Record which you rely on, and for the AI screening calls and recordings, a short legitimate interests assessment. Don't ask about health, ethnicity, religion or other special-category matters in screening questions or forms.

**Automated decisions.** The app can reject a candidate automatically when they fail a must-have question (for example, no right to work), and it records an AI verdict on each screening. UK GDPR has specific rules for significant decisions made solely by automated means (updated by the Data (Use and Access) Act 2025): tell candidates that it happens, and give them a way to challenge it and have a person look again. The simplest approach: have a recruiter check AI rejections before they become final, and say in your rejection email that they can reply to ask for a review. See the ICO's [guidance on automated decision-making](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/individual-rights/automated-decision-making-and-profiling/).

**Privacy notice.** Write one for candidates: what you collect (including recordings and transcripts), why and on what basis, the AI involvement, who processes it (list the services below), where it's stored, how long you keep it, their rights and how to contact you. Link it from your apply page and your first email and text. Claude Code can add the link to the apply page and templates (`/brand`).

**Recordings and transcripts.** They're stored by ElevenLabs, and the app copies transcripts and summaries into its database. In ElevenLabs, review each agent's privacy and retention settings so they match your retention period.

**Where data goes.** The processors CGRecruit uses, and what they hold:

| Service | Holds |
| --- | --- |
| MongoDB Atlas | Everything the app stores (choose the London region) |
| Railway | The running app; passes data through (choose the EU West region, Amsterdam: service **Settings → Region**, see [Railway's regions page](https://docs.railway.com/deployments/regions)) |
| Twilio | Text messages and call logs |
| ElevenLabs | Call audio, transcripts, the details passed to each call |
| SendGrid | Emails sent and received |
| Anthropic | CVs, texts and transcripts sent for the AI to read and answer |
| Google (if used) | New starters in your spreadsheet |

Several are US companies. Check each one's data processing agreement and how it covers UK transfers (the UK International Data Transfer Addendum, or the UK extension to the EU-US Data Privacy Framework), and keep copies.

**Retention.** Decide how long you keep unsuccessful candidates' data and new starters' recruitment records, write it in your privacy notice, and stick to it. The app doesn't delete anything on a timer, and **archiving a candidate isn't deleting them**. To delete:

- **Delete** on the candidate removes their record, call records and communications log from the app's database. It does **not** remove their rows in the text log (`training_sms_messages`), the email intake log, link-only applications, email replies, notifications or the opt-out list, nor their recordings and logs at ElevenLabs, Twilio and SendGrid.
- Ask Claude Code to build a proper erasure routine (all the app's collections, plus the ElevenLabs conversations) and a scheduled clean-up for your retention period before you collect real data at volume.
- Keep the opt-out list (`sms_optouts`): it's how you honour "stop texting me" in future.

**Subject access requests.** Candidates can ask for a copy of their data, and you normally have one month to respond. Their record in the app (the candidate drawer shows the CV, messages, transcripts and recordings) is the starting point; include the copies held by the services above where relevant.

**Security.** Give staff their own logins with the least access they need (**Settings → Team**: recruiters see only their offices), turn on two-factor sign-in on every provider account, and keep secrets in Railway only ([SECURITY.md](../SECURITY.md)).

## Olivia's voice and accent

- Pick a **British English voice**: browse the ElevenLabs **Voice Library**, filter by accent, add one to **My Voices**, then choose it in **Settings → Screen Call Agent → Voice** ([ELEVENLABS.md](ELEVENLABS.md#3-choosing-the-voice)).
- Keep **Language** as English and the English voice model.
- Write the script in British English: the AI copies the tone and spelling of its instructions. Use UK terms (mobile, postcode, right to work, Monday to Friday), £ for pay, and the 24-hour clock or am/pm consistently.
- Test with a few different accents among your team before going live, and listen to the first real calls in the candidate drawer.
