"""Parsing for Indeed 'new application' notification emails.

Indeed usually does not attach the resume to these emails. It sends a link, and
the link is a click-tracker: cts.indeed.com/v1/<base64url( gzip(JSON) + signature )>,
where the JSON is {"u": "<real destination>", "m": {...tracking...}}.

This module only READS the email: who applied, for which job, and the summary
Indeed puts in the body. It never follows a link or contacts Indeed. Email intake
uses the result to list the applicant in the Needs Attention panel for a recruiter
to add by hand (see routes/email_intake.py). Treat the resume link as private:
callers must not store or log it (redact_resume_url gives a loggable form).

We decode the tracker locally rather than following the redirect, so parsing
doesn't register as the recruiter having clicked the email.

Everything here is pure — no network, no database — so it can be tested against
saved email bodies.
"""
import base64
import html as _html
import json
import logging
import re
import zlib
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

CTS_PREFIX = "cts.indeed.com/v1/"
RESUME_HOST_PATH = "employers.indeed.com/candidates/resume"

# Any of these in the body is enough to call an email "from Indeed". Kept broad on
# purpose: a false positive costs nothing (we simply find no resume link and fall
# through to the normal rejection), while a false negative silently drops a real
# applicant.
_INDEED_MARKERS = (
    "cts.indeed.com",
    "indeedemail.com",
    "indeed for employers",
    "employers.indeed.com",
)


def looks_like_indeed_application(subject: str, html: str, text: str) -> bool:
    """True if this email is plausibly an Indeed 'someone applied' notification."""
    hay = f"{subject}\n{html}\n{text}".lower()
    if not any(m in hay for m in _INDEED_MARKERS):
        return False
    return ("applied" in hay) or ("new application" in hay)


SAFELINK_HOST = "safelinks.protection.outlook.com"


def unwrap_safelinks(url: str) -> str:
    """Return the original URL from a Microsoft Safe Links wrapper.

    Microsoft 365 rewrites every link in a message it scans, so an Indeed email
    auto-forwarded from Outlook arrives with its tracker buried inside
    ...safelinks.protection.outlook.com/?url=<percent-encoded original>. Without
    unwrapping, the tracker is invisible to the decoder and a real applicant is
    silently rejected as having no resume.

    Anything that isn't wrapped is returned unchanged.
    """
    if not url or SAFELINK_HOST not in url.lower():
        return url
    try:
        target = (parse_qs(urlparse(url).query).get("url") or [""])[0]
        return _html.unescape(target) if target else url
    except Exception:
        return url


def decode_cts_link(url: str) -> Optional[str]:
    """Unwrap a cts.indeed.com tracking URL into its real destination.

    The payload is base64url( gzip-member + ~33 trailing signature bytes ), so a
    plain gzip.decompress() fails on the trailing data — decompressobj stops at
    the end of the member and leaves the signature in unused_data, which we ignore.
    Returns None for anything that isn't a decodable tracker.
    """
    url = unwrap_safelinks(url)
    if not url or CTS_PREFIX not in url:
        return None
    try:
        blob = url.split("/v1/", 1)[1].split("?")[0].split("#")[0]
        raw = base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4))
        payload = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw)
        dest = json.loads(payload).get("u")
        return _html.unescape(dest) if dest else None
    except Exception as e:
        logger.debug(f"cts decode failed: {type(e).__name__}: {e}")
        return None


