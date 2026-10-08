#!/usr/bin/env python3
"""Read-only check of the ElevenLabs side of CGRecruit. Places no calls and
changes nothing.

Run it with the live app's variables injected, so no secret is typed or shown:

    railway run python3 .claude/skills/connect-elevenlabs/check_elevenlabs.py

(or export ELEVENLABS_API_KEY and APP_PUBLIC_URL yourself).

It reports: whether the key works, every agent (voice set? tools attached?
voicemail detection? post-call webhook pointing at the app?), the workspace
booking tools and whether their addresses match APP_PUBLIC_URL, imported phone
numbers and the agent each is assigned to, and the workspace
conversation-initiation webhook used by inbound agents. The API key is never
printed. Standard library only.
"""
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.elevenlabs.io"
TOOLS = ["get_available_slots", "book_slot", "send_booking_link",
         "reschedule_start_date", "register_caller", "send_office_details"]


def env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def get(path: str, key: str):
    req = urllib.request.Request(API + path, headers={"xi-api-key": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or "null")


def main() -> int:
    key = env("ELEVENLABS_API_KEY")
    public = env("APP_PUBLIC_URL").rstrip("/")
    print(f"ELEVENLABS_API_KEY: {'set' if key else '(not set)'}")
    print(f"APP_PUBLIC_URL:     {public or '(not set)'}")
    # Presence only: the values are never printed.
    for name in ("ELEVENLABS_TOOL_SECRET", "ELEVENLABS_WEBHOOK_SECRET"):
        print(f"{name}: {'set' if env(name) else '(not set: see docs/ELEVENLABS.md#webhook-security)'}")
    if not key:
        print("\nElevenLabs isn't configured in this environment. Nothing to check.")
        return 1
    if not public.startswith("https://"):
        print("\nWARNING: APP_PUBLIC_URL should be the app's final https:// address before you sync.")
    problems = []

    # Tools
    try:
        tools = (get("/v1/convai/tools", key) or {}).get("tools") or []
    except urllib.error.HTTPError as e:
        print(f"\nElevenLabs refused the key ({e.code}). Check it, and that it can read Agents.")
        return 1
    by_name = {}
    for t in tools:
        cfg = t.get("tool_config") or {}
        schema = cfg.get("api_schema") or {}
        has_secret_header = any(
            str(h).lower() == "x-cgr-tool-secret" for h in (schema.get("request_headers") or {})
        )
        by_name[cfg.get("name")] = (t.get("id") or t.get("tool_id"), (schema.get("url") or ""), has_secret_header)
    print("\nBooking tools (workspace):")
    for name in TOOLS:
        if name not in by_name:
            print(f"- {name}: missing (created when you save Settings -> Screen Call Agent)")
            problems.append(f"tool {name} missing")
            continue
        url, has_header = by_name[name][1], by_name[name][2]
        ok = bool(public) and url.startswith(public + "/api/")
        print(f"- {name}: {url} {'OK' if ok else '<- not your APP_PUBLIC_URL'}"
              f" · tool secret header: {'yes' if has_header else 'MISSING'}")
        if not ok:
            problems.append(f"tool {name} points elsewhere")
        if not has_header and name != "get_available_slots":
            problems.append(f"tool {name} has no X-CGR-Tool-Secret header (set ELEVENLABS_TOOL_SECRET, then save Screen Call Agent)")
    tool_ids = {v[0] for k, v in by_name.items() if k in TOOLS}

    # Agents
    agents, cursor = [], None
    while True:
        q = "/v1/convai/agents?page_size=100" + (f"&cursor={cursor}" if cursor else "")
        page = get(q, key) or {}
        agents += page.get("agents") or []
        cursor = page.get("next_cursor")
        if not page.get("has_more") or not cursor:
            break
    print(f"\nAgents ({len(agents)}):")
    post_call = f"{public}/api/webhooks/elevenlabs/post-call" if public else ""
    for a in agents:
        aid = a.get("agent_id")
        try:
            d = get(f"/v1/convai/agents/{aid}", key) or {}
        except urllib.error.HTTPError:
            print(f"- {a.get('name')} ({aid}): couldn't read")
            continue
        conv = d.get("conversation_config") or {}
        prompt = (conv.get("agent") or {}).get("prompt") or {}
        attached = set(prompt.get("tool_ids") or [])
        built_in = prompt.get("built_in_tools") or {}
        voice = (conv.get("tts") or {}).get("voice_id")
        hook = ((d.get("platform_settings") or {}).get("webhook") or {}).get("url") or ""
        auth_on = bool(((d.get("platform_settings") or {}).get("auth") or {}).get("enable_auth"))
        has_tools = bool(attached & tool_ids)
        print(f"- {a.get('name')} ({aid})")
        print(f"    voice: {'set' if voice else 'not set'} · booking tools: {'attached' if has_tools else 'none'}"
              f" · voicemail detection: {'on' if built_in.get('voicemail_detection') is not None else 'off'}"
              f" · authentication: {'on' if auth_on else 'OFF'}")
        if not auth_on:
            problems.append(f"agent {a.get('name')} accepts browser conversations without a signed link"
                            " (save Settings -> Screen Call Agent to re-sync, unless ELEVENLABS_AGENT_AUTH=false)")
        if hook:
            print(f"    post-call webhook: {hook} {'OK' if hook == post_call else '<- not this app'}")
        if not has_tools:
            problems.append(f"agent {a.get('name')} has no booking tools")

    # Phone numbers
    try:
        nums = get("/v1/convai/phone-numbers", key) or []
    except urllib.error.HTTPError:
        nums = []
    print(f"\nImported phone numbers ({len(nums)}):")
    for n in nums:
        ag = n.get("assigned_agent") or {}
        print(f"- {n.get('phone_number')} ({n.get('provider')}) -> {ag.get('agent_name') or 'no agent assigned'}")
    if not nums:
        print("  (none: Settings -> Offices & Variants -> Auto-detect from ElevenLabs imports them)")

    # Workspace conversation-initiation webhook (inbound agents)
    try:
        s = get("/v1/convai/settings", key) or {}
        init_cfg = s.get("conversation_initiation_client_data_webhook") or {}
        init = init_cfg.get("url") or ""
        init_has_header = any(str(h).lower() == "x-cgr-tool-secret" for h in (init_cfg.get("request_headers") or {}))
        expected = f"{public}/api/webhooks/elevenlabs/conversation-init" if public else ""
        if init:
            print(f"\nInbound start-of-call webhook: {init} {'OK' if init == expected else '<- not this app'}"
                  f" · tool secret header: {'yes' if init_has_header else 'MISSING (re-sync the inbound agent)'}")
        else:
            print("\nInbound start-of-call webhook: not set (fine unless you use the inbound receptionist)")
    except urllib.error.HTTPError:
        pass

    print("\n" + ("Looks good." if not problems else "To fix:\n- " + "\n- ".join(problems)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.URLError as e:
        print(f"Couldn't reach ElevenLabs ({e.__class__.__name__}).")
        sys.exit(1)
