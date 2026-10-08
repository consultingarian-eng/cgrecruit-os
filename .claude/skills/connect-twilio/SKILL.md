---
name: connect-twilio
description: Connect the owner's Twilio account to their live CGRecruit - account and keys in Railway, buying UK or US numbers, the regulatory bundle or A2P 10DLC registration, pointing each texting number's incoming-message webhook at the app, linking each office's calling number, and checking it all with a read-only script. Never sends a text or places a call without the owner's explicit yes, and then only to their own phone. Use when they ask about phone numbers, texting, SMS, Twilio, the inbound SMS webhook, caller ID, or Twilio errors.
argument-hint: "[optional: 'check' to just run the checks]"
---

# Connect Twilio

Twilio carries every text the app sends and receives, and the phone line Olivia's calls run on. The source of truth is `docs/TWILIO.md`: read it now and follow its order. If this skill and the guide disagree, trust the guide (and the code over both).

`/setup` should be finished first: the app must be live on its final `https://` address, with `APP_PUBLIC_URL` set to it. Check with `curl https://<their address>/api/` before you start. Also check **Settings → Auto-Dialer → Auto-dialer enabled** is **off** until go-live (it is on in a new install; with Twilio and ElevenLabs connected it calls real applicants).

If they passed `check` (`$ARGUMENTS`), skip to **Check it**.

## Rules

- **Never send a text or place a call yourself**, and never ask the app to, unless the owner has said yes to that specific test and given **their own** number. No candidate numbers, ever.
- **Never print secrets.** The checker runs with `railway run`, which injects Railway's variables into one command without showing them. Don't run `railway variables` without `--kv | cut -d= -f1` (names only). The Account SID may be shown masked to 4 characters; the Auth Token never.
- **The only write the checker can make** is `--set-sms-webhook <number>`, which points one number's incoming texts at the app. Run it only after the owner agrees, one number at a time.
- **Don't touch a number's Voice Configuration** once it's been imported into ElevenLabs.
- Keep `TWILIO_*` out of the local `backend/.env`.

## Steps (teach one at a time, check each)

1. **Account and keys.** They sign up, upgrade from trial, and paste `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` into Railway → Variables themselves, then Deploy. Explain that the token also verifies Twilio's webhooks. Check: the checker's "Account" line shows `status=active type=Full`.
2. **Where they are.** Ask which country and how many offices. UK: texting needs **+44 7** mobile numbers, and a **Regulatory Bundle**; start it now. US: local numbers need **A2P 10DLC** (or toll-free verification) before texting at volume.
3. **Buy numbers.** At least one Voice+SMS number per office (the office's caller ID); decide whether each office texts from its own number or one shared number. They buy in Console; you don't need their card or login.
4. **Texting numbers in the company profile.** Put each office's texting number in `sms_number` for that office in `backend/company_profile.json` (or a single fallback in `TWILIO_PHONE_NUMBER` in Railway). Commit and push the profile change after showing them the diff.
5. **Incoming texts.** For every texting number: Console → the number → Messaging Configuration → A message comes in → Webhook `https://<their address>/api/webhooks/twilio/inbound-sms`, HTTP POST. Offer to do it with `--set-sms-webhook` instead, with their OK. If the number is in a **Messaging Service** (it will be after 10DLC), the service's Integration → Incoming Messages setting wins: set "Send a webhook" there with the same address.
6. **Calling numbers.** In the app, Settings → **Offices & Variants** → each office → **Caller ID for outbound calls** → its number → Save. Importing into ElevenLabs happens in `/connect-elevenlabs` (**Auto-detect from ElevenLabs**); offer to continue there.
7. **Registration.** Walk through the 10DLC Brand and Campaign (US) with sample messages copied from **Settings → Applicant Comms**, or confirm the UK bundle is approved. This can take days; the rest can proceed meanwhile, but don't go live on texting until it's approved.
8. **Phone-number format (UK).** Confirm `PHONE_DEFAULT_COUNTRY=GB` is set in Railway → Variables and deployed (it makes numbers typed as `07…` store as `+44…`; US offices leave it unset). Check: they add themselves as a test candidate with their own `07…` mobile, and the candidate's number shows as `+44…` (`docs/UK.md#phone-numbers`).

## Check it

Run, from the repo root, with their Railway project linked (`railway link` if needed):

```
railway run python3 .claude/skills/connect-twilio/check_twilio.py
```

Read the output to them in plain words: account type, each number's capabilities, where its incoming texts go, any Messaging Service override, and which numbers still don't point at the app. Fix and re-run until every texting number reports `OK`.

Then the **inbound test**, which sends nothing from the app: ask them to text any word from **their own phone** to one of their numbers. Check:
- In the app, **Inbox** shows the message (an unknown number appears as unmatched).
- In Twilio → Monitor → Logs → Messaging, the incoming message shows the webhook returned `200`. A `403` means a signature mismatch: `TWILIO_AUTH_TOKEN` doesn't match, or the webhook address isn't exactly the app's public address.

**Outbound test, only with their explicit yes:** they add themselves as a candidate (Add Applicant → Manual Entry, their own name and mobile) on an office set to **Chat first** in Settings → Screen Call Agent. The arrival text should reach their phone; they reply and the screening continues. Afterwards they delete that candidate.

## Common problems

See the table in `docs/TWILIO.md` ("When it doesn't work"). The usual ones: the webhook on the number while a Messaging Service overrides it; a UK landline number used for texting; 10DLC not approved yet (US carrier errors 30007/30034); a rotated Auth Token not updated in Railway.

## When you finish

Summarise: numbers per office and their roles (calls, texts), where incoming texts go, registration status, what's left (for example "UK bundle pending"), and next: `/connect-elevenlabs`.
