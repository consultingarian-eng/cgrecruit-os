"""Twilio signs its callbacks, and on /api/twiml/status that signature is the
only authentication there is. A demo account can read conversation ids, so
without this check it could POST CallStatus=completed and close a real call.
The check fails closed (no token, or an error, means refused).
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1/x")
os.environ.setdefault("DB_NAME", "x")
os.environ.setdefault("JWT_SECRET", "unit-test-secret-with-32-plus-chars!!")

import deps  # noqa: E402


class _Req:
    def __init__(self, headers, form, path="/api/twiml/status/conv-1"):
        self.headers = headers
        self._form = form
        self.url = type("U", (), {"scheme": "https", "netloc": "cgr.example", "path": path})()

    async def form(self):
        return self._form


def _signed(token, url, params):
    from twilio.request_validator import RequestValidator
    return RequestValidator(token).compute_signature(url, params)


def test_a_correct_signature_is_accepted(monkeypatch):
    token = "test-auth-token"
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", token)
    url = "https://cgr.example/api/twiml/status/conv-1"
    params = {"CallStatus": "completed", "CallDuration": "42"}
    req = _Req({"X-Twilio-Signature": _signed(token, url, params)}, params)
    assert asyncio.run(deps.twilio_signature_invalid(req)) is False


def test_a_forged_callback_is_rejected(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-auth-token")
    params = {"CallStatus": "completed", "CallDuration": "42"}
    # What someone with a conversation id but no auth token would send.
    req = _Req({"X-Twilio-Signature": "obviously-wrong"}, params)
    assert asyncio.run(deps.twilio_signature_invalid(req)) is True
    # Or nothing at all.
    assert asyncio.run(deps.twilio_signature_invalid(_Req({}, params))) is True


def test_no_configured_token_blocks(monkeypatch):
    """Fail closed: without TWILIO_AUTH_TOKEN nothing can prove a callback came
    from Twilio, so it is refused."""
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ALLOW_UNSIGNED_TWILIO_WEBHOOKS", raising=False)
    req = _Req({}, {"CallStatus": "completed"})
    assert asyncio.run(deps.twilio_signature_invalid(req)) is True


def test_local_dev_can_opt_out_explicitly(monkeypatch):
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ALLOW_UNSIGNED_TWILIO_WEBHOOKS", "true")
    req = _Req({}, {"CallStatus": "completed"})
    assert asyncio.run(deps.twilio_signature_invalid(req)) is False


def test_an_unexpected_error_fails_closed(monkeypatch):
    """An error while checking is not proof of anything."""
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-auth-token")

    class _Boom(_Req):
        async def form(self):
            raise RuntimeError("boom")

    assert asyncio.run(deps.twilio_signature_invalid(_Boom({"X-Twilio-Signature": "x"}, {}))) is True
