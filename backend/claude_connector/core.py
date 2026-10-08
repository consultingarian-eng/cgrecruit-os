"""Claude connector core: a read-only MCP server with its own OAuth sign-in.

Claude (claude.ai, Desktop, mobile, Claude Code) connects to ``<public_url>/mcp``.
The first call gets a 401 pointing at our OAuth metadata. Claude registers
itself (RFC 7591 dynamic client registration), sends the person to
``/oauth/authorize``, they sign in with their normal app email + password, and
Claude receives an opaque access token.

Why this is read-only by construction:
  * The access token is a random string stored hashed in ``connector_tokens``.
    The app's REST API only accepts its own JWTs, so a connector token cannot
    call a single app endpoint, let alone a write.
  * ``/mcp`` only exposes the tools the app adapter registers, and every tool
    is a read (each is annotated ``readOnlyHint``).

Revocation: each grant is bound to the user's credential fingerprint (adapter
supplied: password hash, plus CG1's session_version). A password change, a
deleted/deactivated account, or a demo flag ends the connection on the next
call, and the refresh fails with ``invalid_grant`` so Claude asks the person
to sign in again.

Only standard-library + Starlette primitives are used so the same file runs on
CGRecruit (Starlette 0.37) and CG1 (Starlette 1.x) without adding packages.
Protocol: MCP streamable HTTP, stateless, JSON responses, handshake-era
versions (2025-03-26 .. 2025-11-25). A 2026-07-28 client's ``server/discover``
probe gets METHOD_NOT_FOUND and falls back to ``initialize``, per the spec.
This file is shared verbatim between CGRecruit and CG1 - keep them identical.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import logging
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

log = logging.getLogger("claude_connector")

SCOPE = "read"
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)
CODE_TTL = timedelta(minutes=10)
AUTH_REQUEST_TTL = timedelta(minutes=20)
AUDIT_TTL_SECONDS = 180 * 24 * 3600

# Sign-in throttle (failures only, like CG1's app login). The per-account limit
# is the real defence; the per-IP one is a backstop because proxy headers vary.
LOGIN_FAILS_PER_ACCOUNT = 10
LOGIN_FAILS_PER_IP = 100
LOGIN_WINDOW = timedelta(minutes=15)
REGISTRATIONS_PER_IP_PER_HOUR = 30

HANDSHAKE_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
LATEST_VERSION = HANDSHAKE_VERSIONS[0]

# Where Claude may send people back to. Anything else is refused at
# registration, so nobody can mint a look-alike login link that hands a code
# to their own site. Loopback is Claude Code (RFC 8252, any port).
HOSTED_CALLBACKS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]")
ALLOWED_ORIGINS = ("https://claude.ai", "https://claude.com")

MAX_RESULT_CHARS = 400_000


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> Optional[datetime]:
    """Mongo hands back naive UTC datetimes (tz_aware=False clients)."""
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_secret(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        # Proxies append; the rightmost hop is the one our edge saw.
        return fwd.split(",")[-1].strip() or "unknown"
    return (request.client.host if request.client else "") or "unknown"


def _is_loopback(uri: str) -> bool:
    try:
        p = urlparse(uri)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    if host == "::1":
        host = "[::1]"
    return p.scheme == "http" and host in LOOPBACK_HOSTS


def _allowed_redirect(uri: str) -> bool:
    return uri in HOSTED_CALLBACKS or _is_loopback(uri)


def _redirect_matches(registered: List[str], requested: str) -> bool:
    if requested in registered:
        return True
    if not _is_loopback(requested):
        return False
    # RFC 8252 7.3: loopback redirects match regardless of port.
    rq = urlparse(requested)
    for r in registered:
        if not _is_loopback(r):
            continue
        rp = urlparse(r)
        if (rp.hostname or "").lower() == (rq.hostname or "").lower() and rp.path == rq.path:
            return True
    return False


def _with_query(uri: str, **params: Optional[str]) -> str:
    p = urlparse(uri)
    q = parse_qsl(p.query, keep_blank_values=True)
    q.extend((k, v) for k, v in params.items() if v is not None)
    return urlunparse(p._replace(query=urlencode(q)))


def _pkce_ok(verifier: str, challenge: str) -> bool:
    if not verifier or not re.fullmatch(r"[A-Za-z0-9\-._~]{43,128}", verifier):
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return secrets.compare_digest(computed, challenge or "")


def _oauth_error(error: str, description: str = "", status: int = 400) -> JSONResponse:
    body = {"error": error}
    if description:
        body["error_description"] = description
    headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    if status == 401:
        headers["WWW-Authenticate"] = 'Basic realm="oauth"'
    return JSONResponse(body, status_code=status, headers=headers)


async def _read_form(request: Request) -> Dict[str, str]:
    """Token/revoke bodies are form-encoded (RFC 6749); tolerate JSON too."""
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            data = await request.json()
        except Exception:
            return {}
        return {k: str(v) for k, v in data.items() if v is not None} if isinstance(data, dict) else {}
    form = await request.form()
    return {k: str(v) for k, v in form.items()}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

class ToolError(Exception):
    """A problem the model can fix (bad argument, no access, nothing found)."""


@dataclass
class ToolContext:
    user: Dict[str, Any]
    db: Any


@dataclass
class Tool:
    name: str
    title: str
    description: str
    handler: Callable[[ToolContext, Dict[str, Any]], Awaitable[Any]]
    properties: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    required: List[str] = field(default_factory=list)

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": {
                "type": "object",
                "properties": self.properties,
                "required": list(self.required),
                "additionalProperties": False,
            },
            "annotations": {
                "title": self.title,
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        }


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_args(tool: Tool, raw: Any) -> Dict[str, Any]:
    """Check arguments against the tool's (simple) JSON schema and fill defaults.

    Supports the subset the tools use: string (enum, format=date), integer and
    number (minimum/maximum), boolean, and arrays of strings.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ToolError("Arguments must be an object.")
    unknown = sorted(set(raw) - set(tool.properties))
    if unknown:
        raise ToolError(f"Unknown argument(s): {', '.join(unknown)}. Allowed: {', '.join(sorted(tool.properties)) or 'none'}.")
    out: Dict[str, Any] = {}
    for name, spec in tool.properties.items():
        if name not in raw or raw[name] is None:
            if name in tool.required:
                raise ToolError(f"Missing required argument '{name}'.")
            if "default" in spec:
                out[name] = spec["default"]
            continue
        value = raw[name]
        kind = spec.get("type")
        if kind == "string":
            if not isinstance(value, str):
                raise ToolError(f"'{name}' must be a string.")
            value = value.strip()
            if spec.get("format") == "date":
                if not _DATE_RE.match(value):
                    raise ToolError(f"'{name}' must be a date like 2026-10-08.")
                try:
                    datetime.strptime(value, "%Y-%m-%d")
                except ValueError:
                    raise ToolError(f"'{name}' is not a real date.")
        elif kind == "integer":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
                raise ToolError(f"'{name}' must be a whole number.")
            value = int(value)
        elif kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ToolError(f"'{name}' must be a number.")
        elif kind == "boolean":
            if not isinstance(value, bool):
                raise ToolError(f"'{name}' must be true or false.")
        elif kind == "array":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ToolError(f"'{name}' must be a list of strings.")
        if "enum" in spec and value not in spec["enum"]:
            raise ToolError(f"'{name}' must be one of: {', '.join(map(str, spec['enum']))}.")
        if kind in ("integer", "number"):
            if "minimum" in spec and value < spec["minimum"]:
                raise ToolError(f"'{name}' must be at least {spec['minimum']}.")
            if "maximum" in spec and value > spec["maximum"]:
                raise ToolError(f"'{name}' must be at most {spec['maximum']}.")
        out[name] = value
    return out


