"""Auth: bcrypt password hashing + JWT.

Tokens are accepted from EITHER (in order of priority):
  1. `Authorization: Bearer <jwt>` header — used by automated tests, integrations
     that hit our REST API directly, and any client that doesn't speak cookies.
  2. `cgr_token` httpOnly cookie set by `/api/auth/login` — used by the React
     SPA so a malicious script (XSS) can't read the token from JavaScript.

Both paths produce the same decoded payload via `decode_token`. Frontend code
should use `withCredentials: true` so the cookie auto-attaches; the Bearer
header path is kept as a strict-additive fallback.

CSRF: cookie is set with `SameSite=Lax` so cross-origin POSTs from a
malicious site won't auto-attach it. We also enforce `Secure` flag in
production. Top-level navigation that retains Lax (clicked links from email,
etc.) is fine because we don't have any state-changing GET endpoints.
"""
import os
import bcrypt
import jwt
from datetime import datetime, timedelta, timezone
from fastapi import Depends, HTTPException, Request, Response, status, Header
from typing import Optional


# ---- Password + token policy ----
MIN_PASSWORD_LENGTH = 10
# HMAC algorithms only: JWT_ALGORITHM can never select "none" or an asymmetric
# algorithm that would treat JWT_SECRET as a public key.
_ALLOWED_JWT_ALGORITHMS = ("HS256", "HS384", "HS512")
MIN_JWT_SECRET_LENGTH = 32


def _jwt_settings():
    secret = os.environ["JWT_SECRET"]
    if len(secret) < MIN_JWT_SECRET_LENGTH:
        raise RuntimeError(
            f"JWT_SECRET must be at least {MIN_JWT_SECRET_LENGTH} characters "
            "(generate one with: python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
        )
    algo = os.environ.get("JWT_ALGORITHM", "HS256").strip().upper() or "HS256"
    if algo not in _ALLOWED_JWT_ALGORITHMS:
        raise RuntimeError(f"JWT_ALGORITHM must be one of {', '.join(_ALLOWED_JWT_ALGORITHMS)}")
    return secret, algo


# ---- Cookie config ----
COOKIE_NAME = "cgr_token"
COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 30  # 30 days; matches JWT_EXPIRES_HOURS default 720h


def _cookie_secure() -> bool:
    """True when the deploy is HTTPS — locks the cookie to TLS in prod."""
    # Production runs behind https. Local dev (FastAPI on
    # localhost:8001 hit directly) is the only case where Secure would block
    # the cookie — we default to True and let an env override flip it off
    # for explicitly-local sessions.
    return os.environ.get("AUTH_COOKIE_SECURE", "true").lower() not in ("0", "false", "no")


def set_auth_cookie(response: Response, token: str) -> None:
    """Attach the JWT as an httpOnly cookie on the response. Called by the
    register/login endpoints; the SPA never has to touch the token in JS."""
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        # Path "/" so the cookie reaches /api routes through the K8s ingress.
        path="/",
    )


def clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")


def hash_password(plain: str) -> str:
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(plain.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def create_token(user_id: str, email: str) -> str:
    secret, algo = _jwt_settings()
    hours = int(os.environ.get("JWT_EXPIRES_HOURS", "720"))
    payload = {
        "sub": user_id,
        "email": email,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(hours=hours),
    }
    return jwt.encode(payload, secret, algorithm=algo)


def decode_token(token: str) -> dict:
    secret, algo = _jwt_settings()
    try:
        return jwt.decode(token, secret, algorithms=[algo])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


async def get_current_user_payload(
    request: Request,
    authorization: Optional[str] = Header(None),
) -> dict:
    """Resolve the JWT payload from either the Authorization header (priority)
    or the httpOnly `cgr_token` cookie. Raises 401 if neither produces a
    valid token. The header-first ordering means tests and Bearer-based API
    consumers keep working without any change."""
    token: Optional[str] = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not token:
        token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Missing credentials — log in again",
        )
    return decode_token(token)
