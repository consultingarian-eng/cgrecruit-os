"""Demo-account guard.

A user doc with is_demo: true is a walk-around trial account: it sees
everything its role and tenant scope allow, but no submission may reach
production data. This middleware refuses every mutating /api/ request whose
JWT subject belongs to a demo account, in one blanket rule, so no individual
endpoint needs to know demo accounts exist. Auth endpoints stay open so the
account still logs in and out like any other.

It also scrubs credentials out of what a demo account reads back. Blocking by
method alone is not enough when a read hands over a secret that works on a
path with no JWT on it at all: `candidate.public_token` comes back on every
candidate read and is the only thing guarding POST /api/public/applicant/
{token}/book, which rebooks a real applicant and emails them about it.

Pure ASGI on purpose — BaseHTTPMiddleware was removed from this app because
it wraps HTMLResponse bodies in a streaming iterator that can get lost (see
the note next to the CORS registration in server.py).
"""
import json
import logging
import time

from starlette.requests import Request

from auth_service import get_current_user_payload
from deps import db
from link_redaction import redact_magic_links_deep

logger = logging.getLogger(__name__)

BLOCKED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
ALLOWED_PATHS = {"/api/auth/login", "/api/auth/logout"}

# Reads that happen to be POSTs. A trial account is supposed to SEE everything,
# and this handler only renders and returns, so refusing it just blanks out a
# working screen. Only add a path here after reading its handler end to end and
# confirming it cannot write.
ALLOWED_READ_POSTS = {"/api/templates/preview"}

# The mirror image: GETs a trial account must not have. These hand back live
# credentials — the intake webhook token is the only thing standing in front of
# POST /api/webhooks/zapier/<token>, which files a real candidate and fires the
# warm-up email, the SMS and the auto-dial at a real person. It also mints and
# persists the token when it is missing, so it is a write wearing a GET.
BLOCKED_READS = {
    "/api/integrations/zapier-token",
}

# Field names that are credentials rather than information. A trial account may
# read the record; it may not read the secret that lets it act on that record
# somewhere this guard cannot see. Blanked (not removed) so response shapes
# stay the same and the SPA keeps rendering.
SCRUBBED_KEYS = frozenset({
    "public_token",
    "zapier_webhook_token",
    "cg1_webhook_secret",
    "webhook_secret",
    "password_hash",
})

# Responses larger than this, or streamed without a declared length, go out
# untouched — holding an unbounded body in memory to rewrite it would be worse
# than the leak. Candidate lists are a few MB at most.
_MAX_SCRUB_BYTES = 32 * 1024 * 1024

_demo_ids_cache = {"ids": frozenset(), "at": 0.0}
_DEMO_IDS_TTL_SECONDS = 60.0


async def _demo_user_ids() -> frozenset:
    now = time.monotonic()
    if now - _demo_ids_cache["at"] > _DEMO_IDS_TTL_SECONDS:
        rows = await db.users.find({"is_demo": True}, {"id": 1}).to_list(100)
        _demo_ids_cache["ids"] = frozenset(r["id"] for r in rows if r.get("id"))
        _demo_ids_cache["at"] = now
    return _demo_ids_cache["ids"]


async def _request_token_sub(scope) -> str | None:
    """The JWT subject of the caller, or None. Never raises — a bad/absent
    token just means the normal auth layer answers the request."""
    request = Request(scope)
    try:
        payload = await get_current_user_payload(
            request, authorization=request.headers.get("Authorization")
        )
        return payload.get("sub")
    except Exception:
        return None


async def is_demo_caller(scope) -> bool:
    """True when this request carries a demo account's token.

    This runs on every request now, so the cheap check goes first: an HS256
    decode with no I/O, and only then the 60s-cached id lookup. An anonymous
    request — every static asset, every health check — costs no database call
    at all, which is how it was before the guard looked at reads."""
    sub = await _request_token_sub(scope)
    if not sub:
        return False
    return sub in await _demo_user_ids()