# ---------------------------------------------------------------------------
# App adapter
# ---------------------------------------------------------------------------

class Adapter(Protocol):
    product: str            # shown on the sign-in page, e.g. "CGRecruit"
    instructions: str       # sent to Claude at initialize: vocabulary + traps
    tools: List[Tool]

    async def authenticate(self, email: str, password: str) -> Dict[str, Any]:
        """Return the user doc, or raise LoginRefused with a person-facing reason."""

    async def load_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Re-read the user for every call; None ends the connection."""

    def refuse_reason(self, user: Dict[str, Any]) -> Optional[str]:
        """Why this account may not use the connector (demo, inactive), or None."""

    def fingerprint(self, user: Dict[str, Any]) -> str:
        """Changes whenever the app would want old sessions to stop working."""


class LoginRefused(Exception):
    pass


# ---------------------------------------------------------------------------
# The connector
# ---------------------------------------------------------------------------

class Connector:
    def __init__(self, *, db: Any, public_url: str, adapter: Adapter, collection_prefix: str = "connector_"):
        self.db = db
        self.base = public_url.rstrip("/")
        self.adapter = adapter
        self.resource = f"{self.base}/mcp"
        self.prm_url = f"{self.base}/.well-known/oauth-protected-resource/mcp"
        self.tools: Dict[str, Tool] = {t.name: t for t in adapter.tools}
        p = collection_prefix
        self.c_clients = db[f"{p}clients"]
        self.c_requests = db[f"{p}auth_requests"]
        self.c_codes = db[f"{p}codes"]
        self.c_tokens = db[f"{p}tokens"]
        self.c_rate = db[f"{p}rate"]
        self.c_audit = db[f"{p}audit"]
        self._indexes_ready = False
        self._index_lock = asyncio.Lock()

    # -- wiring -------------------------------------------------------------

    def routes(self) -> List[Route]:
        return [
            Route("/.well-known/oauth-protected-resource", self.protected_resource_metadata, methods=["GET"]),
            Route("/.well-known/oauth-protected-resource/mcp", self.protected_resource_metadata, methods=["GET"]),
            Route("/.well-known/oauth-authorization-server", self.authorization_server_metadata, methods=["GET"]),
            Route("/oauth/register", self.register, methods=["POST"]),
            Route("/oauth/authorize", self.authorize, methods=["GET", "POST"]),
            Route("/oauth/token", self.token, methods=["POST"]),
            Route("/oauth/revoke", self.revoke, methods=["POST"]),
            Route("/mcp", self.mcp, methods=["GET", "POST", "DELETE"]),
        ]

    def install(self, app: Any) -> None:
        """Put our routes ahead of everything, including the SPA catch-all."""
        app.router.routes[0:0] = self.routes()

    async def _ensure_indexes(self) -> None:
        if self._indexes_ready:
            return
        async with self._index_lock:
            if self._indexes_ready:
                return
            try:
                for coll in (self.c_requests, self.c_codes, self.c_tokens, self.c_rate):
                    await coll.create_index("expires_at", expireAfterSeconds=0)
                await self.c_audit.create_index("at", expireAfterSeconds=AUDIT_TTL_SECONDS)
                await self.c_clients.create_index("client_id", unique=True)
                await self.c_requests.create_index("request_id", unique=True)
                await self.c_codes.create_index("code_hash", unique=True)
                await self.c_tokens.create_index("token_hash", unique=True)
                await self.c_tokens.create_index("family_id")
                await self.c_rate.create_index("key")
                self._indexes_ready = True
            except Exception:  # never block sign-in on index housekeeping
                log.exception("connector index creation failed")

    # -- discovery ----------------------------------------------------------

    async def protected_resource_metadata(self, request: Request) -> Response:
        return JSONResponse({
            "resource": self.resource,
            "authorization_servers": [self.base],
            "scopes_supported": [SCOPE],
            "bearer_methods_supported": ["header"],
            "resource_name": self.adapter.product,
        })

    async def authorization_server_metadata(self, request: Request) -> Response:
        return JSONResponse({
            "issuer": self.base,
            "authorization_endpoint": f"{self.base}/oauth/authorize",
            "token_endpoint": f"{self.base}/oauth/token",
            "registration_endpoint": f"{self.base}/oauth/register",
            "revocation_endpoint": f"{self.base}/oauth/revoke",
            "scopes_supported": [SCOPE],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
            "revocation_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
            "code_challenge_methods_supported": ["S256"],
        })

    # -- throttling ---------------------------------------------------------

    async def _count(self, key: str) -> int:
        try:
            return await self.c_rate.count_documents({"key": key, "expires_at": {"$gt": _now()}})
        except Exception:
            log.exception("connector rate count failed")
            return 0

    async def _bump(self, key: str, ttl: timedelta) -> None:
        try:
            await self.c_rate.insert_one({"key": key, "expires_at": _now() + ttl})
        except Exception:
            log.exception("connector rate bump failed")

    # -- registration (RFC 7591) ------------------------------------------

    async def register(self, request: Request) -> Response:
        await self._ensure_indexes()
        ip_key = f"register:ip:{_client_ip(request)}"
        if await self._count(ip_key) >= REGISTRATIONS_PER_IP_PER_HOUR:
            return _oauth_error("invalid_client_metadata", "Too many registrations; try again later.", 429)
        try:
            meta = await request.json()
        except Exception:
            return _oauth_error("invalid_client_metadata", "Body must be JSON.")
        if not isinstance(meta, dict):
            return _oauth_error("invalid_client_metadata", "Body must be a JSON object.")

        redirect_uris = meta.get("redirect_uris")
        if not isinstance(redirect_uris, list) or not redirect_uris or not all(isinstance(u, str) for u in redirect_uris):
            return _oauth_error("invalid_redirect_uri", "redirect_uris is required.")
        bad = [u for u in redirect_uris if not _allowed_redirect(u)]
        if bad:
            return _oauth_error("invalid_redirect_uri", "This server only works with Claude. Redirect not allowed: " + ", ".join(bad[:3]))

        grant_types = meta.get("grant_types") or ["authorization_code", "refresh_token"]
        if not isinstance(grant_types, list) or "authorization_code" not in grant_types:
            return _oauth_error("invalid_client_metadata", "grant_types must include authorization_code.")
        grant_types = [g for g in grant_types if g in ("authorization_code", "refresh_token")]
        response_types = meta.get("response_types") or ["code"]
        if response_types != ["code"] and "code" not in response_types:
            return _oauth_error("invalid_client_metadata", "Only response_type=code is supported.")
        method = meta.get("token_endpoint_auth_method") or "client_secret_basic"
        if method not in ("none", "client_secret_post", "client_secret_basic"):
            return _oauth_error("invalid_client_metadata", f"token_endpoint_auth_method {method} is not supported.")

        client_id = str(uuid.uuid4())
        issued_at = int(time.time())
        secret = None if method == "none" else _new_secret()
        name = str(meta.get("client_name") or "Claude")[:120]
        doc = {
            "client_id": client_id,
            "client_name": name,
            "redirect_uris": redirect_uris,
            "grant_types": grant_types,
            "token_endpoint_auth_method": method,
            "secret_hash": _hash(secret) if secret else None,
            "created_at": _now(),
            "created_ip": _client_ip(request),
        }
        await self.c_clients.insert_one(doc)
        await self._bump(ip_key, timedelta(hours=1))
        body = {
            "client_id": client_id,
            "client_id_issued_at": issued_at,
            "client_name": name,
            "redirect_uris": redirect_uris,
            "grant_types": grant_types,
            "response_types": ["code"],
            "token_endpoint_auth_method": method,
            "scope": SCOPE,
        }
        if secret:
            body["client_secret"] = secret
            body["client_secret_expires_at"] = 0
        return JSONResponse(body, status_code=201, headers={"Cache-Control": "no-store"})

    async def _get_client(self, client_id: str) -> Optional[Dict[str, Any]]:
        if not client_id:
            return None
        return await self.c_clients.find_one({"client_id": client_id}, {"_id": 0})

    async def _authenticate_client(self, request: Request, form: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """Token/revoke client authentication. None means invalid_client."""
        basic_id = basic_secret = None
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(auth[6:].strip()).decode("utf-8")
                basic_id, basic_secret = (unquote(x) for x in decoded.split(":", 1))
            except Exception:
                return None
        client_id = basic_id or form.get("client_id", "")
        client = await self._get_client(client_id)
        if not client:
            return None
        if form.get("client_id") and form.get("client_id") != client_id:
            return None
        method = client.get("token_endpoint_auth_method")
        if method == "none":
            return client
        presented = basic_secret if basic_secret is not None else form.get("client_secret")
        if not presented or not client.get("secret_hash"):
            return None
        if not secrets.compare_digest(_hash(presented), client["secret_hash"]):
            return None
        return client

    # -- authorization (sign-in page) ---------------------------------------

    async def authorize(self, request: Request) -> Response:
        await self._ensure_indexes()
        if request.method == "POST":
            return await self._authorize_submit(request)
        q = request.query_params
        client = await self._get_client(q.get("client_id", ""))
        if not client:
            return self._page_error("This sign-in link isn't valid. Start the connection again from Claude.")
        redirect_uri = q.get("redirect_uri")
        registered = client.get("redirect_uris") or []
        if redirect_uri:
            if not _redirect_matches(registered, redirect_uri):
                return self._page_error("This sign-in link isn't valid. Start the connection again from Claude.")
        elif len(registered) == 1:
            redirect_uri = registered[0]
        else:
            return self._page_error("This sign-in link isn't valid. Start the connection again from Claude.")

        state = q.get("state")

        def fail(error: str, desc: str) -> Response:
            return RedirectResponse(_with_query(redirect_uri, error=error, error_description=desc, state=state), status_code=302)

        if q.get("response_type") != "code":
            return fail("unsupported_response_type", "Only response_type=code is supported.")
        challenge = q.get("code_challenge", "")
        if not challenge or q.get("code_challenge_method", "plain") != "S256":
            return fail("invalid_request", "PKCE with S256 is required.")
        resource = q.get("resource")
        if resource and resource.rstrip("/") != self.resource:
            return fail("invalid_target", f"Unknown resource; use {self.resource}")

        request_id = _new_secret(24)
        await self.c_requests.insert_one({
            "request_id": request_id,
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "redirect_explicit": bool(q.get("redirect_uri")),
            "code_challenge": challenge,
            "state": state,
            "resource": resource,
            "expires_at": _now() + AUTH_REQUEST_TTL,
        })
        return self._page_login(request_id, redirect_uri)

    async def _authorize_submit(self, request: Request) -> Response:
        form = await request.form()
        request_id = str(form.get("request_id") or "")
        pending = await self.c_requests.find_one({"request_id": request_id}, {"_id": 0}) if request_id else None
        if not pending or (_aware(pending.get("expires_at")) or _now()) <= _now():
            return self._page_error("This sign-in page has expired. Start the connection again from Claude.")
        redirect_uri = pending["redirect_uri"]
        state = pending.get("state")

        if str(form.get("action") or "") == "cancel":
            await self.c_requests.delete_one({"request_id": request_id})
            return RedirectResponse(_with_query(redirect_uri, error="access_denied", error_description="Sign-in was cancelled.", state=state), status_code=303)

        email = str(form.get("email") or "").strip().lower()
        password = str(form.get("password") or "")
        acct_key = f"login:acct:{email}"
        ip_key = f"login:ip:{_client_ip(request)}"
        if await self._count(acct_key) >= LOGIN_FAILS_PER_ACCOUNT or await self._count(ip_key) >= LOGIN_FAILS_PER_IP:
            return self._page_login(request_id, redirect_uri, email=email, error="Too many attempts. Wait 15 minutes and try again.")
        if not email or not password:
            return self._page_login(request_id, redirect_uri, email=email, error="Enter your email and password.")

        try:
            user = await self.adapter.authenticate(email, password)
        except LoginRefused as exc:
            await self._bump(acct_key, LOGIN_WINDOW)
            await self._bump(ip_key, LOGIN_WINDOW)
            return self._page_login(request_id, redirect_uri, email=email, error=str(exc))
        reason = self.adapter.refuse_reason(user)
        if reason:
            return self._page_login(request_id, redirect_uri, email=email, error=reason)

        claimed = await self.c_requests.find_one_and_delete({"request_id": request_id})
        if not claimed:
            return self._page_error("This sign-in page was already used. Start the connection again from Claude.")
        code = _new_secret(32)
        await self.c_codes.insert_one({
            "code_hash": _hash(code),
            "client_id": pending["client_id"],
            "user_id": user["id"],
            "fingerprint": self.adapter.fingerprint(user),
            "redirect_uri": redirect_uri,
            "redirect_explicit": pending.get("redirect_explicit", True),
            "code_challenge": pending["code_challenge"],
            "resource": pending.get("resource"),
            "expires_at": _now() + CODE_TTL,
        })
        log.info("connector sign-in ok user=%s client=%s", user.get("id"), pending["client_id"])
        return RedirectResponse(_with_query(redirect_uri, code=code, state=state), status_code=303)

    # -- token endpoint -----------------------------------------------------

    async def token(self, request: Request) -> Response:
        await self._ensure_indexes()
        form = await _read_form(request)
        client = await self._authenticate_client(request, form)
        if not client:
            return _oauth_error("invalid_client", "Client authentication failed.", 401)
        grant = form.get("grant_type")
        if grant == "authorization_code":
            return await self._grant_code(client, form)
        if grant == "refresh_token":
            if "refresh_token" not in (client.get("grant_types") or []):
                return _oauth_error("unauthorized_client", "Client may not use refresh tokens.")
            return await self._grant_refresh(client, form)
        return _oauth_error("unsupported_grant_type", "Use authorization_code or refresh_token.")

    async def _grant_code(self, client: Dict[str, Any], form: Dict[str, str]) -> Response:
        code = form.get("code", "")
        record = await self.c_codes.find_one_and_delete({"code_hash": _hash(code)}) if code else None
        if not record or record.get("client_id") != client["client_id"]:
            return _oauth_error("invalid_grant", "Authorization code is invalid or already used.")
        if (_aware(record.get("expires_at")) or _now()) <= _now():
            return _oauth_error("invalid_grant", "Authorization code expired.")
        sent_redirect = form.get("redirect_uri")
        if record.get("redirect_explicit") or sent_redirect:
            if sent_redirect != record.get("redirect_uri"):
                return _oauth_error("invalid_grant", "redirect_uri does not match the authorization request.")
        if not _pkce_ok(form.get("code_verifier", ""), record.get("code_challenge", "")):
            return _oauth_error("invalid_grant", "PKCE verification failed.")
        resource = form.get("resource")
        if resource and resource.rstrip("/") != self.resource:
            return _oauth_error("invalid_target", f"Unknown resource; use {self.resource}")
        user = await self._live_user(record["user_id"], record.get("fingerprint"))
        if not user:
            return _oauth_error("invalid_grant", "This account can no longer be connected.")
        return await self._issue(client["client_id"], user, record.get("fingerprint"), family_id=str(uuid.uuid4()))

    async def _grant_refresh(self, client: Dict[str, Any], form: Dict[str, str]) -> Response:
        raw = form.get("refresh_token", "")
        record = await self.c_tokens.find_one_and_delete({"token_hash": _hash(raw), "kind": "refresh"}) if raw else None
        if not record or record.get("client_id") != client["client_id"]:
            return _oauth_error("invalid_grant", "Refresh token is invalid or already used.")
        if (_aware(record.get("expires_at")) or _now()) <= _now():
            return _oauth_error("invalid_grant", "Refresh token expired.")
        requested = (form.get("scope") or SCOPE).split()
        if any(s not in (SCOPE, "offline_access") for s in requested):
            return _oauth_error("invalid_scope", f"Only '{SCOPE}' is available.")
        user = await self._live_user(record["user_id"], record.get("fingerprint"))
        if not user:
            await self.c_tokens.delete_many({"family_id": record.get("family_id")})
            return _oauth_error("invalid_grant", "This account can no longer be connected.")
        await self.c_tokens.delete_many({"family_id": record.get("family_id"), "kind": "access"})
        return await self._issue(client["client_id"], user, record.get("fingerprint"), family_id=record.get("family_id"))

    async def _issue(self, client_id: str, user: Dict[str, Any], fingerprint: Optional[str], family_id: str) -> Response:
        access, refresh = _new_secret(32), _new_secret(32)
        now = _now()
        base = {"client_id": client_id, "user_id": user["id"], "fingerprint": fingerprint, "family_id": family_id, "scope": SCOPE, "created_at": now}
        await self.c_tokens.insert_one({**base, "kind": "access", "token_hash": _hash(access), "expires_at": now + ACCESS_TTL})
        await self.c_tokens.insert_one({**base, "kind": "refresh", "token_hash": _hash(refresh), "expires_at": now + REFRESH_TTL})
        return JSONResponse({
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": int(ACCESS_TTL.total_seconds()),
            "refresh_token": refresh,
            "scope": SCOPE,
        }, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    async def revoke(self, request: Request) -> Response:
        form = await _read_form(request)
        client = await self._authenticate_client(request, form)
        if not client:
            return _oauth_error("invalid_client", "Client authentication failed.", 401)
        raw = form.get("token", "")
        if raw:
            record = await self.c_tokens.find_one({"token_hash": _hash(raw)})
            if record and record.get("client_id") == client["client_id"]:
                await self.c_tokens.delete_many({"family_id": record.get("family_id")})
        return Response(status_code=200, headers={"Cache-Control": "no-store"})

    async def _live_user(self, user_id: str, fingerprint: Optional[str]) -> Optional[Dict[str, Any]]:
        user = await self.adapter.load_user(user_id)
        if not user or self.adapter.refuse_reason(user):
            return None
        if fingerprint is not None and not secrets.compare_digest(self.adapter.fingerprint(user), fingerprint):
            return None
        return user

    # -- MCP endpoint ---------------------------------------------------------

    def _unauthorized(self, description: str, token_sent: bool) -> Response:
        parts = [f'resource_metadata="{self.prm_url}"', f'scope="{SCOPE}"']
        if token_sent:
            parts.insert(0, 'error="invalid_token"')
            parts.insert(1, f'error_description="{description}"')
        return JSONResponse(
            {"error": "invalid_token" if token_sent else "unauthorized", "error_description": description},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer " + ", ".join(parts)},
        )

    async def _bearer_user(self, request: Request) -> Optional[Dict[str, Any]]:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return None
        raw = auth[7:].strip()
        record = await self.c_tokens.find_one({"token_hash": _hash(raw), "kind": "access"}) if raw else None
        if not record or (_aware(record.get("expires_at")) or _now()) <= _now():
            return None
        return await self._live_user(record["user_id"], record.get("fingerprint"))

    async def mcp(self, request: Request) -> Response:
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") not in (*ALLOWED_ORIGINS, self.base):
            return JSONResponse({"error": "Origin not allowed"}, status_code=403)

        await self._ensure_indexes()
        user = await self._bearer_user(request)
        if not user:
            sent = request.headers.get("authorization", "").lower().startswith("bearer ")
            return self._unauthorized("Sign in again to reconnect." if sent else "Sign-in required.", sent)

        if request.method != "POST":
            # Stateless server: no standalone SSE stream, no sessions to delete.
            return Response(status_code=405, headers={"Allow": "POST"})

        try:
            message = json.loads(await request.body())
        except Exception:
            return JSONResponse(_rpc_error(None, -32700, "Parse error"), status_code=400)
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return JSONResponse(_rpc_error(None, -32600, "Send one JSON-RPC 2.0 message per request."), status_code=400)
        if "method" not in message:
            return Response(status_code=202)  # a response/ack from the client
        if "id" not in message:
            return Response(status_code=202)  # notification, e.g. notifications/initialized

        msg_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        try:
            if method == "initialize":
                result = self._initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [t.describe() for t in self.tools.values()]}
            elif method == "tools/call":
                result = await self._call_tool(user, params)
            else:
                return JSONResponse(_rpc_error(msg_id, -32601, f"Method not found: {method}"))
        except _RpcError as exc:
            return JSONResponse(_rpc_error(msg_id, exc.code, exc.message))
        return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def _initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        asked = params.get("protocolVersion")
        version = asked if asked in HANDSHAKE_VERSIONS else LATEST_VERSION
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.adapter.product.lower(), "title": self.adapter.product, "version": "1.0.0"},
            "instructions": self.adapter.instructions,
        }

    async def _call_tool(self, user: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        tool = self.tools.get(name) if isinstance(name, str) else None
        if not tool:
            raise _RpcError(-32602, f"Unknown tool: {name}")
        started = time.monotonic()
        ok, error = True, None
        try:
            args = validate_args(tool, params.get("arguments"))
            payload = await tool.handler(ToolContext(user=user, db=self.db), args)
            text = json.dumps(payload, default=_json_default, ensure_ascii=False, separators=(",", ":"))
            if len(text) > MAX_RESULT_CHARS:
                raise ToolError("That answer is too large to return. Narrow it down (shorter date range, one pipeline/office, or a lower limit).")
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        except ToolError as exc:
            ok, error = False, str(exc)
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except Exception:
            ok, error = False, "internal error"
            log.exception("connector tool %s failed", name)
            result = {"content": [{"type": "text", "text": "Something went wrong running that tool. Try again, or ask a narrower question."}], "isError": True}
        await self._audit(user, tool.name, params.get("arguments"), ok, error, started)
        return result

    async def _audit(self, user: Dict[str, Any], tool: str, args: Any, ok: bool, error: Optional[str], started: float) -> None:
        try:
            await self.c_audit.insert_one({
                "at": _now(),
                "user_id": user.get("id"),
                "email": user.get("email"),
                "tool": tool,
                "args": args if isinstance(args, dict) else None,
                "ok": ok,
                "error": error,
                "ms": int((time.monotonic() - started) * 1000),
            })
        except Exception:
            log.exception("connector audit write failed")

    # -- pages ----------------------------------------------------------------

    def _page(self, body: str, status: int = 200, return_to: Optional[str] = None) -> HTMLResponse:
        product = html.escape(self.adapter.product)
        # Browsers apply form-action to the redirect that follows the POST, so the
        # page must allow the exact origin we send the person back to.
        form_action = "'self'"
        if return_to:
            rp = urlparse(return_to)
            form_action += f" {rp.scheme}://{rp.netloc}"
        doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>Connect Claude to {product}</title>
<style>
:root{{--bg:#f6f6f4;--card:#fff;--ink:#1c1c1a;--muted:#6b6b66;--line:#deded8;--accent:#1c1c1a;--accent-ink:#fff;--err:#a8261b;--err-bg:#fbeceb}}
@media (prefers-color-scheme: dark){{:root{{--bg:#151514;--card:#1f1f1d;--ink:#ededea;--muted:#a3a39c;--line:#34342f;--accent:#ededea;--accent-ink:#151514;--err:#ff8a7f;--err-bg:#3a1f1c}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}}
main{{max-width:420px;margin:0 auto;padding:48px 16px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px 24px}}
h1{{font-size:21px;margin:0 0 8px}}p{{margin:0 0 16px;color:var(--muted)}}
label{{display:block;font-size:14px;font-weight:600;margin:14px 0 6px}}
input{{width:100%;padding:11px 12px;border:1px solid var(--line);border-radius:9px;background:var(--bg);color:var(--ink);font-size:16px}}
.row{{display:flex;gap:10px;margin-top:22px}}
button{{flex:1;padding:12px;border-radius:9px;border:1px solid var(--accent);font-size:15px;font-weight:600;cursor:pointer}}
.go{{background:var(--accent);color:var(--accent-ink)}}.no{{background:transparent;color:var(--ink);border-color:var(--line)}}
.err{{background:var(--err-bg);color:var(--err);border-radius:9px;padding:10px 12px;margin:0 0 6px;font-size:14px}}
.fine{{font-size:13px;margin:18px 0 0}}
</style></head><body><main><div class="card">{body}</div></main></body></html>"""
        return HTMLResponse(doc, status_code=status, headers={
            "Cache-Control": "no-store",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": f"default-src 'none'; style-src 'unsafe-inline'; form-action {form_action}; frame-ancestors 'none'; base-uri 'none'",
            "Referrer-Policy": "same-origin",  # no-referrer would send "Origin: null" on the form POST
        })

    def _page_login(self, request_id: str, redirect_uri: str, email: str = "", error: str = "") -> HTMLResponse:
        product = html.escape(self.adapter.product)
        if _is_loopback(redirect_uri):
            dest = "Claude Code on this computer (localhost). Only continue if you started this from Claude Code yourself."
        else:
            dest = html.escape(urlparse(redirect_uri).hostname or "Claude")
        err = f'<div class="err" role="alert">{html.escape(error)}</div>' if error else ""
        body = f"""<h1>Connect Claude to {product}</h1>
<p>Sign in with your {product} account. Claude gets <strong>read-only</strong> access to what your account can already see. It can't change anything.</p>
{err}<form method="post" action="/oauth/authorize" autocomplete="on">
<input type="hidden" name="request_id" value="{html.escape(request_id)}">
<label for="email">Email</label><input id="email" name="email" type="email" autocomplete="username" required value="{html.escape(email)}">
<label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required>
<div class="row"><button class="no" type="submit" name="action" value="cancel" formnovalidate>Cancel</button><button class="go" type="submit" name="action" value="allow">Sign in &amp; connect</button></div>
</form><p class="fine">You'll be sent back to: {dest}</p>"""
        return self._page(body, status=200 if not error else 400, return_to=redirect_uri)

    def _page_error(self, message: str) -> HTMLResponse:
        return self._page(f"<h1>Can't connect</h1><p>{html.escape(message)}</p>", status=400)


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _rpc_error(msg_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _aware(value).isoformat()
    if isinstance(value, (set, tuple)):
        return list(value)
    return str(value)
