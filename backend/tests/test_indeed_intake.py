"""Tests for Indeed application-email parsing.

Fixtures are synthetic. They mirror the structure of a real Indeed notification —
including the exact click-tracker encoding, verified against a live email — but
carry an invented applicant, so no real candidate's PII lands in the repo.

The behaviour that matters most here is the negative case: this parser sits in the
inbound-email path ahead of the existing "no resume attachment" rejection, so any
email it wrongly claims is an email the normal pipeline stops seeing.
"""
import base64
import gzip
import json
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from indeed_intake import (  # noqa: E402
    decode_cts_link,
    extract_applicant_name,
    extract_highlights,
    extract_job_title,
    extract_resume_url,
    looks_like_indeed_application,
    parse_indeed_application,
    redact_resume_url,
    unwrap_safelinks,
)

RESUME_DEST = (
    "https://employers.indeed.com/candidates/resume"
    "?refUid=0example0000000&id=AAAAAExampleResumeId00000000000000000000&ctx=email&from=email"
)
LOGIN_DEST = (
    "https://account.indeed.com/o/myaccess/switch/confirm"
    "?employerId=00000000000000000000000000000000&continue=https%3A%2F%2Femployers.indeed.com"
)
SUBJECT = ("[Action required] New application for Sales & Customer Service Representative "
           "– Springfield, IL (Full-time), Springfield, IL")


def cts(dest: str, sig: bytes = b"x" * 33) -> str:
    """Rebuild Indeed's tracker encoding: base64url( gzip(JSON) + signature ).

    The trailing signature is the detail that breaks a naive gzip.decompress(),
    so it is reproduced here rather than omitted for convenience.
    """
    payload = json.dumps({"u": dest, "m": {"clickType": "View resume link"}}).encode()
    return "https://cts.indeed.com/v1/" + base64.urlsafe_b64encode(
        gzip.compress(payload) + sig
    ).decode().rstrip("=")


def email_html(applicant="Rowan Sample", resume=True) -> str:
    view = f'<a href="{cts(RESUME_DEST)}">View resume &rArr;</a>' if resume else ""
    return f"""
    <table><tr><td><img alt="Indeed For Employers"></td></tr></table>
    <h1><a href="{cts('https://employers.indeed.com/candidates/view?id=1')}">{applicant}</a> applied</h1>
    <p>Sales &amp; Customer Service Representative &#8226; Springfield, IL</p>
    <a href="{cts(LOGIN_DEST)}">See full application</a>
    {view}
    <p>indeedemail.com</p>
    """


EMAIL_TEXT = """From: Rowan Sample <rowan0_example@indeedemail.com>
Subject: [Action required] New application for Sales & Customer Service Representative

Rowan Sample applied
Relevant experience: Member Sales at Example Wholesale
Qualifications
US work authorization
Ability to Commute
Customer service
See full application
View resume
This is an application update about a candidate who applied to your job
"""


# ─── tracker decoding ───────────────────────────────────────────────────────

def test_decodes_tracker_with_trailing_signature():
    assert decode_cts_link(cts(RESUME_DEST)) == RESUME_DEST


def test_decode_tolerates_signature_of_any_length():
    for n in (0, 1, 33, 64):
        assert decode_cts_link(cts(RESUME_DEST, sig=b"z" * n)) == RESUME_DEST


@pytest.mark.parametrize("bad", [
    "", "https://example.com/foo", "https://cts.indeed.com/v1/not-base64!!",
    "https://cts.indeed.com/v1/" + base64.urlsafe_b64encode(b"not gzip").decode(),
])
def test_decode_returns_none_for_junk(bad):
    assert decode_cts_link(bad) is None


# ─── Microsoft Safe Links (auto-forwarding from Outlook) ────────────────────

def safelink(inner: str) -> str:
    """Wrap a URL the way Microsoft 365 rewrites links in a scanned message."""
    return ("https://eur01.safelinks.protection.outlook.com/?url="
            + quote(inner, safe="")
            + "&data=05%7C02%7C&sdata=abc%3D&reserved=0")


def test_unwraps_a_safelink():
    assert unwrap_safelinks(safelink(RESUME_DEST)) == RESUME_DEST


def test_unwrapping_leaves_ordinary_urls_alone():
    assert unwrap_safelinks(RESUME_DEST) == RESUME_DEST
    assert unwrap_safelinks("") == ""


def test_decodes_a_tracker_that_outlook_rewrote():
    # The forwarding case: Outlook wraps Indeed's tracker, which would otherwise
    # hide the cts prefix from the decoder entirely.
    assert decode_cts_link(safelink(cts(RESUME_DEST))) == RESUME_DEST


def test_finds_the_resume_link_in_an_outlook_forwarded_email():
    html = f'<a href="{safelink(cts(RESUME_DEST))}">View resume &rArr;</a>'
    assert extract_resume_url(html) == RESUME_DEST


