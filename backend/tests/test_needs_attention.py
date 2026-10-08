"""Tests for which ingested candidates get flagged as needing attention.

The cost of a false negative here is a real applicant nobody ever calls, so the
positive cases matter. But the cost of a false positive is a panel full of noise
that gets dismissed on sight, which is how a warning system dies — hence the
equal attention to what must NOT be flagged.
"""
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from needs_attention_service import (  # noqa: E402
    THIN_RESUME_CHARS,
    candidate_issues,
    notification_text,
    summarise,
    worst_severity,
)

GOOD_RESUME = "x" * (THIN_RESUME_CHARS + 50)


def candidate(**over):
    base = {
        "first_name": "April",
        "last_name": "Birchmont",
        "email": "april@example.com",
        "phone": "+15550100124",
        "resume_text": GOOD_RESUME,
        "parsed_resume": {"first_name": "April"},
    }
    base.update(over)
    return base


class TestNothingWrong:
    def test_complete_candidate_is_not_flagged(self):
        assert candidate_issues(candidate()) == []

    def test_manually_added_candidate_without_a_resume_is_not_flagged_as_thin(self):
        # No resume was ever processed, so "resume text almost empty" is not a
        # failure — it is just a candidate someone typed in by hand.
        c = candidate(resume_text=None, parsed_resume=None)
        assert "thin_resume" not in candidate_issues(c)


class TestMissingContactDetails:
    def test_missing_phone(self):
        assert "no_phone" in candidate_issues(candidate(phone=""))

    def test_missing_email(self):
        assert "no_email" in candidate_issues(candidate(email=""))

    def test_whitespace_only_counts_as_missing(self):
        issues = candidate_issues(candidate(phone="   ", email="\t"))
        assert "no_phone" in issues and "no_email" in issues

    def test_none_counts_as_missing(self):
        issues = candidate_issues(candidate(phone=None, email=None))
        assert "no_phone" in issues and "no_email" in issues


class TestThinResume:
    def test_short_extraction_with_no_contact_details_is_flagged(self):
        # A scanned-image PDF extracts to almost nothing, and anything scored off
        # it is meaningless — but it looks like a normal candidate on the board.
        assert "thin_resume" in candidate_issues(candidate(resume_text="Name only"))

    def test_empty_extraction_is_flagged_when_a_resume_was_processed(self):
        assert "thin_resume" in candidate_issues(candidate(resume_text=""))

    def test_a_genuinely_sparse_resume_is_not_flagged(self):
        # Real case: Kai Example, 195 chars, high-school diploma and four
        # skills. Complete and usable. Flagging him is the false positive that
        # trains people to ignore the panel.
        real = (
            "Kai Example\n"
            "Example City, EX 00000 | +1 555 010 0146 | kaiexample_pn4@indeedemail.com\n"
            "Education\nHigh school diploma\nExample City\n"
            "Skills\nOrganizational skills  Cash handling  Teamwork  Cleaning"
        )
        assert len(real) < THIN_RESUME_CHARS, "fixture must be short, or it proves nothing"
        assert "thin_resume" not in candidate_issues(candidate(resume_text=real))

    def test_a_phone_number_alone_is_enough_to_clear_it(self):
        assert "thin_resume" not in candidate_issues(candidate(resume_text="Jane Doe (555) 123-4567"))

    def test_long_text_is_never_flagged_even_without_contact_details(self):
        assert "thin_resume" not in candidate_issues(candidate(resume_text="y" * THIN_RESUME_CHARS))

    def test_manually_added_candidate_is_never_flagged_as_thin(self):
        # No parsed_resume means no resume was processed — nothing failed.
        assert "thin_resume" not in candidate_issues(
            candidate(resume_text="", parsed_resume=None))


class TestSeverityOrdering:
    def test_a_link_only_application_outranks_everything(self):
        assert worst_severity(["resume_not_attached"]) > worst_severity(["no_phone", "no_email", "thin_resume"])

    def test_no_phone_outranks_no_email(self):
        # Missing a phone means the dialer never runs; missing an email means one
        # message didn't send. The first is worse.
        assert worst_severity(["no_phone"]) > worst_severity(["no_email"])

    def test_no_issues_is_zero(self):
        assert worst_severity([]) == 0


class TestSummaries:
    def test_lists_every_problem_worst_first(self):
        s = summarise(["no_email", "no_phone"])
        assert s.index("phone") < s.index("email")

    def test_notification_names_the_candidate_and_pipeline(self):
        msg = notification_text("Willa Birchmont", ["no_phone"], "Riverside")
        assert "Willa Birchmont" in msg["title"]
        assert "phone" in msg["title"].lower()
        assert "Riverside" in msg["body"]

    def test_notification_copes_with_an_unnamed_applicant(self):
        msg = notification_text("", ["no_phone"], "")
        assert msg["title"].startswith("An applicant")
