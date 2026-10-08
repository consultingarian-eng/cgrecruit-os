"""Which freshly-ingested candidates need a human to look at them.

Intake runs unattended — an application email arrives, the resume is parsed, and
a call is scheduled without anyone watching. That is the point, but it means a
partial result is invisible: a candidate with no phone number sits in the
pipeline looking perfectly healthy while the dialer quietly never calls them, and
an application that only LINKED to its resume produces no candidate record at all.

This module decides what counts as "needs attention". Kept free of FastAPI and
database imports so the rules can be tested directly against candidate documents.

Deliberately scoped to *recent* candidates. An all-time list of everyone ever
missing an email would be long, stale and ignored; the value here is catching a
bad ingest while it is still worth fixing.
"""
import re
from typing import Any, Dict, List

# Below this a resume is suspiciously short. Not damning on its own — a real
# applicant with a high-school diploma and four skills lands around 195 chars,
# and flagging them would be the false positive that gets this panel ignored.
THIN_RESUME_CHARS = 200

_CONTACT = re.compile(r"[\w.+-]+@[\w-]+\.\w+|\+?\d[\d\-.\s()]{8,}\d")

# Candidates this old are no longer "just arrived"; chasing them is normal
# recruiting work, not an ingest problem.
DEFAULT_WINDOW_DAYS = 14

SEVERITY = {
    "resume_not_attached": 3,   # no candidate record exists at all
    "no_phone": 2,       # the dialer silently cannot call
    "thin_resume": 1,    # scored against almost no text
    "no_email": 1,       # no warmup, no booking link
}

LABELS = {
    "resume_not_attached": "Resume not attached — add by hand",
    "no_phone": "No phone number",
    "thin_resume": "Resume text almost empty",
    "no_email": "No email address",
}

DETAILS = {
    "resume_not_attached": "No candidate was created — the application email only linked to the resume. Add them by hand or forward the resume to the intake address, then mark it handled.",
    "no_phone": "The auto-dialer can't call this candidate.",
    "thin_resume": "Extraction produced almost no text, so any AI score is unreliable.",
    "no_email": "Warmup email and booking link can't be sent.",
}


def candidate_issues(cand: Dict[str, Any]) -> List[str]:
    """Issue codes for one candidate document. Empty list means it's fine.

    `thin_resume` is deliberately hard to trigger. It exists to catch extraction
    failing — a scanned-image PDF with no text layer — not to judge applicants for
    having short resumes. Shortness alone is not the signal: a real Indeed resume
    with a high-school diploma and four skills is under 200 characters and is
    perfectly usable, because it still carries a phone number and an email.

    So a resume counts as thin only when it is both short AND carries no contact
    details at all, which is what a failed extraction actually looks like.

    It also only applies where a resume was really processed — a manually-added
    candidate with no resume is not a failed ingest.
    """
    issues: List[str] = []

    if not (cand.get("phone") or "").strip():
        issues.append("no_phone")
    if not (cand.get("email") or "").strip():
        issues.append("no_email")

    if cand.get("parsed_resume"):
        text = (cand.get("resume_text") or "").strip()
        if len(text) < THIN_RESUME_CHARS and not _CONTACT.search(text):
            issues.append("thin_resume")

    return issues


def worst_severity(issues: List[str]) -> int:
    return max((SEVERITY.get(i, 0) for i in issues), default=0)


def summarise(issues: List[str]) -> str:
    """One line naming every problem, worst first."""
    ordered = sorted(issues, key=lambda i: -SEVERITY.get(i, 0))
    return ", ".join(LABELS.get(i, i) for i in ordered)


def notification_text(name: str, issues: List[str], pipeline_name: str = "") -> Dict[str, str]:
    """Title and body for the bell when an incomplete candidate is ingested."""
    who = name or "An applicant"
    where = f" — {pipeline_name}" if pipeline_name else ""
    return {
        "title": f"{who} added with {summarise(issues).lower()}",
        "body": " ".join(DETAILS.get(i, "") for i in
                         sorted(issues, key=lambda i: -SEVERITY.get(i, 0))).strip() + where,
    }