def test_still_refuses_the_login_link_when_rewritten():
    # The rewrite must not become a way for the login-walled link to sneak through.
    html = f'<a href="{safelink(cts(LOGIN_DEST))}">See full application</a>'
    assert extract_resume_url(html) is None


def test_finds_a_direct_link_hidden_inside_a_rewrite():
    # Some gateways encode the destination without leaving a usable anchor.
    html = f'<p>{safelink(RESUME_DEST)}</p>'
    assert extract_resume_url(html) == RESUME_DEST


# ─── picking the right link ─────────────────────────────────────────────────

def test_prefers_the_view_resume_link():
    assert extract_resume_url(email_html()) == RESUME_DEST


def test_never_returns_the_login_walled_link():
    """'See full application' goes to an Indeed login page, which is not the
    resume link this parser looks for.

    The wrong answer is only reachable by decoding — the destination never appears
    as plaintext in the email — so the guard has to be proven against the decoded
    form, not against a substring of the HTML.
    """
    html = email_html(resume=False)
    decoded = [decode_cts_link(h) for h in
               __import__("re").findall(r'href="([^"]+)"', html)]
    assert any(d and "account.indeed.com" in d for d in decoded), \
        "fixture should contain the login link, or this proves nothing"
    assert extract_resume_url(html) is None


def test_finds_a_direct_link_if_the_tracker_is_gone():
    assert extract_resume_url(f'<a href="{RESUME_DEST}">View resume</a>') == RESUME_DEST


# ─── applicant name ─────────────────────────────────────────────────────────

def test_extracts_name_from_headline():
    assert extract_applicant_name(SUBJECT, email_html("Rowan Sample"), "") == "Rowan Sample"


def test_extracts_name_from_forwarded_header_when_html_is_stripped():
    assert extract_applicant_name(SUBJECT, "", EMAIL_TEXT) == "Rowan Sample"


def test_does_not_invent_a_name():
    assert extract_applicant_name("", "<p>nothing here</p>", "") == ""


@pytest.mark.parametrize("junk", ["View resume", "Indeed", "see full application",
                                  "http://x.com", "a@b.com"])
def test_rejects_non_names(junk):
    html = f"<h1><a href='#'>{junk}</a> applied</h1>"
    assert extract_applicant_name("", html, "") == ""


# ─── metadata ───────────────────────────────────────────────────────────────

def test_job_title_drops_indeeds_duplicated_location():
    assert extract_job_title(SUBJECT) == (
        "Sales & Customer Service Representative – Springfield, IL (Full-time)")


def test_job_title_survives_forward_prefixes():
    assert extract_job_title("Fw: Re: " + SUBJECT).startswith("Sales & Customer Service")


def test_highlights():
    h = extract_highlights(EMAIL_TEXT)
    assert h["relevant_experience"] == "Member Sales at Example Wholesale"
    assert "US work authorization" in h["qualifications"]
    assert "Ability to Commute" in h["qualifications"]


# ─── the non-interference guarantee ─────────────────────────────────────────

def test_parses_a_real_shaped_indeed_email():
    r = parse_indeed_application(SUBJECT, email_html(), EMAIL_TEXT)
    assert r["resume_url"] == RESUME_DEST
    assert r["applicant_name"] == "Rowan Sample"
    assert r["qualifications"]


@pytest.mark.parametrize("subject,html,text", [
    ("Resume - Indigo Varnley", "<p>Please see attached</p>", "Please see attached"),
    ("", "", ""),
    ("Fwd: candidate", "<a href='https://linkedin.com/in/x'>profile</a>", "profile"),
    # Indeed-ish but with no resume link: must NOT be claimed, or the normal
    # rejection path stops seeing it.
    ("New application for X", "<p>Indeed For Employers … applied</p>", "applied"),
    # the login link alone is not enough
    ("New application", f"<a href='{cts(LOGIN_DEST)}'>See full application</a> applied", "applied"),
])
def test_returns_none_for_everything_else(subject, html, text):
    assert parse_indeed_application(subject, html, text) is None


def test_detector_requires_both_an_indeed_marker_and_an_application_word():
    assert not looks_like_indeed_application("hello", "<p>cts.indeed.com</p>", "")
    assert not looks_like_indeed_application("someone applied", "<p>greenhouse.io</p>", "")
    assert looks_like_indeed_application("", "<p>cts.indeed.com</p>", "applied")


# ─── the URL is a credential ────────────────────────────────────────────────

def test_redaction_removes_the_whole_token_not_just_part_of_it():
    red = redact_resume_url(RESUME_DEST)
    assert "AAAAAExampleResumeId00000000000000000000" not in red
    assert "id=" not in red
    assert "refUid" not in red          # a prefix of a secret is still a secret
    assert red.startswith("https://employers.indeed.com/candidates/resume")


def test_redaction_handles_empty():
    assert redact_resume_url("") == ""
