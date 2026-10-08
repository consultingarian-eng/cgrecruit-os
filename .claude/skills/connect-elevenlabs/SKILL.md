---
name: connect-elevenlabs
description: Set up Olivia's voice on the owner's live CGRecruit with ElevenLabs - API key in Railway, the default screening agent and its id, per-office agents, choosing a voice (including British voices), importing and linking each office's Twilio number, the booking tools and post-call webhook, the optional inbound receptionist and no-show revival agents, then a read-only check and a test call to the owner's own phone only with their explicit yes. Use when they ask about Olivia, AI calls, voice agents, voices or accents, ElevenLabs, test calls, booking during calls, or missing transcripts.
argument-hint: "[optional: 'check' to just run the checks]"
---

# Connect ElevenLabs (Olivia's voice)

ElevenLabs runs Olivia on the phone: she screens, checks real slots and books, in one call. The source of truth is `docs/ELEVENLABS.md`: read it now and follow its order. If this skill and the guide disagree, trust the guide (and the code over both: `backend/voice_service.py`, `backend/routes/webhooks.py`, `backend/routes/inbound.py`).

Before starting, check: **Settings → Auto-Dialer → Auto-dialer enabled** is **off** (it is on in a new install, and with Twilio and ElevenLabs connected it would call real applicants), the app is live on its final `https://` address (`curl https://<their address>/api/`), `APP_PUBLIC_URL` is that address (the booking tools and webhooks are built from it), and `/connect-twilio` is done (each office has a Twilio-owned Voice number).

If they passed `check` (`$ARGUMENTS`), skip to **Check it**.

## Rules

- **Never place a call** (test call, batch dial, "call now", revival) unless the owner has said yes to that specific call and given **their own** number. Never a candidate's number.
- **Never print the API key.** The checker runs under `railway run`; it prints only whether the key is set.
- **The checker is read-only.** Changes to agents happen through the app (saving settings, the sync buttons), which the owner clicks, so they see what's being pushed.
- Keep `ELEVENLABS_API_KEY` out of the local `backend/.env`.

## Steps (teach one at a time, check each)

1. **Plan and key.** They choose a plan with enough agent minutes and concurrent calls (link `elevenlabs.io/pricing`; no figures from you), create an API key (if restricted: Agents read/write, Voices read), and paste it into Railway as `ELEVENLABS_API_KEY`. In the same place they add `ELEVENLABS_TOOL_SECRET`, a random string they generate in **their own** terminal (`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`); never shown to you. Then Deploy. Without the tool secret, Olivia's booking/texting tools and the inbound start-of-call webhook are refused by the app.
2. **Concurrency.** In the app, **Settings → Auto-Dialer**: set the tenant ceiling to their plan's concurrent-call limit (**Save tenant ceiling**), and the per-office maximum.
3. **The default agent.** In ElevenLabs: Agents → Create agent → Blank. They copy its Agent ID (`agent_…`). In the app, editing the **global default** (the only scope before any office exists; otherwise the **Edit Global default** button in the section's banner): **Settings → Screen Call Agent → Advanced — provider IDs (auto-managed) → Agent ID**, paste, **Save**. The app owns the agents from then on: every save and every server restart re-syncs them, overwriting edits made directly in ElevenLabs. Saving syncs prompt, first message, voice, LLM, audio format, voicemail detection, the booking tools and the post-call webhook. A "Saved — but ElevenLabs sync failed" toast gives the reason; fix and save again.
4. **Voice.** Settings → Screen Call Agent → Voice & AI. For a British accent: ElevenLabs Voice Library → filter by accent → add to My Voices → it appears in the picker. Keep Language English and an English voice model; pick a fast LLM. Save.
5. **Script.** If `/brand` hasn't been run, offer it now: Olivia's name, opening line (which should say she's an AI assistant and the call is recorded), questions and Additional Context must be theirs before any real call.
6. **Per-office agents and numbers.** Offices created after the key and default agent id were set already have their own agent ("Olivia — <office>"). An office created before that shares the default agent: to give it its own, they create another blank agent in ElevenLabs and paste its id into that office's **ElevenLabs Agent ID Override (advanced)** (Settings → Offices & Variants), then save. In **Settings → Offices & Variants**: each office's **Caller ID for outbound calls** set, then **Auto-detect from ElevenLabs**, which imports each number into ElevenLabs and links it to the office's agent. Per-office name, opening, context and voice: **Settings → Office AI Voice & Prompts**.
7. **Post-call webhook.** In ElevenLabs → Agents → Settings → Webhooks they add a post-call webhook to `https://<their address>/api/webhooks/elevenlabs/post-call` with **HMAC** authentication, and paste its secret into Railway as `ELEVENLABS_WEBHOOK_SECRET` themselves, then Deploy. The app refuses unsigned post-call requests. Explain the app also fetches finished outbound calls every minute, so the transcript still arrives without it; inbound receptionist calls need the webhook.
8. **Optional agents.** Inbound receptionist: **Settings → Inbound Call Agent → Create & sync inbound agent** (needs each office's address in `backend/company_profile.json`). No-show revival: **Settings → No-Show Revival**, switch on, set limits, **Create & sync revival agent**. Only if they want them.

## Check it

From the repo root, with their Railway project linked:

```
railway run python3 .claude/skills/connect-elevenlabs/check_elevenlabs.py
```

Explain the output plainly: all six booking tools present and pointing at their address; each agent has a voice, tools attached, voicemail detection on and the post-call webhook on their address; each imported number assigned to the right office's agent; the inbound start-of-call webhook (only matters with the inbound receptionist). Fix anything flagged by saving the relevant settings again (the tools and webhooks follow `APP_PUBLIC_URL` at sync time) and re-run.

## The test call (only with their explicit yes)

1. Ask: "Shall I walk you through a test call to your own mobile?" Proceed only on yes.
2. They open **Settings → Office AI Voice & Prompts →** the office → **Place a test call**, enter **their own** number in international format (`+44…`) and the name to be called, and press the button themselves. It uses the office's real agent, voice, prompt and tools and creates no candidate.
3. Booking only works for a number that belongs to a candidate. To test booking end to end, they first add themselves as a candidate (Add Applicant → Manual Entry, their own mobile), make the test call, book a slot, then cancel it and delete themselves afterwards.
4. Check together: ElevenLabs → the agent → Conversations shows the transcript and tool calls (`get_available_slots`, `book_slot` succeeding); if they were a candidate, the candidate drawer shows the summary and recording within a minute or two.
5. Ask how it sounded: pace, pauses, accent, anything she got wrong. Adjust the script, voice or LLM and repeat if they want.

## Common problems

See "When it doesn't work" in `docs/ELEVENLABS.md`. The usual ones: no ElevenLabs phone-number id on an office (run Auto-detect); tools pointing at an old address (save Screen Call Agent again after changing `APP_PUBLIC_URL`); a `{{variable}}` in the script the app doesn't send (calls connect then drop; the list is under "What the script can use" in `docs/ELEVENLABS.md`); no availability for the office; calls waiting because of the call window or the concurrency limit.

## When you finish

Summarise: which agents exist per office, the voice, which numbers are linked, whether the inbound and revival agents are on, the test-call result, and next steps (`/brand` if not done; `docs/UK.md` before a UK go-live; switching **Auto-dialer enabled** back on in **Settings → Auto-Dialer**, for each office, only when they're ready for real candidates).
