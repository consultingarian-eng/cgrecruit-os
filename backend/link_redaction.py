"""Candidate magic links inside text.

A candidate's `public_token` is the only authentication on their portal. With
it, anyone can open their details, book or rebook their interview and email
them about it (POST /api/public/applicant/{token}/book,
/api/public/reschedule/{token}/book, ...). Those routes carry no session, so
nothing upstream can tell a stranger from the candidate.

Blanking the `public_token` field is not enough, because the token also travels
inside text: every email and text we send has the portal, chat and reschedule
links in its body, and rendered template previews have them too. Anything that
hands such text to someone who must not act for the candidate (a demo account,
the Claude connector) passes it through redact_magic_links() first.
"""
import re
from typing import Any, Iterable

# What a token is replaced with. It is a valid-looking path segment, so a
# redacted link still reads as a link but opens nothing.
REDACTED_TOKEN = "redacted"

# /applicant/<token>, /retry/<token>, /reschedule/<token>, /form/<token>,
# /portal/<token>: absolute or relative, with /api/public/ in front or not,
# and with the slashes percent-encoded (as in a link passed through a
# redirect or tracking URL).
_MAGIC_LINK = re.compile(
    r"((?:/|%2[Ff]|&(?:amp;)?#(?:x2[Ff]|47);)(?:applicant|retry|reschedule|form|portal)(?:/|%2[Ff]|&(?:amp;)?#(?:x2[Ff]|47);))([A-Za-z0-9_-]{8,})",
)


def redact_magic_links(text: str, known_tokens: Iterable[str] = ()) -> str:
    """Replace the token in every magic-link path, and any of `known_tokens`
    wherever it appears, with REDACTED_TOKEN."""
    if not isinstance(text, str) or not text:
        return text
    out = _MAGIC_LINK.sub(lambda m: m.group(1) + REDACTED_TOKEN, text)
    for tok in known_tokens:
        if tok and tok != REDACTED_TOKEN and tok in out:
            out = out.replace(tok, REDACTED_TOKEN)
    return out


def redact_magic_links_deep(value: Any, known_tokens: Iterable[str] = ()) -> Any:
    """redact_magic_links() applied to every string in a JSON-like value."""
    known = tuple(t for t in known_tokens if isinstance(t, str) and len(t) >= 8)
    return _deep(value, known)


def _deep(value: Any, known: tuple) -> Any:
    if isinstance(value, str):
        return redact_magic_links(value, known)
    if isinstance(value, dict):
        return {k: _deep(v, known) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep(v, known) for v in value]
    return value
