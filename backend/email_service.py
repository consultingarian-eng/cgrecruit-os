"""SendGrid email service for stage-based applicant communications."""
import os
import logging
import re
import base64
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition
import company_profile
from app_tz import app_zone, default_tz_name  # APP_TIMEZONE (noqa: F401)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

logger = logging.getLogger(__name__)


def _resolve_tz(tz_name: Optional[str]):
    if not tz_name or ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return timezone.utc


def _hhmm_to_ampm(hhmm: str) -> str:
    """Convert '09:00' → '9:00 AM', '19:00' → '7:00 PM'."""
    try:
        h, m = int(hhmm.split(":")[0]), int(hhmm.split(":")[1] if ":" in hhmm else "0")
        suffix = "AM" if h < 12 else "PM"
        display_h = h % 12 or 12
        return f"{display_h}:{m:02d} {suffix}"
    except Exception:
        return hhmm


def is_in_call_window(settings: Dict[str, Any], ahead_minutes: int = 0) -> bool:
    """Returns True if (now + ahead_minutes) falls within the recruiter's configured call window.
    Pass ahead_minutes=warmup_delay_minutes to check whether the *scheduled call* will be
    in-window, not just the moment the warmup fires."""
    import pytz
    from datetime import timedelta
    ad = (settings or {}).get("auto_dialer") or {}
    region = (settings or {}).get("region_language") or {}
    tz_name = region.get("timezone") or default_tz_name()
    start_hhmm = ad.get("call_window_start") or "09:00"
    end_hhmm = ad.get("call_window_end") or "19:00"
    allowed_days = ad.get("call_window_days") or [0, 1, 2, 3, 4]
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.timezone(default_tz_name())
    now = datetime.now(tz) + timedelta(minutes=ahead_minutes)
    if now.weekday() not in allowed_days:
        return False
    sh, sm = int(start_hhmm.split(":")[0]), int(start_hhmm.split(":")[1] if ":" in start_hhmm else "0")
    eh, em = int(end_hhmm.split(":")[0]), int(end_hhmm.split(":")[1] if ":" in end_hhmm else "0")
    window_start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    window_end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    return window_start <= now <= window_end


# ===== Brand palette for candidate emails. Emails keep a light card body for
# cross-client rendering (Gmail dark-mode inversion mangles dark layouts), and
# the header carries a gradient. Override any key with "email_colors" in
# backend/company_profile.json to use your own brand colours.
_DEFAULT_BRAND = {
    "primary": "#ec008c",           # links, buttons, accents
    "primary_dark": "#8a2bc2",      # gradient fallback color
    "accent": "#4353ff",
    "ink": "#1a1026",               # deep plum-black body text on light bg
    "ink_muted": "#6b5a80",
    "bg": "#f5effa",                # outer canvas — light lavender tint
    "card": "#FFFFFF",
    "border": "#e9defa",
    "success": "#10B981",
    # Header gradient (magenta → purple → blue → cyan).
    "grad": "linear-gradient(115deg,#ec008c,#a21caf 28%,#6d28a8 50%,#4353ff 76%,#22d3ee)",
    # Button gradient (magenta → fuchsia → purple).
    "grad_btn": "linear-gradient(135deg,#ec008c,#c026a9 52%,#7a2a9e)",
}


def _brand_palette() -> Dict[str, str]:
    import company_profile
    return {**_DEFAULT_BRAND, **(company_profile.PROFILE.get("email_colors") or {})}


BRAND = _brand_palette()


