# ElevenLabs: Olivia's voice

ElevenLabs runs **Olivia** on the phone: an AI voice agent that speaks, listens, asks your screening questions, checks your real interview slots and books one, all in the same call. In Claude Code, `/connect-elevenlabs` walks you through this page and ends with a test call to **your own** phone.

Set up Twilio first ([TWILIO.md](TWILIO.md)): Olivia's calls run over your Twilio numbers.

**What ElevenLabs powers here**

| Agent | What it does | Needed? |
| --- | --- | --- |
| **Screening agent** (one per office) | Calls new applicants, screens, books an interview | Yes, for voice screening |
| **Browser test widget** | Talk to the screening agent from **Settings → Screen Call Agent**, with no phone call | Comes with the screening agent |
| **Inbound receptionist** (one per office) | Answers calls to the office number: recognises booked candidates, gives directions, reschedules, can register and screen a new caller | Optional |
| **No-show revival agent** (one per office) | A courtesy call to people who missed an interview, offering a new time | Optional, off by default |

Recordings and transcripts stay in ElevenLabs; the app copies the transcript and a summary onto the candidate and plays the recording from ElevenLabs when a recruiter opens it.

If you only want text screening, you can skip ElevenLabs: set **Settings → Screen Call Agent → How candidates are first contacted** to **Chat only**.

---

## 1. Account and API key

