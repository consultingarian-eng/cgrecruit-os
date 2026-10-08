#!/usr/bin/env python3
"""Read-only check of the Twilio side of CGRecruit. Sends nothing.

Run it with the live app's variables injected, so no secret is typed or shown:

    railway run python3 .claude/skills/connect-twilio/check_twilio.py

(or export TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and APP_PUBLIC_URL yourself).

It reports: the account's status and type, every number with its voice/SMS
capability and where its incoming texts go, Messaging Services that override
those numbers, and whether each texting number points at
<APP_PUBLIC_URL>/api/webhooks/twilio/inbound-sms. Secrets are never printed;
the account SID is masked to its first 4 characters.

One optional WRITE, only when the owner has agreed to it:

    railway run python3 .claude/skills/connect-twilio/check_twilio.py --set-sms-webhook +447700900123

points that one number's "A message comes in" webhook at the app (HTTP POST).
It changes Twilio configuration only; it never sends a text or places a call.
Standard library only.
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.twilio.com/2010-04-01"
MSG_API = "https://messaging.twilio.com/v1"


def mask(v: str) -> str:
    return (v[:4] + "…") if v else "(not set)"


def env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


class Twilio:
    def __init__(self, sid: str, token: str):
        self.sid = sid
        self.auth = "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode()

    def call(self, url: str, data: dict = None) -> dict:
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, method="POST" if body else "GET",
                                     headers={"Authorization": self.auth})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")

    def pages(self, url: str, key: str):
        while url:
            page = self.call(url)
            for item in page.get(key) or []:
                yield item
            nxt = page.get("next_page_uri") or (page.get("meta") or {}).get("next_page_url")
            if not nxt:
                break
            url = nxt if nxt.startswith("http") else "https://api.twilio.com" + nxt


def main() -> int:
    sid, token = env("TWILIO_ACCOUNT_SID"), env("TWILIO_AUTH_TOKEN")
    public = env("APP_PUBLIC_URL").rstrip("/")
    print(f"TWILIO_ACCOUNT_SID: {mask(sid)}")
    print(f"TWILIO_AUTH_TOKEN:  {'set' if token else '(not set)'}")
    print(f"APP_PUBLIC_URL:     {public or '(not set)'}")
    if not (sid and token):
        print("\nTwilio isn't configured in this environment. Nothing to check.")
        return 1
    if not public.startswith("https://"):
        print("\nWARNING: APP_PUBLIC_URL should be the app's final https:// address.")
    expected = f"{public}/api/webhooks/twilio/inbound-sms" if public else ""
    tw = Twilio(sid, token)

    # Optional write: point one number's SMS webhook at the app.
    if len(sys.argv) == 3 and sys.argv[1] == "--set-sms-webhook":
        target = sys.argv[2].replace(" ", "")
        if not expected:
            print("APP_PUBLIC_URL is needed to set the webhook.")
            return 1
        for n in tw.pages(f"{API}/Accounts/{sid}/IncomingPhoneNumbers.json?PageSize=100", "incoming_phone_numbers"):
            if n.get("phone_number") == target:
                tw.call(f"{API}/Accounts/{sid}/IncomingPhoneNumbers/{n['sid']}.json",
                        {"SmsUrl": expected, "SmsMethod": "POST"})
                print(f"\n{target}: incoming texts now go to {expected} (POST).")
                print("If this number is in a Messaging Service, that service's own incoming setting"
                      " wins: set it there too (docs/TWILIO.md, step 4). Run the check again to confirm.")
                return 0
        print(f"\n{target} isn't a number on this Twilio account.")
        return 1
    if len(sys.argv) > 1:
        print(__doc__)
        return 1

    try:
        acct = tw.call(f"{API}/Accounts/{sid}.json")
    except urllib.error.HTTPError as e:
        print(f"\nTwilio refused the credentials ({e.code}). Check the SID and Auth Token.")
        return 1
    print(f"\nAccount: status={acct.get('status')} type={acct.get('type')}")
    if acct.get("type") == "Trial":
        print("  -> Trial account: it can only reach verified numbers and adds a trial notice. Upgrade before going live.")

    # Messaging Services and the numbers they own (their incoming setting wins).
    svc_for_number = {}
    try:
        for s in tw.pages(f"{MSG_API}/Services?PageSize=50", "services"):
            label = s.get("friendly_name") or s.get("sid")
            uses_number = s.get("use_inbound_webhook_on_number")
            inbound = s.get("inbound_request_url") or ""
            for pn in tw.pages(f"{MSG_API}/Services/{s['sid']}/PhoneNumbers?PageSize=50", "phone_numbers"):
                svc_for_number[pn.get("phone_number")] = (label, uses_number, inbound)
    except urllib.error.HTTPError:
        pass  # Messaging API not available to this key; numbers are still checked.

    print("\nNumbers:")
    problems = 0
    count = 0
    for n in tw.pages(f"{API}/Accounts/{sid}/IncomingPhoneNumbers.json?PageSize=100", "incoming_phone_numbers"):
        count += 1
        num = n.get("phone_number")
        caps = n.get("capabilities") or {}
        voice, sms = bool(caps.get("voice")), bool(caps.get("sms"))
        sms_url = n.get("sms_url") or ""
        voice_url = n.get("voice_url") or ""
        print(f"- {num} ({n.get('friendly_name')}) voice={'yes' if voice else 'no'} sms={'yes' if sms else 'no'}")
        if num and num.startswith("+44") and sms and not num.startswith("+447"):
            print("    note: UK texting normally needs a +44 7 mobile number")
        effective = sms_url
        if num in svc_for_number:
            label, uses_number, inbound = svc_for_number[num]
            print(f"    in Messaging Service '{label}'")
            if not uses_number:
                effective = inbound
                print(f"    incoming texts follow the service: {inbound or '(service has no webhook)'}")
        if sms:
            if effective == expected:
                print("    incoming texts -> the app  OK")
            else:
                problems += 1
                print(f"    incoming texts -> {effective or '(nowhere)'}")
                print(f"    expected        -> {expected or '(set APP_PUBLIC_URL)'}")
        if voice:
            if "elevenlabs" in voice_url.lower():
                print("    voice -> ElevenLabs (imported; managed mode)  OK")
            elif voice_url:
                print(f"    voice -> {voice_url}")
            else:
                print("    voice -> not set (fine until it's imported into ElevenLabs)")
    if not count:
        print("  (none yet: buy one in Console -> Phone Numbers -> Buy a number)")
    print(f"\n{problems} texting number(s) not pointing at the app." if problems else "\nAll texting numbers point at the app.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.URLError as e:
        print(f"Couldn't reach Twilio ({e.__class__.__name__}).")
        sys.exit(1)
