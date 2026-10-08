# Twilio: phone numbers, texts and the call line

Twilio is the phone company underneath CGRecruit. It carries every text message the app sends and receives, and the phone line Olivia's calls run on. In Claude Code, `/connect-twilio` walks you through this page and checks each step.

**What Twilio powers here**

| Feature | How it uses Twilio |
| --- | --- |
| Texts to candidates | Arrival texts, text screening, booking confirmations, reminders, the Inbox and replies from recruiters |
| Texts from candidates | Twilio forwards every reply to the app, which answers, books, reschedules or alerts a recruiter |
| Olivia's calls | Each office's number is the caller ID. Normally ElevenLabs places the call over your Twilio number ("managed" mode) |
| Calls to your office | Optional: the inbound agent answers your office number ([ELEVENLABS.md](ELEVENLABS.md#the-inbound-receptionist-optional)) |

Without Twilio the board, calendar, emails and web chat still work, but nothing is texted or phoned.

**Before you start:** check that **Settings → Auto-Dialer → Auto-dialer enabled** is **off** (it is on in a new install). Once Twilio and ElevenLabs are both connected, an applicant would be called within minutes. Switch it on when you go live ([SETUP.md step 9](SETUP.md#9-your-content-then-your-first-office)).

---

## 1. Account and keys

1. Sign up at [twilio.com](https://www.twilio.com/try-twilio).
2. **Upgrade the account** (add billing). A trial account can only text and call numbers you have verified, and it adds a trial notice to every message.
3. From the Console home page, copy the **Account SID** and **Auth Token**:
   - `TWILIO_ACCOUNT_SID`: the Account SID.
   - `TWILIO_AUTH_TOKEN`: the Auth Token. It is a secret. The app also uses it to check that webhooks really come from Twilio (see [Signature checks](#signature-checks)).
4. Put both in Railway → Variables and deploy. Don't put them in your local `backend/.env`: your computer shouldn't be able to text anyone.

Set a spend alert or limit under Billing while you're there.

## 2. Buying numbers

You need, as a minimum:

- **One number per office for calls.** It is the caller ID candidates see when Olivia rings, and the number they ring back.
- **A number that texts.** Each office can text from its own number, or all offices from one.

Buy them in Console → **Phone Numbers → Manage → Buy a number**. Tick the capabilities you need (**Voice** and **SMS**).

**Which number a text comes from.** Texts the app starts (arrival texts, reminders, the first-day confirmation and the rest) are sent from the first of these that is set; replies to a candidate's text go back from the number they texted:
1. `screen_call_agent.sms_sender_number`, one number for every office (it has no screen in Settings; ask Claude Code to set it);
2. the office's `sms_number` in `backend/company_profile.json`;
3. `TWILIO_PHONE_NUMBER` in Railway.

Only if none is set does the first-day confirmation fall back to the office's calling number. Calls always use the office's **Caller ID for outbound calls**. So in the UK, where a geographic calling number usually can't text, give every office a `+44 7…` `sms_number` (or set `TWILIO_PHONE_NUMBER`).

**In the UK**
- Texting needs a **UK mobile number (+44 7…)**: Twilio lists two-way SMS as supported in the UK ([UK SMS guidelines](https://www.twilio.com/en-us/guidelines/gb/sms)). UK geographic numbers (01…, 02…) are usually voice-only: check the number's SMS capability before you buy. Identity and address requirements for UK numbers: [Twilio's UK regulatory page](https://www.twilio.com/en-us/guidelines/gb/regulatory).
- Set `PHONE_DEFAULT_COUNTRY=GB` so candidates' numbers typed the UK way are stored as `+44…` ([UK.md](UK.md#phone-numbers)).
- A UK mobile number with Voice and SMS can serve one office for both calls and texts.
- Twilio asks for a **Regulatory Bundle** (your business details and proof of a UK address) before UK numbers are activated. Console shows exactly which documents it needs when you pick the number; approval can take a few days, so start early.
- More on UK rules (consent, opt-out, calling hours, GDPR): [UK.md](UK.md).

**In the US**
- A local (10-digit) number works for calls and texts, but texting from it to US mobiles requires **A2P 10DLC registration** (below). A toll-free number needs **toll-free verification** instead.

## 3. Registration for texting

Carriers filter unregistered business texting. Do this before you go live; it can take days to weeks.

**US: A2P 10DLC.** In Console → **Messaging → Regulatory Compliance**:
1. Register your **Brand** (your business).
2. Register a **Campaign** describing the messages: recruitment updates to people who applied for a job. Give real sample messages (copy them from **Settings → Applicant Comms**) and explain how people opt in (they gave their number when applying).
3. Twilio puts your numbers in a **Messaging Service** linked to the campaign.

The app adds an opt-out line ("Reply STOP to opt out.") to the first text each person receives, and it stops texting anyone who replies STOP.

**UK.** There's no 10DLC-style registration for person-to-person texting from a UK mobile number, but the Regulatory Bundle above is required, and carriers can still filter messages that look like spam. Keep messages expected, relevant and short.

## 4. Telling Twilio where to send replies

When a candidate texts back, Twilio has to know where to forward the message. For **every number that sends texts**:

1. Console → **Phone Numbers → Manage → Active numbers →** the number.
2. **Messaging Configuration → A message comes in:** Webhook, `https://<your app's address>/api/webhooks/twilio/inbound-sms`, **HTTP POST**.
3. Save.

**If the number belongs to a Messaging Service** (it will, after US 10DLC registration), the service's setting wins over the number's: Console → **Messaging → Services →** your service → **Integration → Incoming Messages → Send a webhook**, with the same address.

Use your final address (your domain, not the Railway one) and `https://`. If you ever change the app's address, change this webhook too.

## 5. Calls: what to point where

**You don't set a voice webhook by hand.** The app arranges it, depending on the office's calling mode.

**Managed mode (the default).** ElevenLabs places Olivia's calls over your Twilio number.
1. Settings → **Offices & Variants →** the office → **Caller ID for outbound calls:** pick the office's Twilio number. Save.
2. Click **Auto-detect from ElevenLabs.** For each office with a number, the app imports the number into ElevenLabs (using your Twilio SID and token), links it to the office's agent and stores the ElevenLabs phone-number id.
3. Importing lets ElevenLabs take over the number's voice settings in Twilio. Don't change the number's **Voice Configuration** in Twilio afterwards, or calls to and from it stop working.

The number must be **owned by your Twilio account** (bought or ported in).

**TwiML bridge (advanced).** The app places the call through Twilio itself and streams the audio to ElevenLabs over a WebSocket. Use it only if you need to call from a number Twilio doesn't own (a **verified caller ID**). There's no switch for it in the settings screens; ask Claude Code to set an office's calling mode to `twiml_bridge`. For each call, Twilio is given these addresses automatically:

| Address | What it's for |
| --- | --- |
| `/api/twiml/voice/<call id>` | Twilio fetches the call instructions when the candidate answers |
| `wss://<your address>/api/twiml/stream/<call id>/<signed token>` | The two-way audio stream between Twilio and the app. The token is signed with `JWT_SECRET`, made for that one call and expires after 5 minutes; the stream refuses any connection without a valid one |
| `/api/twiml/status/<call id>` | Twilio reports ringing, answered, completed |

Your host must accept WebSocket connections on the app's address (Railway does).

## Signature checks

Twilio signs every webhook it sends with your Auth Token (the `X-Twilio-Signature` header). The app checks that signature on incoming texts (`/api/webhooks/twilio/inbound-sms`), on the call instructions (`/api/twiml/voice/…`) and on call status updates (`/api/twiml/status/…`), and refuses anything that doesn't match (`403`).

The check **fails closed**: without `TWILIO_AUTH_TOKEN`, or if anything goes wrong while checking, the request is refused. (Only for a local run with no Twilio account at all can you set `ALLOW_UNSIGNED_TWILIO_WEBHOOKS=true`; never on the live app.)

The check is built from the address Twilio called, query string included, so:
- The webhook address in Twilio must match the app's real public address exactly (same domain, `https://`). Set `APP_PUBLIC_URL` to that address: the app checks against it first, which keeps working behind Railway's proxy.
- If you rotate the Auth Token in Twilio, update `TWILIO_AUTH_TOKEN` in Railway and deploy straight away, or every incoming text is refused.

## 6. Phone-number format

The app stores every number in international format (`+44 7700 900123`, `+1 617 555 0123`). Which way it reads a number typed without a country code depends on **`PHONE_DEFAULT_COUNTRY`**:
- **UK offices:** set `PHONE_DEFAULT_COUNTRY=GB` in Railway. Numbers typed the UK way (`07700 900123`, `447700900123`, `7700 900123`) are then stored as `+44…`.
- **US offices:** leave it unset (the default is `US`); a 10-digit number becomes `+1…`.

Set it before your first candidates arrive, so every number is stored the right way from the start ([UK.md](UK.md#phone-numbers)).

## 7. Testing safely

Nothing here should reach a real candidate.

1. **Your phone only.** Use your own mobile as the test candidate. Never test with a number from your candidate list.
2. **Inbound first (costs nothing to anyone else).** Text your Twilio number from your phone. In the app, the message appears in the **Inbox** (an unknown number shows as unmatched). In Twilio → Monitor → Logs → Messaging, the message shows the webhook answered `200`.
3. **A full journey on yourself.** Create a candidate with your own name and number (Add Applicant → Manual Entry) on an office whose first contact is **Chat first**. You should get the arrival text; reply to it and the screening runs. Delete the candidate afterwards.
4. **Olivia's call.** Use the test-call button in **Settings → Office AI Voice & Prompts**, which rings the number you type (yours) and creates no candidate. See [ELEVENLABS.md](ELEVENLABS.md#testing-safely).

`/connect-twilio` runs the read-only checks for you (numbers, capabilities, webhook addresses) and only sends a text or places a call after you say yes and give it your own number.

## When it doesn't work

| Symptom | Likely cause |
| --- | --- |
| Replies never reach the app | Webhook missing or on the wrong address; or the number is in a Messaging Service whose incoming setting points elsewhere |
| Twilio logs show `403` on the webhook | Signature mismatch or no token: `TWILIO_AUTH_TOKEN` missing or wrong, or the webhook address doesn't match `APP_PUBLIC_URL` |
| Twilio logs show `11200` / timeouts | The app was down or redeploying; Twilio retries, and the app ignores duplicates |
| Texts to UK mobiles fail | Sending from a UK geographic or US number; use a UK mobile (+44 7…) number |
| Texts to US mobiles are filtered (`30007`, `30034`) | A2P 10DLC registration missing or not yet approved |
| "Not a Twilio-owned number" when assigning | Buy or port the number, or use TwiML bridge mode with a verified caller ID |
| Calls fail after you edited the number in Twilio | The voice settings ElevenLabs set were changed: run **Auto-detect from ElevenLabs** again |
| Nothing is sent and the log says no Twilio number is configured, or the first-day confirmation fails with "No texting number for this office" | The office has no texting number: set its `sms_number` in `backend/company_profile.json` or `TWILIO_PHONE_NUMBER` |

## Costs

Twilio charges per text segment, per call minute and per number per month, and prices differ by country. See [twilio.com/en-us/pricing](https://www.twilio.com/en-us/pricing), and the country pages for [SMS](https://www.twilio.com/en-us/sms/pricing/gb) and [voice](https://www.twilio.com/en-us/voice/pricing/gb). Olivia's call minutes are also billed by ElevenLabs.

Texts written only with plain characters (no emoji, curly quotes or long dashes) fit 160 characters per segment; one unusual character drops that to 70 and multiplies the cost of every message. Keep templates plain.