def render_template(text: str, vars_map: Dict[str, str]) -> str:
    """Replace [Placeholder] with values from vars_map. Unknown placeholders left untouched.
    Also collapses any blank lines that result from empty placeholders so the final
    email reads cleanly when optional fields (Instagram, Website, Recruiter Phone) are unset."""
    if not text:
        return ""

    def repl(match):
        key = match.group(1).strip()
        return str(vars_map.get(key, match.group(0)))

    rendered = re.sub(r"\[([^\[\]\n]+?)\]", repl, text)
    # Drop lines that are now empty (a placeholder collapsed to "") — but keep
    # lines with even one non-whitespace character (e.g. real signatures).
    out_lines = []
    prev_blank = False
    for line in rendered.splitlines():
        stripped = line.strip()
        if not stripped:
            # Compress consecutive blank lines to a single blank line.
            if prev_blank:
                continue
            prev_blank = True
            out_lines.append("")
        else:
            prev_blank = False
            out_lines.append(line)
    # Trim trailing blank lines.
    while out_lines and not out_lines[-1].strip():
        out_lines.pop()
    return "\n".join(out_lines)


def build_template_vars(candidate: Dict[str, Any], settings: Dict[str, Any], job: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    profile = (settings or {}).get("recruiter_profile", {}) or {}
    sca = (settings or {}).get("screen_call_agent", {}) or {}
    region = (settings or {}).get("region_language", {}) or {}
    tz_name = region.get("timezone") or default_tz_name()
    full_name = f"{candidate.get('first_name', '')} {candidate.get('last_name', '')}".strip()
    appt_at = candidate.get("appointment_at", "") or ""
    training_at = candidate.get("training_start_at", "") or ""
    # Build the public retry-screening URL using the candidate's existing public_token.
    base_url = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
    retry_link = f"{base_url}/retry/{candidate.get('public_token','')}" if candidate.get("public_token") else ""
    reschedule_link = f"{base_url}/reschedule/{candidate.get('public_token','')}" if candidate.get("public_token") else ""
    form_link = f"{base_url}/applicant/{candidate.get('public_token','')}" if candidate.get("public_token") else ""
    social = profile.get("social_links") or {}
    role_title = (job or {}).get("title", "") or profile.get("job_role", "")
    website_url = profile.get("website", "") or ""
    instagram_url = social.get("instagram", "") or ""
    linkedin_url = social.get("linkedin", "") or ""
    company_name = profile.get("company_name", "") or sca.get("company_name", "") or "our team"
    return {
        "First Name": candidate.get("first_name", "") or "",
        "Last Name": candidate.get("last_name", "") or "",
        "Full Name": full_name,
        "Email": candidate.get("email", "") or "",
        "Phone Number": candidate.get("phone", "") or "",
        # Role: empty when no job/role configured — templates should use "[Role Phrase]"
        # for the natural "X role" fallback.
        "Role": role_title or "the role",
        # Smart fallback: "Customer Service role" if known, else "role".
        "Role Phrase": (f"{role_title} role" if role_title else "role"),
        "Job Title": role_title,
        "Company": company_name,
        "Website": website_url,
        "Instagram": instagram_url,
        "LinkedIn": linkedin_url,
        # Pre-formatted "You can read more about us at <url>." — empty if not set.
        "Website Line": (f"You can read more about us at {website_url}." if website_url else ""),
        "Instagram Line": (f"You can also find us on Instagram: {instagram_url}" if instagram_url else ""),
        "Recruiter Email": profile.get("recruiter_email", "") or "",
        "Recruiter Phone": profile.get("phone", "") or sca.get("custom_caller_id", "") or "",
        "City": profile.get("city", "") or (job or {}).get("city", "") or "",
        "Date": _format_date(appt_at, tz_name),
        "Time": _format_time(appt_at, tz_name),
        # When no join link is configured the var must still read as a sentence:
        # templates say "Join: [Zoom Link]", and an empty value left "Join: "
        # dangling (and the old placeholder URL opened Zoom's invalid-meeting
        # error). The recruiter is belled to fix it (booking.link_missing).
        "Zoom Link": candidate.get("appointment_link", "") or "link to follow shortly",
        "Reschedule URL": reschedule_link,
        "Rebook URL": reschedule_link,
        "Screening Link": retry_link,
        "Form Link": form_link,
        # Same URL as [Form Link] under a name that says what the page is: the
        # candidate's status portal (journey, countdown, join link, prep). Use
        # this in booking/confirmation copy; [Form Link] in questionnaire copy.
        "Portal Link": form_link,
        "Retry Link": retry_link,
        # Caller ID = the pipeline's verified outbound caller-ID phone number,
        # used in copy like "We'll call you from [Caller ID]". This is distinct
        # from [Phone Number] above which is the CANDIDATE's phone (the person
        # being messaged). [Call Number] is kept as a legacy alias.
        "Caller ID": sca.get("custom_caller_id", "") or "our team",
        "Call Number": sca.get("custom_caller_id", "") or "our team",
        "Agent Name": sca.get("agent_name", "Olivia"),
        "Disqualification Reason": candidate.get("disqualification_reason", "") or "",
        "Start Date": _format_date(training_at, tz_name),
        "Start Time": _format_time(training_at, tz_name),
        "Call Window Start": _hhmm_to_ampm(((settings or {}).get("auto_dialer") or {}).get("call_window_start") or "09:00"),
        "Call Window End": _hhmm_to_ampm(((settings or {}).get("auto_dialer") or {}).get("call_window_end") or "19:00"),
        # Moment-of-apply CTAs — deep-link into the retry page's mode switcher
        # so the candidate lands directly on the right tab (chat / instant
        # callback / pick-a-time) instead of the default chat view.
        "Call Delay Minutes": str(sca.get("warmup_delay_minutes", 10)),
        "Callback Link": f"{retry_link}?tab=callback" if retry_link else "",
        "Pick Time Link": f"{retry_link}?tab=schedule" if retry_link else "",
    }


def _format_date(iso: str, tz_name: Optional[str] = None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        dt = dt.astimezone(_resolve_tz(tz_name))
        return dt.strftime("%A, %b %-d")
    except Exception:
        return iso


def _format_time(iso: str, tz_name: Optional[str] = None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        dt = dt.astimezone(_resolve_tz(tz_name))
        return dt.strftime("%-I:%M %p %Z").strip()
    except Exception:
        return iso


# ===== HTML shell builders =====
def _is_zoomish(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return any(s in u for s in ("zoom.us", "meet.google.com", "teams.microsoft.com", "webex.com"))


def _build_appointment_card(candidate: Dict[str, Any], settings: Dict[str, Any]) -> str:
    """If the candidate has an appointment + meeting link, render a callout card."""
    appt_at = candidate.get("appointment_at") or ""
    join_url = candidate.get("appointment_link") or ""
    if not appt_at:
        return ""
    region = (settings or {}).get("region_language", {}) or {}
    tz_name = region.get("timezone") or default_tz_name()
    date_str = _format_date(appt_at, tz_name)
    time_str = _format_time(appt_at, tz_name)

    # Custom etiquette block — recruiters can override per-pipeline if they want
    profile = (settings or {}).get("recruiter_profile") or {}
    etiquette = profile.get("interview_etiquette") or (
        "<strong>Quick interview tips:</strong> Join 2 minutes early so you can sort out audio/video. "
        "Pick a quiet, well-lit spot — windows behind you wash out the camera. Test your mic + camera "
        "in Zoom settings beforehand. Have your résumé open and a glass of water nearby. Smile, breathe, "
        "and remember: we're rooting for you. 💜"
    )

    cta_block = ""
    link_block = ""
    import html as _html
    join_url = _html.escape(join_url, quote=True) if join_url else ""
    if join_url:
        cta_block = f"""
        <tr><td style="padding-top:14px">
            <a href="{join_url}" target="_blank"
               style="display:inline-block;background-color:{BRAND['primary']};background:{BRAND['grad_btn']};color:#FFFFFF;text-decoration:none;
                      padding:13px 26px;border-radius:999px;font-weight:700;font-size:14px;
                      letter-spacing:.01em;font-family:'Space Grotesk',Arial,sans-serif;">
              Join interview →
            </a>
        </td></tr>
        """
        link_block = f"""
        <tr><td style="padding-top:10px;font-family:Arial,sans-serif;">
            <div style="font-size:11px;color:{BRAND['ink_muted']};text-transform:uppercase;letter-spacing:.16em;font-weight:600;margin-bottom:4px;">
              Or paste this link into your browser
            </div>
            <a href="{join_url}" target="_blank"
               style="font-size:13px;color:{BRAND['primary']};text-decoration:underline;word-break:break-all;">
              {join_url}
            </a>
        </td></tr>
        """

    return f"""
    <table role="presentation" cellpadding="0" cellspacing="0" border="0"
           style="width:100%;margin:24px 0;background:linear-gradient(135deg,{BRAND['primary']}0d 0%,{BRAND['accent']}0d 100%);
                  border:1px solid {BRAND['border']};border-radius:16px;overflow:hidden;">
        <tr>
            <td style="padding:20px 24px;font-family:Arial,sans-serif;">
                <div style="font-size:11px;font-weight:700;color:{BRAND['primary']};letter-spacing:.18em;
                            text-transform:uppercase;margin-bottom:8px;">
                    📅 Your interview
                </div>
                <div style="font-size:20px;font-weight:700;color:{BRAND['ink']};line-height:1.3;">
                    {date_str}
                </div>
                <div style="font-size:16px;color:{BRAND['ink_muted']};margin-top:4px;">
                    {time_str}
                </div>
                <table role="presentation" cellpadding="0" cellspacing="0" border="0">
                    {cta_block}
                    {link_block}
                </table>
            </td>
        </tr>
        <tr>
            <td style="padding:14px 24px 18px;background:#FFFFFFaa;border-top:1px solid {BRAND['border']};
                       font-family:Arial,sans-serif;font-size:13px;line-height:1.55;color:{BRAND['ink']};">
                {etiquette}
            </td>
        </tr>
    </table>
    """


# Templates where the candidate is being told a confirmed slot — booking it for
# the first time or moving it to a new time. Reminders/tentative holds don't get
# an .ics: re-attaching one on every reminder would just clutter the thread, and
# a "pencil_in" slot isn't confirmed yet.
ICS_INVITE_TEMPLATE_KEYS = {"approval", "approval_unscreened", "appointment_rescheduled"}

# Map links, addresses and office keys live in the company profile
# (backend/company_profile.json → offices). Used by the STARTER email (so new
# starters can find the office) and the inbound voice agent. Deliberately NOT
# added to booking/reschedule emails. Some map apps pin a street address on the
# wrong building — give each office whichever link drops the pin on your door.


def office_key_from_pipeline(pipe: Dict[str, Any]) -> str:
    """Resolve an office key from a pipeline doc: cg1_office_key first, then
    the office whose match words appear in the slug/name — the same rule the
    starter-email + new-hire-sheet paths use. '' when nothing matches."""
    import company_profile
    return company_profile.office_key_for_pipeline(pipe)


def office_maps_link(office_key: str) -> str:
    """Google Maps link for an office key, or '' when none exists."""
    import company_profile
    return company_profile.office_maps_link(office_key)


def office_apple_maps_link(office_key: str) -> str:
    """Apple Maps link for an office key, or '' when none exists."""
    import company_profile
    return company_profile.office_apple_maps_link(office_key)


def _ics_escape(text: str) -> str:
    """Escape TEXT values per RFC 5545 §3.3.11 (backslash, semicolon, comma, newline)."""
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _build_ics_invite(candidate: Dict[str, Any], settings: Dict[str, Any],
                      duration_minutes: Optional[int] = None) -> Optional[bytes]:
    """Render a minimal VCALENDAR/VEVENT so the confirmation email can carry a
    one-tap "add to calendar" attachment. METHOD:PUBLISH (not REQUEST) — we want
    a plain calendar entry, not an RSVP/organizer-attendee invite that prompts
    accept/decline and gets weird on reschedule."""
    appt_at = candidate.get("appointment_at")
    if not appt_at:
        return None
    try:
        start = datetime.fromisoformat(appt_at.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None
    end = start + timedelta(minutes=int(duration_minutes or 45))
    stamp_fmt = "%Y%m%dT%H%M%SZ"

    profile = (settings or {}).get("recruiter_profile") or {}
    sca = (settings or {}).get("screen_call_agent") or {}
    company = profile.get("company_name") or sca.get("company_name") or (os.environ.get("SENDGRID_FROM_NAME") or company_profile.company_name())
    recruiter = candidate.get("appointment_recruiter") or "the hiring team"
    join_url = candidate.get("appointment_link") or ""
    domain = os.environ.get("EMAIL_REPLY_DOMAIN") or "cgrecruit.invalid"

    description_lines = [f"Interview with {recruiter} ({company})."]
    if join_url:
        description_lines.append(f"Join: {join_url}")
    portal_base = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
    if portal_base and candidate.get("public_token"):
        description_lines.append(f"Details & prep: {portal_base}/applicant/{candidate['public_token']}")
    description = _ics_escape("\n".join(description_lines))

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//{_ics_escape(company)}//Interview Scheduler//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        # Stable per-candidate UID — a reschedule resends the same UID so calendar
        # apps that support updates (Apple/Outlook) replace the entry instead of
        # duplicating it.
        f"UID:appt-{candidate.get('id')}@{domain}",
        f"DTSTAMP:{datetime.now(timezone.utc).strftime(stamp_fmt)}",
        f"DTSTART:{start.strftime(stamp_fmt)}",
        f"DTEND:{end.strftime(stamp_fmt)}",
        f"SUMMARY:{_ics_escape(f'Interview with {company}')}",
        f"DESCRIPTION:{description}",
    ]
    if join_url:
        lines.append(f"LOCATION:{_ics_escape(join_url)}")
    lines += [
        "STATUS:CONFIRMED",
        "TRANSP:OPAQUE",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def _strip_inline_link_from_body(body_html: str, url: str) -> str:
    """If the body contains the meeting URL or the literal word 'Join:', remove that line —
    we surface it more prominently in the appointment card. Keep the rest of the body intact."""
    if not url or not body_html:
        return body_html
    # Remove lines that contain the URL itself or the prefixed "Join:" pattern
    lines = body_html.split("<br>")
    keep = []
    for ln in lines:
        plain = re.sub(r"<[^>]+>", "", ln).strip()
        if url and url in ln:
            continue
        if plain.lower().startswith("join:") or plain.lower() == "join":
            continue
        if plain.lower() in ("date:", "time:") or re.match(r"^date:|^time:", plain.lower()):
            # Drop the bare "Date: ..." / "Time: ..." lines too — they're now in the card
            continue
        keep.append(ln)
    return "<br>".join(keep)


def build_email_html(
    body: str,
    candidate: Dict[str, Any],
    settings: Dict[str, Any],
    template_key: str = "",
) -> str:
    """Wrap the user's editable text body in a polished, branded HTML shell.

    The body string is the user's template content (already rendered with placeholders).
    We:
      • escape minimally + linkify
      • inject an appointment card if relevant
      • add a branded header + footer
    """
    profile = (settings or {}).get("recruiter_profile") or {}
    sca = (settings or {}).get("screen_call_agent") or {}
    company = profile.get("company_name") or sca.get("company_name") or (os.environ.get("SENDGRID_FROM_NAME") or company_profile.company_name())
    sender_name = profile.get("recruiter_name") or "The Hiring Team"
    address = profile.get("company_address") or ""

    # Convert the plain-text body to safe HTML: escape everything (quotes too,
    # so a URL can't break out of its href), linkify URLs, keep paragraphs.
    # The body includes candidate-typed values (names from the public apply
    # and referral forms), so it is never trusted as HTML.
    import html as _html
    safe = _html.escape(body or "", quote=True)
    safe = re.sub(
        r"(https?://[^\s<>\"']+)",
        lambda m: f'<a href="{m.group(1)}" style="color:{BRAND["primary"]};text-decoration:underline" target="_blank">{m.group(1)}</a>',
        safe,
    )
    body_html = safe.replace("\n\n", "<br><br>").replace("\n", "<br>")

    # Only show the "Your Interview" callout card on templates that are *about*
    # an upcoming appointment. Other templates (warmup, screening_retry,
    # rejection, form, no-show, close_success after a candidate is hired but
    # no longer interviewing, etc.) should NOT include it — even if the
    # candidate happens to have an appointment_at on file from a prior stage.
    APPOINTMENT_TEMPLATE_KEYS = {
        "approval",                     # interview booked (initial booking, retries, auto-promote)
        "appointment_rescheduled",      # interview moved to a new slot
        "pencil_in",                    # tentative slot
        "appointment_reminder_1h",      # 1-hour reminder
        "appointment_reminder_10m",     # 10-minute reminder
    }
    show_appt_card = (template_key in APPOINTMENT_TEMPLATE_KEYS)
    appt_card = _build_appointment_card(candidate, settings) if show_appt_card else ""
    if appt_card and candidate.get("appointment_link"):
        body_html = _strip_inline_link_from_body(body_html, candidate.get("appointment_link"))

    # For appointment emails, pull the reschedule URL out of the body and render
    # it as a subtle footer BELOW the appointment card so the join button is
    # always the most prominent CTA.
    reschedule_footer = ""
    if appt_card and candidate.get("public_token"):
        base_url = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
        reschedule_url = f"{base_url}/reschedule/{candidate['public_token']}"
        body_html = _strip_inline_link_from_body(body_html, reschedule_url)
        reschedule_footer = (
            f'<div style="margin-top:16px;font-size:13px;color:{BRAND["ink_muted"]};line-height:1.6;">'
            f'Need to reschedule? <a href="{reschedule_url}" style="color:{BRAND["primary"]};text-decoration:underline;" target="_blank">Pick a new time here.</a>'
            f'</div>'
        )

    # Screening emails: the retry link renders as a branded button, not a raw
    # URL. The template keeps [Retry Link] on its own line so plain-text
    # clients still get a tappable URL; here the button replaces that line IN
    # PLACE — appending it after the body put the only continue-CTA below the
    # sign-off, and the first live nudge email read as having no link at all.
    SCREENING_BUTTON_TEMPLATE_KEYS = {
        "warmup_chat_first", "warmup", "warmup_offhours", "screening_retry",
        "retry_chat_nudge", "reengage_chat",
    }
    if template_key in SCREENING_BUTTON_TEMPLATE_KEYS and candidate.get("public_token"):
        base_url = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
        retry_url = f"{base_url}/retry/{candidate['public_token']}"
        if retry_url in (body or ""):
            button = (
                f'<div style="margin:14px 0;">'
                f'<a href="{retry_url}" target="_blank"'
                f' style="display:inline-block;background-color:{BRAND["primary"]};background:{BRAND["grad_btn"]};color:#FFFFFF;text-decoration:none;'
                f'padding:14px 32px;border-radius:999px;font-weight:700;font-size:15px;'
                f"letter-spacing:.01em;font-family:'Space Grotesk',Arial,sans-serif;\">"
                f'Complete screening →</a></div>'
            )
            lines = body_html.split("<br>")
            replaced = False
            out_lines = []
            for ln in lines:
                if not replaced and retry_url in ln:
                    out_lines.append(button)
                    replaced = True
                else:
                    out_lines.append(ln)
            body_html = "<br>".join(out_lines)
            if not replaced:
                body_html += button

    year = datetime.utcnow().year
    preheader = _html.escape((body or "").splitlines()[0][:90] if body else (company or ""), quote=True)
    company = _html.escape(company or "", quote=True)
    sender_name = _html.escape(sender_name or "", quote=True)
    address = _html.escape(address or "", quote=True)
    first_name_html = _html.escape(candidate.get("first_name") or "there", quote=True)
    address_block = f'<div style="font-size:11px;color:{BRAND["ink_muted"]};margin-top:6px">{address}</div>' if address else ""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{company}</title>
</head>
<body style="margin:0;padding:0;background:{BRAND['bg']};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;">
<div style="display:none;max-height:0;overflow:hidden;">{preheader}</div>
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:{BRAND['bg']};padding:32px 16px;">
  <tr><td align="center">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600"
           style="max-width:600px;width:100%;background:{BRAND['card']};border-radius:16px;
                  overflow:hidden;box-shadow:0 4px 24px rgba(15,15,18,0.06);">
      <!-- Brand header — signature gradient (bgcolor fallback for
           clients that don't render CSS gradients, e.g. old Outlook) -->
      <tr>
        <td bgcolor="{BRAND['primary_dark']}"
            style="background-color:{BRAND['primary_dark']};background:{BRAND['grad']};
                   padding:24px 32px;color:#FFFFFF;">
          <div style="font-size:11px;font-weight:700;letter-spacing:.22em;text-transform:uppercase;opacity:.9;">
            {company}
          </div>
          <div style="font-size:19px;font-weight:700;margin-top:4px;letter-spacing:-0.01em;font-family:'Space Grotesk',Arial,sans-serif;">
            Hi {first_name_html} 👋
          </div>
        </td>
      </tr>
      <!-- Body -->
      <tr>
        <td style="padding:28px 32px 8px;color:{BRAND['ink']};font-size:15px;line-height:1.65;">
          {body_html}
          {appt_card}
          {reschedule_footer}
        </td>
      </tr>
      <!-- Sign-off -->
      <tr>
        <td style="padding:0 32px 28px;color:{BRAND['ink']};font-size:14px;line-height:1.5;">
          <div style="margin-top:12px;">— {sender_name}</div>
        </td>
      </tr>
      <!-- Footer -->
      <tr>
        <td style="background:{BRAND['bg']};padding:20px 32px;border-top:1px solid {BRAND['border']};
                   font-size:11px;color:{BRAND['ink_muted']};line-height:1.5;font-family:Arial,sans-serif;">
          <div>You're receiving this because you applied for a role at {company}.</div>
          {address_block}
          <div style="margin-top:6px">© {year} {company}</div>
        </td>
      </tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""


async def send_email_via_sendgrid(
    to_email: str,
    subject: str,
    body: str,
    candidate: Optional[Dict[str, Any]] = None,
    settings: Optional[Dict[str, Any]] = None,
    template_key: str = "",
    reply_to: Optional[str] = None,
    appointment_duration_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    api_key = os.environ.get("SENDGRID_API_KEY", "")
    from_email = (os.environ.get("SENDGRID_FROM_EMAIL") or "").strip()
    profile = ((settings or {}).get("recruiter_profile") or {})
    sca_settings = ((settings or {}).get("screen_call_agent") or {})
    from_name = profile.get("company_name") or sca_settings.get("company_name") or (os.environ.get("SENDGRID_FROM_NAME") or company_profile.company_name())
    if not api_key:
        return {"status": "skipped", "reason": "SENDGRID_API_KEY not configured"}
    if not from_email:
        # A verified SendGrid sender is account-specific — there is no safe default.
        return {"status": "skipped", "reason": "SENDGRID_FROM_EMAIL not configured"}
    try:
        html_body = build_email_html(body or "", candidate or {}, settings or {}, template_key=template_key)
        message = Mail(
            from_email=(from_email, from_name),
            to_emails=to_email,
            subject=subject,
            plain_text_content=body,
            html_content=html_body,
        )
        if reply_to:
            from sendgrid.helpers.mail import ReplyTo
            message.reply_to = ReplyTo(reply_to)
        if template_key in ICS_INVITE_TEMPLATE_KEYS:
            ics_bytes = _build_ics_invite(candidate or {}, settings or {}, appointment_duration_minutes)
            if ics_bytes:
                message.attachment = Attachment(
                    FileContent(base64.b64encode(ics_bytes).decode("ascii")),
                    FileName("interview-invite.ics"),
                    FileType("text/calendar"),
                    Disposition("attachment"),
                )
        client = SendGridAPIClient(api_key)
        resp = client.send(message)
        return {"status": "sent", "code": resp.status_code, "message_id": resp.headers.get("X-Message-Id", "")}
    except Exception as e:
        logger.exception(f"SendGrid send failed: {e}")
        return {"status": "failed", "error": str(e)}


def get_template_for_key(settings_doc: Dict[str, Any], key: str) -> Dict[str, Any]:
    """Return {subject, body, sms_body, enabled, email_enabled, sms_enabled} for
    a template key. For built-in keys we fall back to the bundled defaults; for
    user-created custom-reminder keys we use whatever the recruiter saved (no
    defaults exist for those)."""
    from models import get_default_templates  # local import to avoid cycles
    defaults = get_default_templates()
    custom = ((settings_doc or {}).get("applicant_comms") or {}).get("templates") or {}
    default_tpl = defaults.get(key)
    user_tpl = custom.get(key) or {}
    enabled = user_tpl.get("enabled")
    enabled_val = True if enabled is None else bool(enabled)
    # Per-channel flags default to True when missing (backwards-compatible
    # for settings docs saved before v30).
    email_enabled = user_tpl.get("email_enabled")
    sms_enabled = user_tpl.get("sms_enabled")
    if default_tpl is None:
        # User-created custom reminder — use the user's saved values directly.
        return {
            "subject": user_tpl.get("subject") or "",
            "body": user_tpl.get("body") or "",
            "sms_body": user_tpl.get("sms_body") or "",
            "enabled": enabled_val,
            "email_enabled": True if email_enabled is None else bool(email_enabled),
            "sms_enabled": True if sms_enabled is None else bool(sms_enabled),
        }
    use_custom = bool(user_tpl.get("use_custom"))
    return {
        "subject": (user_tpl.get("subject") if use_custom and user_tpl.get("subject") else default_tpl.subject),
        "body": (user_tpl.get("body") if use_custom and user_tpl.get("body") else default_tpl.body),
        "sms_body": (user_tpl.get("sms_body") if use_custom and user_tpl.get("sms_body") else default_tpl.sms_body),
        "enabled": enabled_val,
        "email_enabled": True if email_enabled is None else bool(email_enabled),
        "sms_enabled": True if sms_enabled is None else bool(sms_enabled),
    }


def is_template_channel_enabled(tpl_or_settings, channel: str, key: Optional[str] = None) -> bool:
    """True if `channel` ('email' or 'sms') is enabled for the given template.
    Accepts either an already-resolved template dict (from get_template_for_key)
    or a (settings_doc, key) pair so callers can short-circuit BEFORE rendering.

    Legacy semantics: if `enabled=False` everything is off, regardless of the
    per-channel flags. New per-channel flags (`email_enabled`, `sms_enabled`)
    default to True when missing — so older settings docs that never set them
    still fire over both channels."""
    if key is not None:
        tpl = get_template_for_key(tpl_or_settings, key)
    else:
        tpl = tpl_or_settings or {}
    if tpl.get("enabled") is False:
        return False
    flag = "email_enabled" if channel == "email" else "sms_enabled"
    return tpl.get(flag) is not False
