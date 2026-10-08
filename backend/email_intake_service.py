"""Email intake service: receives resume emails via SendGrid Inbound Parse webhook,
extracts the resume attachment, parses with Claude, and creates a candidate.

SendGrid Inbound Parse posts multipart/form-data with these key fields:
    from        - sender address ("Jane Doe <jane@example.com>")
    to          - recipient address ("apply@inbox.example.com")
    subject     - email subject
    text        - plain text body
    html        - html body
    attachments - integer count of attachments
    attachment-info - JSON describing each attachment
    attachment1, attachment2, ... - the actual file uploads
    envelope    - JSON envelope info
"""
import re
import json
import logging
from typing import Dict, Any, Optional, List, Tuple

logger = logging.getLogger(__name__)


_RESUME_EXT = (".pdf", ".docx", ".doc", ".txt", ".rtf")


def parse_email_address(raw: str) -> Tuple[str, str]:
    """Returns (display_name, email_address). 'Jane Doe <jane@x.com>' -> ('Jane Doe', 'jane@x.com')"""
    if not raw:
        return "", ""
    raw = raw.strip()
    m = re.match(r'^\s*"?([^"<]*?)"?\s*<([^>]+)>\s*$', raw)
    if m:
        return m.group(1).strip(), m.group(2).strip().lower()
    if "@" in raw:
        return "", raw.strip().lower()
    return raw, ""


def split_full_name(full: str) -> Tuple[str, str]:
    parts = (full or "").strip().split()
    if not parts:
        return "Unknown", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def extract_local_part(email: str) -> str:
    """Returns the part before '@'. Handles plus-addressing: apply+downtown@... -> 'apply+downtown'."""
    if not email or "@" not in email:
        return ""
    return email.split("@", 0 + 1)[0].strip().lower()


def detect_pipeline(
    pipelines: List[Dict[str, Any]],
    to_address: str,
    subject: str,
    body: str,
) -> Optional[Dict[str, Any]]:
    """Decide which pipeline a new candidate belongs to.

    Order of preference:
    1. Recipient local-part exactly matches a pipeline `public_slug`
       (e.g. downtown@inbox... → Downtown pipeline)
    2. Recipient local-part matches via plus-addressing
       (e.g. apply+downtown@inbox...)
    3. Subject/body contains pipeline name or slug as a keyword
    4. First pipeline (fallback)
    """
    if not pipelines:
        return None

    local = extract_local_part(to_address)
    haystack = f"{subject} {body}".lower()

    # 1. exact slug match
    if local:
        for p in pipelines:
            if (p.get("public_slug") or "").lower() == local:
                return p

    # 2. plus-addressing apply+slug
    if "+" in local:
        suffix = local.split("+", 1)[1]
        for p in pipelines:
            if (p.get("public_slug") or "").lower() == suffix:
                return p

    # 3. keyword match in subject/body — pipeline name tokens or slug
    for p in pipelines:
        slug = (p.get("public_slug") or "").lower()
        name = (p.get("name") or "").lower()
        # split pipeline name into short tokens for matching ("Downtown, EX" -> ["downtown", "ex"])
        tokens = [t for t in re.split(r"[,\s/]+", name) if len(t) >= 4]
        if slug and slug in haystack:
            return p
        for t in tokens:
            if re.search(rf"\b{re.escape(t)}\b", haystack):
                return p

    # 4. fallback
    return pipelines[0]


def select_resume_attachment(
    attachments: Dict[str, Any],
    attachment_info: Dict[str, Any],
) -> Optional[Tuple[str, bytes]]:
    """Pick the single best resume attachment. Returns (filename, UploadFile) or None."""
    all_atts = select_all_resume_attachments(attachments, attachment_info)
    return all_atts[0] if all_atts else None


def select_all_resume_attachments(
    attachments: Dict[str, Any],
    attachment_info: Dict[str, Any],
) -> List[Tuple[str, Any]]:
    """Return ALL resume attachments from the SendGrid multipart payload.
    Sorted by type preference (pdf > docx > doc > txt > rtf).
    Used for bulk email drops where one email may contain many resumes.
    """
    candidates: List[Tuple[str, Any]] = []
    seen_names: set = set()
    for field, upload in (attachments or {}).items():
        filename = getattr(upload, "filename", "") or ""
        if filename.lower().endswith(_RESUME_EXT) and filename not in seen_names:
            candidates.append((filename, upload))
            seen_names.add(filename)
    if not candidates and attachment_info:
        for field, info in attachment_info.items():
            ct = (info.get("type") or "").lower()
            name = info.get("filename") or ""
            if ct in ("application/pdf",
                      "application/msword",
                      "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                      "text/plain") and name not in seen_names:
                upload = (attachments or {}).get(field)
                if upload:
                    candidates.append((name, upload))
                    seen_names.add(name)
    rank = {".pdf": 0, ".docx": 1, ".doc": 2, ".txt": 3, ".rtf": 4}
    candidates.sort(key=lambda x: rank.get("." + x[0].rsplit(".", 1)[-1].lower(), 99))
    return candidates


def normalize_phone(raw: str) -> str:
    """Normalize a phone-ish string to digits with leading + if available."""
    if not raw:
        return ""
    raw = raw.strip()
    digits = re.sub(r"[^\d+]", "", raw)
    return digits


def extract_phone_from_text(text: str) -> str:
    """Best-effort phone extraction from email body."""
    if not text:
        return ""
    # find first US-style or international phone
    m = re.search(r"(\+?\d[\d\-\.\s\(\)]{8,}\d)", text)
    if not m:
        return ""
    return normalize_phone(m.group(1))