async def demo_write_blocked(scope) -> bool:
    """True when a demo account is reaching for something it must not have —
    any mutation, or one of the credential-handing reads in BLOCKED_READS."""
    path = scope.get("path", "")
    if not path.startswith("/api/"):
        return False
    if path in BLOCKED_READS:
        pass  # refused whatever the method
    elif scope.get("method") not in BLOCKED_METHODS:
        return False
    elif path in ALLOWED_PATHS or path in ALLOWED_READ_POSTS:
        return False
    return await is_demo_caller(scope)


def _collect_tokens(value, found: set) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if k == "public_token" and isinstance(v, str) and v:
                found.add(v)
            else:
                _collect_tokens(v, found)
    elif isinstance(value, list):
        for v in value:
            _collect_tokens(v, found)


def _blank_keys(value):
    if isinstance(value, dict):
        return {
            k: ("" if k in SCRUBBED_KEYS else _blank_keys(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_blank_keys(v) for v in value]
    return value


def scrub_credentials(value):
    """Blank every SCRUBBED_KEYS field, however deeply nested, and take the
    token out of every candidate magic link inside a string.

    The key alone wasn't enough: the token also rides inside text, such as the
    sent emails and texts on /communications, SMS threads, transcripts and
    rendered template previews (`.../applicant/<token>`, `/reschedule/<token>`).
    Any public_token value seen in the response is also replaced wherever it
    appears."""
    tokens: set = set()
    _collect_tokens(value, tokens)
    return redact_magic_links_deep(_blank_keys(value), tokens)


def _scrubbing_send(send):
    """Wrap an ASGI send so a JSON response of known, sane length is rewritten
    with its credentials blanked. Anything else passes through untouched."""
    held = {"start": None, "chunks": None}

    async def wrapped(message):
        if message["type"] == "http.response.start":
            headers = message.get("headers") or []
            ctype = length = b""
            for key, val in headers:
                lowered = key.lower()
                if lowered == b"content-type":
                    ctype = val.lower()
                elif lowered == b"content-length":
                    length = val
            scrubbable = (
                ctype.startswith(b"application/json")
                and length.isdigit()
                and int(length) <= _MAX_SCRUB_BYTES
            )
            if not scrubbable:
                return await send(message)
            held["start"] = message
            held["chunks"] = []
            return

        if message["type"] == "http.response.body" and held["chunks"] is not None:
            held["chunks"].append(message.get("body") or b"")
            if message.get("more_body"):
                return
            body = b"".join(held["chunks"])
            if not body:
                # HEAD, 204, and friends: the declared length describes a body
                # that was never sent. Re-emit the original start untouched.
                start, held["start"], held["chunks"] = held["start"], None, None
                await send(start)
                return await send(message)
            try:
                body = json.dumps(scrub_credentials(json.loads(body))).encode()
            except Exception:
                logger.warning("demo scrub: response left as-is, could not parse JSON")
            start = dict(held["start"])
            start["headers"] = [
                (k, v) for k, v in (start.get("headers") or [])
                if k.lower() != b"content-length"
            ] + [(b"content-length", str(len(body)).encode())]
            held["start"] = None
            held["chunks"] = None
            await send(start)
            return await send({"type": "http.response.body", "body": body})

        return await send(message)

    return wrapped


class DemoWriteGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        # Only /api/ responses carry credentials, and only /api/ requests can
        # mutate, so everything else skips the check entirely.
        if not scope.get("path", "").startswith("/api/"):
            return await self.app(scope, receive, send)

        try:
            is_demo = await is_demo_caller(scope)
        except Exception:
            # Identifying the caller must never be what takes the API down.
            logger.exception("demo guard: could not resolve caller")
            return await self.app(scope, receive, send)

        if is_demo and await demo_write_blocked(scope):
            detail = (
                "Demo account — credentials aren't part of the trial."
                if scope.get("path") in BLOCKED_READS
                else "Demo account — this trial doesn't save changes."
            )
            body = json.dumps({"detail": detail}).encode()
            await send({
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, _scrubbing_send(send) if is_demo else send)