def _anchors(html: str) -> List[Dict[str, str]]:
    """[{href, text}] for every <a> in the document, text stripped of tags."""
    out = []
    for m in re.finditer(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", html or "", re.I | re.S):
        inner = re.sub(r"<[^>]+>", " ", m.group(2))
        out.append({
            # Unwrapped here so every caller sees the real destination, whether the
            # mail passed through Microsoft's link rewriting or not.
            "href": unwrap_safelinks(_html.unescape(m.group(1))),
            "text": re.sub(r"\s+", " ", _html.unescape(inner)).strip(),
        })
    return out


def extract_resume_url(html: str) -> Optional[str]:
    """Find the "View resume" link in an Indeed application email.

    Its presence is what marks the email as an application; the link itself is
    never followed, stored or logged. "See full application" points somewhere
    else and is deliberately not used as a fallback.
    """
    links = _anchors(html)

    # 1. the anchor Indeed labels "View resume"
    for a in links:
        if a["text"].lower().startswith("view resume"):
            dest = decode_cts_link(a["href"])
            if dest and RESUME_HOST_PATH in dest:
                return dest

    # 2. label may be localised or restyled — decode every tracker and keep the
    #    one that lands on the resume endpoint.
    for a in links:
        dest = decode_cts_link(a["href"])
        if dest and RESUME_HOST_PATH in dest:
            return dest

    # 3. an already-unwrapped direct link (forwarded by hand, or Indeed drops the
    #    tracker in future)
    m = re.search(r"https://" + re.escape(RESUME_HOST_PATH) + r"\?[^\"'\s<>]+", html or "")
    if m:
        return _html.unescape(m.group(0))

    # 4. a rewritten link that never made it into an anchor — some gateways leave
    #    the wrapper as bare text. Unwrapped by query parsing rather than by
    #    percent-decoding the page, which would swallow Safe Links' own parameters
    #    into the URL.
    for wrapped in re.findall(
        r"https://[\w.-]*" + re.escape(SAFELINK_HOST) + r"/\?[^\"'\s<>]+", html or ""
    ):
        dest = unwrap_safelinks(_html.unescape(wrapped))
        if RESUME_HOST_PATH in dest:
            return dest
        decoded = decode_cts_link(dest)
        if decoded and RESUME_HOST_PATH in decoded:
            return decoded
    return None


def extract_applicant_name(subject: str, html: str, text: str) -> str:
    """Best-effort applicant name. Empty string if nothing looks right.

    Never guessed from the sender when the mail has been forwarded — that yields
    the forwarder's own name, which would silently create candidates named after
    whoever set up the mail rule.
    """
    # 1. "<a ...>Rowan Sample</a> applied" — the headline of the Indeed template
    m = re.search(r"<a\b[^>]*>\s*([^<]{2,60}?)\s*</a>\s*(?:</[^>]+>\s*)*applied\b", html or "", re.I | re.S)
    if m:
        name = re.sub(r"\s+", " ", _html.unescape(m.group(1))).strip()
        if _plausible_name(name):
            return name

    # 2. plain-text rendering of the same headline
    for line in (text or "").splitlines():
        line = re.sub(r"[͏​­<>]", "", line).strip()
        m = re.match(r"^(.{2,60}?)\s+applied\s*$", line, re.I)
        if m and _plausible_name(m.group(1).strip()):
            return m.group(1).strip()

    # 3. the forwarded original header — Indeed sets the display name to the
    #    applicant while the address stays an indeedemail.com relay
    m = re.search(r"From:\s*([^<\n]{2,60}?)\s*<[^>]*indeedemail\.com>", text or "", re.I)
    if m and _plausible_name(m.group(1).strip()):
        return m.group(1).strip()

    return ""


def _plausible_name(s: str) -> bool:
    if not s or len(s) > 60:
        return False
    low = s.lower()
    if any(bad in low for bad in ("indeed", "http", "@", "unsubscribe", "view ", "see ")):
        return False
    return bool(re.match(r"^[A-Za-z][A-Za-z'’\-\.\s]+$", s)) and len(s.split()) <= 5


def extract_job_title(subject: str) -> str:
    """'[Action required] New application for Sales Rep - Springfield, IL (Full-time), Springfield, IL'
    -> 'Sales Rep - Springfield, IL (Full-time)'. Cosmetic only; routing does not depend on it."""
    s = re.sub(r"^\s*(?:(?:re|fw|fwd)\s*:\s*)+", "", subject or "", flags=re.I)
    s = re.sub(r"^\s*\[[^\]]*\]\s*", "", s)
    m = re.search(r"new application for\s+(.+)$", s, re.I)
    if not m:
        return ""
    title = m.group(1).strip()
    # Indeed repeats the location after the title; drop the trailing repeat.
    parts = title.rsplit(",", 2)
    if len(parts) == 3 and parts[1].strip() and len(parts[2].strip()) <= 4:
        maybe_loc = f"{parts[1].strip()}, {parts[2].strip()}"
        if maybe_loc.lower() in parts[0].lower():
            return parts[0].strip()
    return title


def extract_highlights(text: str) -> Dict[str, Any]:
    """The summary Indeed puts in the email body: relevant experience + qualification
    chips. Not a substitute for the resume — there is no phone number here — but it
    gives a recruiter something to look at while the resume is still queued."""
    out: Dict[str, Any] = {"relevant_experience": "", "qualifications": []}
    flat = re.sub(r"https?://\S+", " ", text or "")
    flat = re.sub(r"[͏​­]", "", flat)

    m = re.search(r"Relevant experience:\s*(.+)", flat)
    if m:
        out["relevant_experience"] = re.sub(r"\s+", " ", m.group(1)).strip()[:300]

    m = re.search(r"Qualifications\s*(.+?)(?:See full application|View resume|This is an application)",
                  flat, re.I | re.S)
    if m:
        quals = [re.sub(r"\s+", " ", q).strip() for q in m.group(1).split("\n")]
        out["qualifications"] = [q for q in quals if 2 < len(q) < 60][:12]
    return out


def parse_indeed_application(subject: str, html: str, text: str) -> Optional[Dict[str, Any]]:
    """Full extraction. Returns None when this isn't an Indeed application email
    or carries no usable resume link — callers then fall through to normal handling."""
    if not looks_like_indeed_application(subject, html, text):
        return None
    resume_url = extract_resume_url(html)
    if not resume_url:
        return None
    return {
        "resume_url": resume_url,
        "applicant_name": extract_applicant_name(subject, html, text),
        "job_title": extract_job_title(subject),
        **extract_highlights(text),
    }


def redact_resume_url(url: str) -> str:
    """A loggable form of the resume link. The `id` parameter is the private part,
    so it is replaced entirely rather than truncated — a prefix still identifies
    the link."""
    if not url:
        return ""
    base = url.split("?", 1)[0]
    return f"{base}?<redacted>"