1. Sign up at [elevenlabs.io](https://elevenlabs.io). Choose a plan with enough **agent minutes** and **concurrent calls** for your volume ([pricing](https://elevenlabs.io/pricing)).
2. Profile → **API Keys → Create**. If you restrict the key, give it access to **ElevenLabs Agents** (read and write) and **Voices** (read).
3. Put it in Railway → Variables as `ELEVENLABS_API_KEY` and deploy. It's a secret. Leave it out of your local `backend/.env`.
4. In the same place, add **`ELEVENLABS_TOOL_SECRET`**: a long random string you generate yourself (`python3 -c "import secrets; print(secrets.token_urlsafe(32))"` in your own terminal). Set it **before** the first sync below: the sync gives it to ElevenLabs so Olivia's tools can prove they're hers (see [Webhook security](#webhook-security)).
5. Tell the app your plan's limit on simultaneous calls: **Settings → Auto-Dialer → Save tenant ceiling** (all offices together), and **Max concurrent calls (this pipeline)** per office. Calls above the limit wait their turn instead of failing.

## 2. The screening agent

The app writes the agent's prompt, first message, voice, audio format, tools and webhook for you. You only create an empty agent once and tell the app its id.

1. ElevenLabs → **Agents → Create agent → Blank**. Name it after your company (for example "Olivia — default").
2. Copy its **Agent ID** (in the agent's settings or the address bar, it starts `agent_`).
3. In CGRecruit, while editing the **global default** (before you create any office it's the only scope; once offices exist, click **Edit Global default** in the banner at the top of the section): **Settings → Screen Call Agent → Advanced — provider IDs (auto-managed) → Agent ID**, paste it, and **Save**.

Saving pushes everything to ElevenLabs (a "sync"). The sync:
- writes Olivia's **system prompt** from your Purpose, screening questions, booking instructions, closing message and Additional Context;
- sets her **first message**, **language**, **voice**, **voice model** and **LLM**;
- sets the audio format to μ-law 8 kHz (what phone lines carry);
- turns on **voicemail detection** with your voicemail message, and the **end call** tool;
- creates or updates the **booking tools** (below) and attaches them;
- points the agent's **post-call webhook** at the app;
- switches on **agent authentication**, so a conversation can only be started by the app (phone calls, and the owner's test widget with a short-lived signed link). Set `ELEVENLABS_AGENT_AUTH=false` only to debug.

If the sync fails you'll see "Saved — but ElevenLabs sync failed" with the reason. Fix it and save again.

**The app owns the agents.** Every save, and every server start (each deploy or restart), re-syncs all agents from the app's Settings. Changes made directly in ElevenLabs to a synced field (prompt, first message, voice, LLM, tools, webhook) are overwritten, so make them in the app. When you bring in an update from upstream that changes the prompt builder, Olivia's wording changes on that deploy: make a test call afterwards. A restart also drops calls in progress; check **Call Queue** before deploying.

**One agent per office.** When you create an office in **Settings → Offices & Variants → New pipeline** with the API key and the default agent id already set, the app creates a dedicated agent for it ("Olivia — <office>"), copied from the default one. That's why [SETUP.md step 9](SETUP.md#9-your-content-then-your-first-office) sets up the default agent before the first office. **An office created earlier** has no agent of its own and shares the default agent, so its own voice and prompt changes can't be kept separate from other offices. To give it one, create another blank agent in ElevenLabs and paste its id into that office's **ElevenLabs Agent ID Override (advanced)** in **Settings → Offices & Variants**, then save; the app syncs the office's settings to it. Each office can then have its own name for Olivia, opening line, voice and local knowledge (**Settings → Office AI Voice & Prompts**). Saving an office re-syncs that office's agent only.

**Connect the office's number.** Settings → **Offices & Variants →** pick the office's Twilio number as **Caller ID for outbound calls**, save, then **Auto-detect from ElevenLabs**. The app imports the number into ElevenLabs and links it to the office's agent ([TWILIO.md](TWILIO.md#5-calls-what-to-point-where)).

You can review the exact prompt before it goes: the screen's preview shows what will be synced.

### What the script can use

At the start of each screening call the app sends these values, and only these: `{{first_name}}`, `{{full_name}}`, `{{role}}` (the title of the job ad the candidate applied to; empty if they have none), `{{company}}`, `{{city}}` (the office name up to the first comma), `{{agent_name}}`, `{{phone}}`, `{{email}}`, `{{pipeline_slug}}`, plus `{{previous_context}}` and `{{is_dnd_retry}}`, which the app's own prompt uses for repeat calls.

Nothing else is known when the call starts, including the last name, an interview date or time, or a meeting link. The interview is booked during the call, so Olivia reads its day and time back from the slot the candidate chose (the default closing message tells her to). Don't put any other `{{…}}` in the Purpose, opening message, questions, booking instructions or closing message, and don't use the square-bracket forms `[Last Name]`, `[Date]`, `[Time]`, `[Zoom Link]` or `[Call Number]` there either: the app turns them into `{{last_name}}`, `{{date}}` and so on, which are never sent. Square brackets in **Applicant Comms** templates are a different system and are fine there ([BRANDING.md](BRANDING.md#2-in-the-app-settings)).

## 3. Choosing the voice

- **Settings → Screen Call Agent → Voice & AI → Voice** lists the voices in your ElevenLabs account; click ▶ to hear one and click the card to choose it.
- To use a voice from the ElevenLabs **Voice Library** (for example a British accent), add it to **My Voices** in ElevenLabs first; it then appears in the list.
- **Voice Model:** the English models are fastest. Use a multilingual model only when the agent's **Language** isn't English.
- **LLM Model** decides how Olivia thinks between turns. Faster models keep the pauses short, which matters more on the phone than cleverness.
- Each office can override the voice and name in **Settings → Office AI Voice & Prompts**.

Tell candidates up front that Olivia is an AI assistant. The default arrival message already does, and the default prompt tells her to answer honestly if asked.

## 4. The booking tools (what the agent calls back on)

During a call, the agent calls the app over the internet to read slots and book them. The sync creates these as **workspace tools** in ElevenLabs and attaches them to every agent. The app fills in `phone` and `pipeline_slug` (the office) from the call's details, so the AI only supplies the slot or date.

| Tool | Method and address | What it does |
| --- | --- | --- |
| `get_available_slots` | `GET /api/public/availability/{pipeline_slug}` | Lists the office's next interview slots |
| `book_slot` | `POST /api/public/book-by-phone` | Books the slot the candidate agreed (`phone`, `slot_iso`, `pipeline_slug`) |
| `send_booking_link` | `POST /api/public/send-booking-link` | Texts the candidate their own booking page |
| `reschedule_start_date` | `POST /api/public/reschedule-start-by-phone` | Moves a new starter's start date (`new_start_date`) |
| `register_caller` | `POST /api/public/register-caller` | Adds an unknown inbound caller as a candidate so they can be screened |
| `send_office_details` | `POST /api/public/send-office-details` | Texts the office address and map link |

All addresses start with your `APP_PUBLIC_URL`, so set that to your final domain **before** syncing. If you change the address later, save the Screen Call Agent settings again (and re-sync the inbound and revival agents) so the tools follow.

**How they're protected.** These tools act on nothing more than a phone number and an office, so every one that books, texts or creates something (`book_slot`, `send_booking_link`, `reschedule_start_date`, `register_caller`, `send_office_details`) only answers requests carrying the `X-CGR-Tool-Secret` header with your `ELEVENLABS_TOOL_SECRET`. The sync adds that header to every tool definition in ElevenLabs. Without the setting the tools are refused (the agent is told the action didn't happen). `get_available_slots` stays open because candidates' own booking pages use it too; it only lists free times.

If you change `ELEVENLABS_TOOL_SECRET`, save the Screen Call Agent settings again and re-sync the inbound and revival agents, so ElevenLabs gets the new value.

## 5. Webhooks: after the call, and at the start of inbound calls

| Webhook | Address | Set by |
| --- | --- | --- |
| **Post-call** | `POST /api/webhooks/elevenlabs/post-call` | You, once: ElevenLabs → Agents → **Settings → Webhooks**, add a post-call webhook to this address with **HMAC** authentication, copy its secret into `ELEVENLABS_WEBHOOK_SECRET`. (The sync also points each agent at the address.) |
| **Conversation initiation** | `POST /api/webhooks/elevenlabs/conversation-init` | Set automatically at workspace level when you sync the inbound agent, including the `X-CGR-Tool-Secret` header. Only inbound agents use it |

After each call the post-call webhook brings the transcript into the app; Claude writes a summary and a verdict; the candidate moves on (booked, slot-picker link sent, retry scheduled, or rejected on a must-have). **If the webhook is missed, nothing is lost:** every minute the app looks for finished calls that still have no transcript and fetches them from ElevenLabs itself, so a missed webhook only means a short delay.

### Webhook security

| Address | How the app knows it's ElevenLabs | Without the setting |
| --- | --- | --- |
| Booking tools (above) | `X-CGR-Tool-Secret` header = `ELEVENLABS_TOOL_SECRET` | Refused (503) |
| Conversation initiation | Same header (the sync sets it on the workspace webhook) | Refused: the inbound agent answers without knowing the caller |
| Post-call | `ElevenLabs-Signature` HMAC, checked with `ELEVENLABS_WEBHOOK_SECRET`; requests older than 30 minutes are refused | Refused. Outbound screening and revival calls are still recorded by the 60-second sweep, which reads finished calls back from ElevenLabs with your API key. **Inbound** calls are only recorded through this webhook, so set the secret if you use the receptionist |

A forged post-call request could otherwise write a verdict, reject a candidate and send their rejection text, which is why it is never accepted unsigned.

**Who a call was with comes from the app, never from the conversation.** Each conversation carries "dynamic variables" (name, phone number, office), and the booking tools act on the phone number in them. Whoever starts a browser or widget conversation chooses those values, and ElevenLabs signs the post-call webhook and adds the tool secret regardless, so the signature proves the request came from ElevenLabs, not who was speaking. The app therefore:

- keeps **agent authentication** on (above), so browser and widget conversations need a signed link that only the app mints. The settings test widget gets one from an owner-only endpoint (`POST /api/elevenlabs/agent/test-session`);
- has no public route that starts a voice session;
- identifies an outbound call by the conversation record it created when it dialled, and an inbound call by its own record of the conversation-initiation webhook, matched on the Twilio call SID and the caller's number from ElevenLabs' phone-call metadata. A finished conversation that isn't a phone call the app knows about is logged and ignored. If the initiation record is missing, the call is rebuilt from the numbers ElevenLabs reports and handled as a front-desk call (a notification), never as a screening.

The one thing this can't stop is a caller who fakes their caller ID on the phone network. Treat an inbound "please cancel" with the same care you would on any phone line.

## The inbound receptionist (optional)

**Settings → Inbound Call Agent → Create & sync inbound agent.** For the selected office (or all, as the owner), the app:
- creates a receptionist agent with your office address and map link (from `backend/company_profile.json`) and your booking tools;
- sets the workspace **conversation-initiation webhook**, so at the start of each call ElevenLabs asks the app who's calling and the agent can greet booked candidates by name;
- assigns it to the office's ElevenLabs phone number, so it answers **inbound** calls. Outbound screening calls are unaffected (each passes its own agent).

A caller who hasn't been screened yet is switched into the screening conversation; a booked one gets front-desk help; a stranger can be registered and screened. Anything that needs a person raises a notification.

## No-show revival (optional)

**Settings → No-Show Revival:** switch on **Revival calling enabled**, set the waiting days, attempts, gap, lookback and the daily call cap, write the opener and any extra instructions, then **Create & sync revival agent**. It ships switched off.

## Testing safely

Never test on a real candidate. Use your own phone.

1. **Test call.** **Settings → Office AI Voice & Prompts →** the office → **Place a test call.** Enter **your own** number in international format and a name to be called. It uses that office's real agent, voice, prompt and booking tools, and it creates no candidate. Booking works only for a number that belongs to a candidate, so to test booking end to end, first add yourself as a candidate (Add Applicant → Manual Entry, your own number), then delete yourself afterwards.
2. **In the browser.** **Settings → Screen Call Agent** has a test widget that talks to the agent through your computer's microphone, with made-up candidate details. It loads ElevenLabs' widget script from the internet and connects with a short-lived signed link the app asks ElevenLabs for, so it's available to the owner only.
3. **Inbound.** After syncing the inbound agent, ring the office number from your phone.
4. **Check the result.** ElevenLabs → Agents → the agent → **Conversations** shows the transcript and any tool calls (look for `book_slot` returning success). In the app, the candidate drawer shows the summary and recording within a minute or two of hanging up.

`/connect-elevenlabs` does the read-only checks for you (key works, agents exist, tools attached, addresses match your domain, numbers linked) and only places a test call after you say yes, to the number you give it.

## When it doesn't work

| Symptom | Likely cause |
| --- | --- |
| "Agent or phone number not configured for this pipeline" | The office has no agent or no ElevenLabs phone-number id: run **Auto-detect from ElevenLabs** in Offices & Variants |
| "ElevenLabs sync failed" when saving | Wrong or restricted `ELEVENLABS_API_KEY`, or a voice id that isn't in your account |
| Olivia says she can't find slots | No availability for that office (**Calendar → Manage availability**), or the tool addresses point at an old domain: save Screen Call Agent again |
| Calls connect then hang up straight away | A `{{variable}}` in your prompt or first message that the app doesn't send (see [What the script can use](#what-the-script-can-use)) |
| Calls are queued but never placed | Outside the call window or days (**Settings → Auto-Dialer**), the auto-dialer is off, or the concurrent-call limit is reached |
| No transcript or summary after a call | Give it a minute (the app fetches missed ones itself). Still nothing: check `ELEVENLABS_API_KEY`, the post-call webhook address and `ELEVENLABS_WEBHOOK_SECRET` (ElevenLabs' webhook log shows `403` for a wrong secret, `503` for none) |
| Olivia says she couldn't book or text, every time | `ELEVENLABS_TOOL_SECRET` missing on the server, or changed since the last sync: set it and save Screen Call Agent again |
| Long silences | Choose a faster LLM and the English voice model; keep the prompt and Additional Context tight |

## Costs

ElevenLabs bills agent minutes by plan, with limits on concurrent calls ([elevenlabs.io/pricing](https://elevenlabs.io/pricing)). The phone side of each call is billed separately by Twilio. Voicemail detection ends calls that reach an answering machine quickly, and the call window and retry limits in **Settings → Auto-Dialer** cap how many attempts each candidate gets.
