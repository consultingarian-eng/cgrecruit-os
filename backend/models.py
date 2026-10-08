"""Models for CGRecruit."""
from pydantic import BaseModel, Field, EmailStr, ConfigDict
from typing import List, Optional, Dict, Any, Literal
from datetime import datetime, timezone, timedelta
import uuid

import company_profile
from app_tz import app_zone, default_tz_name  # APP_TIMEZONE (noqa: F401)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


# ===== Auth =====
class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10)  # auth_service.MIN_PASSWORD_LENGTH
    name: str
    company: Optional[str] = None


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class User(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=new_id)
    email: str
    name: str
    company: Optional[str] = None
    # Roles:
    #   "super_admin" — owns the tenant, full infra access.
    #   "recruiter"   — sub-account scoped to one or more pipelines; can move/hire candidates.
    #   "viewer"      — read-only sub-account; sees only candidates they added; cannot move/hire/delete.
    role: Literal["super_admin", "recruiter", "viewer", "analyst"] = "super_admin"
    # For recruiters/viewers: the super-admin whose data they access.
    parent_user_id: Optional[str] = None
    # For recruiters/viewers: list of pipeline IDs they can see/manage.
    pipeline_ids: List[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now_iso)


# ===== Pipeline (a pipeline = a hiring board / location) =====
class AvailabilityRule(BaseModel):
    weekday: int = 0  # 0=Mon .. 6=Sun
    start: str = "09:00"
    end: str = "12:00"
    slot_minutes: int = 30
    capacity: int = 50  # max candidates that can book each generated sub-slot
    # Per-slot meeting link. When set, bookings landing on this slot use THIS
    # link in every confirmation/reminder comm instead of pipeline.appointment_link.
    appointment_link_override: str = ""
    # Per-slot interviewer. Four different people run interviews and it changes
    # week to week — one generic name per pipeline misnames three of them. When
    # set, bookings landing on this slot show THIS name on the portal, the
    # confirmations and the calendar invite instead of pipeline.appointment_recruiter.
    appointment_recruiter_override: str = ""


class AvailabilityBlackout(BaseModel):
    id: str = Field(default_factory=new_id)
    start: str  # ISO datetime
    end: str
    reason: str = ""


class PipelineCreate(BaseModel):
    name: str
    description: Optional[str] = ""
    twilio_phone_number: Optional[str] = ""
    public_slug: Optional[str] = ""
    elevenlabs_agent_id_override: Optional[str] = ""
    elevenlabs_phone_number_id_override: Optional[str] = ""
    voice_id_override: Optional[str] = ""
    additional_context_override: Optional[str] = ""
    first_message_override: Optional[str] = ""
    agent_name_override: Optional[str] = ""
    calling_mode: Optional[str] = ""  # "" / "managed" / "twiml_bridge"
    appointment_link: Optional[str] = ""  # Zoom/Meet/Teams link sent in the booking confirmation email
    appointment_recruiter: Optional[str] = ""  # Name shown alongside the appointment ("with Sam K.")
    availability_rules: Optional[List[AvailabilityRule]] = None
    availability_blackouts: Optional[List[AvailabilityBlackout]] = None
    appointment_duration_minutes: Optional[int] = 45
    cg1_office_key: Optional[str] = ""


def _default_avail_rules() -> List[AvailabilityRule]:
    """Default Mon–Fri 9–12 + 13–18, 30-min slots."""
    rules = []
    for d in range(0, 5):
        rules.append(AvailabilityRule(weekday=d, start="09:00", end="12:00", slot_minutes=30))
        rules.append(AvailabilityRule(weekday=d, start="13:00", end="18:00", slot_minutes=30))
    return rules


class Pipeline(BaseModel):
    id: str = Field(default_factory=new_id)
    user_id: str
    name: str
    description: str = ""
    twilio_phone_number: str = ""
    public_slug: str = ""
    elevenlabs_agent_id_override: str = ""
    elevenlabs_phone_number_id_override: str = ""
    voice_id_override: str = ""
    additional_context_override: str = ""
    first_message_override: str = ""
    agent_name_override: str = ""
    calling_mode: str = "managed"  # "managed" (ElevenLabs Phone Numbers) or "twiml_bridge" (BYO caller ID)
    # Dedicated ElevenLabs agent for no-show revival calls (created via POST /api/revival/agent/sync).
    revival_agent_id: str = ""
    # Dedicated ElevenLabs agent that ANSWERS inbound calls to this office's line
    # (created via POST /api/inbound/agent/sync). Handles booked/starting
    # candidates who call to ask for the address or to reschedule. Per-office so
    # each office answers with its own address + slots.
    inbound_agent_id: str = ""
    # Physical office address the inbound agent reads out ("we're at ...").
    # Offices with a google_maps_link in the company profile also get that link.
    office_address: str = ""
    appointment_link: str = ""  # Zoom/Meet/Teams link sent on booking confirmation
    appointment_recruiter: str = ""  # Name shown on confirmation
    availability_rules: List[AvailabilityRule] = Field(default_factory=_default_avail_rules)
    availability_blackouts: List[AvailabilityBlackout] = Field(default_factory=list)
    appointment_duration_minutes: int = 45
    # Office key — ties this pipeline to an office in backend/company_profile.json
    # (and to the matching office in the optional CG1 field app), e.g. 'downtown'.
    cg1_office_key: str = ""
    created_at: str = Field(default_factory=now_iso)


# ===== Job =====
class JobCreate(BaseModel):
    pipeline_id: str
    title: str
    category: Optional[str] = ""
    description: Optional[str] = ""
    city: Optional[str] = ""
    region: Optional[str] = ""
    country: Optional[str] = ""
    postcode: Optional[str] = ""
    is_active: bool = True


class Job(BaseModel):
    id: str = Field(default_factory=new_id)
    user_id: str
    pipeline_id: str
    title: str
    category: str = ""
    description: str = ""
    city: str = ""
    region: str = ""
    country: str = ""
    postcode: str = ""
    is_active: bool = True
    created_at: str = Field(default_factory=now_iso)


# ===== Candidate =====
STAGES = ["APPLICANT", "SCREENING", "APPOINTMENT", "FORM", "CLOSE", "TRAINING"]
SCREENING_STATUSES = ["pending", "queued", "in_progress", "approved", "strong", "no_answer", "didnt_connect", "incomplete_info", "rejected"]


class CandidateCreate(BaseModel):
    pipeline_id: str
    job_id: Optional[str] = None
    first_name: str
    last_name: str = ""
    email: Optional[str] = ""
    phone: Optional[str] = ""
    skip_warmup: bool = False


class Candidate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=new_id)
    user_id: str
    pipeline_id: str
    job_id: Optional[str] = None
    first_name: str
    last_name: str = ""
    email: str = ""
    phone: str = ""
    stage: str = "SCREENING"
    screening_status: str = "pending"
    appointment_at: Optional[str] = None
    appointment_recruiter: Optional[str] = None
    appointment_link: Optional[str] = None
    # Attendance state set by the recruiter after the appointment date passes.
    # null → not yet recorded; "attended_form" / "attended_no_form" / "no_show"
    attendance_status: Optional[str] = None
    # Who recorded the outcome and when. Set by both the attendance endpoint and
    # the stage-move gate. Lets a promptly-marked session be told apart from one
    # reconstructed days later — which matters while we are still establishing
    # whether an office-to-office attendance gap is real or a recording artefact.
    attendance_recorded_at: Optional[str] = None
    attendance_recorded_by: Optional[str] = None
    # Stamped when the two-hours-after nudge has gone out, so the reminder to
    # mark a session off is asked once and not every hour until someone does.
    attendance_nudge_sent_at: Optional[str] = None
    # Set by screening_start: which mode handled this candidate's arrival, and —
    # when no screening call was placed — why not. Previously that only existed
    # as a log line, which is how candidates ended up stranded unnoticed.
    screening_mode_used: Optional[str] = None
    screening_no_dial_reason: Optional[str] = None
    screening_start_error: Optional[str] = None
    # Speed to contact, the measurement the whole chat-first change is judged on.
    # Candidates are created the instant they apply, so the gap between these two
    # is effectively apply-to-engagement. Both are set once and never overwritten
    # — a candidate who replies, goes quiet and replies again a day later has one
    # first-reply time, not a moving one.
    first_outreach_at: Optional[str] = None
    first_reply_at: Optional[str] = None
    first_reply_channel: Optional[str] = None
    # How many chase texts have gone out. Replaces a single "have we nudged?"
    # flag that could never express a sequence.
    retry_chat_nudge_count: int = 0
    # ISO timestamp of the previous (no-show'd) appointment so we can show
    # "Rescheduled from X" badges and audit trails.
    previous_appointment_at: Optional[str] = None
    # Set true when this candidate has used the no-show reschedule portal.
    rescheduled: bool = False
    rating: int = 0  # 0-5
    smart_score: Optional[int] = None  # 0-100
    smart_score_rationale: Optional[str] = None
    verdict: Optional[str] = None  # strong|good|borderline|weak — set by AI after call
    call_summary: Optional[str] = None  # AI-generated summary of the screening call
    disqualification_reason: Optional[str] = None  # human-readable hard-gate failure label
    resume_url: Optional[str] = None
    resume_text: Optional[str] = None
    parsed_resume: Optional[Dict[str, Any]] = None  # {summary, skills, experience, education}
    public_token: str = Field(default_factory=lambda: uuid.uuid4().hex)
    form_responses: Optional[Dict[str, Any]] = None  # {qid: {question, answer}} since v25
    chat_log: List[Dict[str, Any]] = Field(default_factory=list)  # [{role, text, at}]
    # Number of screening attempts made (initial call + retries). Capped at 3.
    screening_attempts: int = 0
    # Set when a retry email/SMS has been auto-fired so we don't double-send.
    screening_retry_sent_at: Optional[str] = None
    # Timestamp of the candidate's most recent retry-chat message — lets the
    # abandoned-chat nudge sweep find stale chats via an indexed range query
    # instead of scanning chat_log. Set on every applicant-role retry turn.
    retry_chat_last_at: Optional[str] = None
    # Set once the abandoned-chat nudge has fired for this candidate so it
    # only ever sends once.
    retry_chat_nudge_sent_at: Optional[str] = None
    call_attempts: int = 0
    last_call_status: Optional[str] = None  # initiated|no_answer|completed|failed
    next_call_at: Optional[str] = None  # ISO timestamp for next scheduled retry/dial
    auto_dial: bool = True  # set False to opt this candidate out
    # Set true when recruiter clicks Hire on the form responses tab.
    hired: bool = False
    hired_at: Optional[str] = None
    form_submitted_at: Optional[str] = None
    form_decline_at: Optional[str] = None
    # Set when a TO CLOSE candidate is rejected (rejection email sent + archived).
    close_rejection_at: Optional[str] = None
    # Status of the CG1 outbound webhook on hire (sent / skipped / failed) — informational.
    cg1_webhook_status: Optional[str] = None
    # CG1 Training integration fields — set when candidate is moved to TRAINING.
    training_start_at: Optional[str] = None        # ISO datetime of their Day 1 start
    cg1_starter_email_id: Optional[str] = None    # CG1 starter_emails record ID (for attended callback)
    training_attended: bool = False                # set true when CG1 fires the attended callback
    training_attended_at: Optional[str] = None
    # Re-notify-when-slots-open watchlist (no-show candidates who didn't pick a slot).
    rebook_watchlist: bool = False
    rebook_watchlist_at: Optional[str] = None
    rebook_watchlist_notified_at: Optional[str] = None
    # Waiting for the booking window to roll forward. Set when a candidate wanted
    # to book and the window held nothing; the sweep texts them their booking page
    # once it does. `booking_retry_at` is when to look again, not when to send.
    booking_retry_at: Optional[str] = None
    booking_retry_sent_at: Optional[str] = None
    booking_retry_attempts: int = 0
    # v32 — delayed auto-promote: when an AI screening call lands a positive
    # verdict + booked slot, we schedule a 2-hour delayed promotion. The FE
    # shows a countdown + Cancel button so recruiters can override before it
    # fires. Cleared on manual move / cancel / fire.
    auto_promote_at: Optional[str] = None
    auto_promote_status: Optional[str] = None  # pending | fired | cancelled:<reason>
    # Soft-reject: when set, the candidate is hidden from the Kanban by
    # default, drops out of all auto-dial / retry queues, and is shown only
    # under the "Rejected" filter so recruiters can review or restore. Hard
    # delete still exists separately and removes the doc entirely.
    archived_at: Optional[str] = None
    archived_reason: Optional[str] = None  # free-text or one of: rejected, no_show_no_response, manual
    # No-show revival — the AI courtesy call that follows up on archived no-shows.
    revival_call_attempts: int = 0
    revival_last_attempt_at: Optional[str] = None
    revival_outcome: Optional[str] = None  # rebooked|declined|interested_no_booking|no_answer|voicemail
    revival_feedback: Optional[str] = None  # their stated reason for missing the appointment
    referred_by: Optional[str] = None  # employee name who referred this candidate
    # Set to the auth_user_id of whichever recruiter/viewer added this candidate.
    # Used to scope the Kanban for viewer-role users (they only see their own adds).
    added_by_user_id: Optional[str] = None
    # Permanent milestone timestamps — set on first arrival at each stage, never cleared.
    # Used by intelligence queries so moving a candidate backward doesn't lose funnel credit.
    moved_to_form_at: Optional[str] = None
    moved_to_close_at: Optional[str] = None
    moved_to_training_at: Optional[str] = None
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)


class CandidateMove(BaseModel):
    stage: Optional[str] = None
    screening_status: Optional[str] = None
    # v37 — supplied when the move is leaving a lapsed interview behind. The move
    # endpoint refuses such a move without one, because dragging a candidate on
    # from APPOINTMENT is precisely what used to destroy the answer: the
    # attendance buttons only render while the candidate sits in that stage, and
    # /move never wrote the field. Many booked interviews had no outcome, and
    # the ones that attended were the ones missing — you record a no-show because
    # there is nothing else to do with them, but an attendee gets dragged onward.
    attendance_status: Optional[str] = None
    appointment_at: Optional[str] = None
    appointment_recruiter: Optional[str] = None
    appointment_link: Optional[str] = None
    rating: Optional[int] = None
    send_email_template: Optional[str] = None  # comm key to trigger after move
    training_start_at: Optional[str] = None    # ISO datetime sent when moving to TRAINING
    # Per-person training time overrides (override the pipeline's stored template for this send only)
    monday_start: Optional[str] = None
    monday_end: Optional[str] = None
    tuesday_start: Optional[str] = None
    tuesday_end: Optional[str] = None
    # Set True when importing legacy records — skips starter email and SMS so nothing re-fires
    skip_notifications: Optional[bool] = False


# ===== Conversation (call transcript) =====
class Conversation(BaseModel):
    id: str = Field(default_factory=new_id)
    candidate_id: str
    user_id: str
    elevenlabs_conversation_id: Optional[str] = None
    twilio_call_sid: Optional[str] = None
    status: str = "initiated"  # initiated|in_progress|completed|failed
    duration_seconds: Optional[int] = None
    transcript: List[Dict[str, str]] = []  # [{role, text}]
    summary: Optional[str] = None
    suitability_score: Optional[int] = None
    # TwiML Bridge fields — populated when calling_mode == "twiml_bridge"
    agent_id: Optional[str] = None
    calling_mode: str = "managed"
    dynamic_variables: Optional[Dict[str, Any]] = None
    is_dnd_retry: bool = False  # True on the immediate DND-bypass redial
    # "screening" (default) or "no_show_revival" — post-call processing branches on this.
    call_type: str = "screening"
    # Snapshot of the candidate's (missed) appointment_at at dial time. A booking
    # during the call changes appointment_at, so post-call compares against this
    # to robustly detect a rebook.
    revival_prev_appointment_at: Optional[str] = None
    created_at: str = Field(default_factory=now_iso)


# ===== Communication log =====
class Communication(BaseModel):
    id: str = Field(default_factory=new_id)
    candidate_id: str
    user_id: str
    type: str  # email|sms
    template_key: str
    subject: Optional[str] = None
    body: str
    to_address: str
    status: str = "sent"  # sent|failed
    error: Optional[str] = None
    created_at: str = Field(default_factory=now_iso)


# ===== Notifications =====
class Notification(BaseModel):
    """In-app notification surfaced via the bell icon in the top nav. The
    `kind` field controls the icon + colour on the frontend; common kinds:
      - call.completed_positive  — Aria finished, candidate looks strong
      - call.completed_negative  — Aria finished, soft no
      - call.no_answer / call.voicemail — call attempt outcome
      - appointment.booked       — slot booked (via call OR portal OR web voice)
      - appointment.no_show      — candidate didn't attend their booked slot
      - candidate.applied        — new public-portal application landed
      - candidate.duplicate      — cross-pipeline duplicate detected
      - candidate.replied_chat   — candidate replied on the retry chat (web)
    Most notifications carry a `link` deep into the app (e.g. open this
    candidate's drawer)."""
    id: str = Field(default_factory=new_id)
    user_id: str
    kind: str
    title: str
    body: str = ""
    link: Optional[str] = None  # frontend route, e.g. /?candidate=abc
    candidate_id: Optional[str] = None
    pipeline_id: Optional[str] = None
    read: bool = False
    created_at: str = Field(default_factory=now_iso)
    read_at: Optional[str] = None


# ===== Settings =====
class RecruiterProfile(BaseModel):
    # Defaults come from backend/company_profile.json; each pipeline can override
    # them in Settings → Recruiter Profile.
    company_name: str = Field(default_factory=company_profile.company_name)
    city: str = ""
    job_role: str = ""
    recruiter_email: str = Field(default_factory=lambda: company_profile.get("contact_email"))
    phone: str = ""
    website: str = Field(default_factory=lambda: company_profile.get("website"))
    logo_url: str = Field(default_factory=lambda: company_profile.get("logo_url"))
    social_links: Dict[str, str] = {}
    interview_etiquette: str = ""  # appended to interview confirmation email; HTML allowed


# Company specifics for the default AI-recruiter scripts below. They come from
# backend/company_profile.json (role_title, work_schedule, about_company,
# pay_summary) and are only starting values: every one of these fields is
# editable per pipeline in Settings → Screen Call Agent.
_ROLE_TITLE = company_profile.get("role_title", "Sales Representative")
_WORK_SCHEDULE = company_profile.get("work_schedule", "10:00 AM to 6:00 PM, Monday to Friday")


def _default_additional_context() -> str:
    about = company_profile.get("about_company", "EDIT ME: two or three sentences about what your company does.")
    pay = company_profile.get("pay_summary", "EDIT ME: how the role is paid.")
    return (
        f"# About {company_profile.company_name()}\n"
        f"{about}\n\n"
        f"# About the role — {_ROLE_TITLE}\n"
        f"- **Hours**: {_WORK_SCHEDULE}.\n"
        f"- **Pay**: {pay}\n"
        "- **Training**: Full training and ongoing coaching provided — no prior sales\n"
        "  experience required.\n\n"
        "# Interview format\n"
        "The next step is a short video interview with our hiring managers."
    )


class ScreenCallAgentSettings(BaseModel):
    language: str = "English"
    custom_caller_id: str = ""  # legacy — superseded by sms_sender_number
    sms_sender_number: str = ""  # the SINGLE warmed Twilio number used for ALL outbound SMS
    sms_optout_disclosure: str = ""  # appended to first SMS to each candidate (TCPA / 10DLC)
    warmup_delay_minutes: int = 10
    # How a candidate is first screened. Until v37 this field existed but was read
    # by nothing — changing it in the UI had no effect at all. It is now the single
    # switch that decides the arrival sequence (see backend/screening_start.py).
    #
    #   voice_first  the historic behaviour — AI call `warmup_delay_minutes` after
    #                arrival, chat only ever offered after that call goes unanswered.
    #   chat_first   text + email with the booking link on arrival; the call follows
    #                `chat_first_delay_minutes` later only if they haven't replied.
    #   chat_only    no screening calls; the candidate self-serves or nothing happens.
    #
    # "voice_only" / "voice_and_chat" are the pre-v37 spellings and both still
    # resolve to voice_first — see resolve_screening_mode(). Old settings docs
    # therefore keep working untouched.
    screening_mode: Literal[
        "voice_first", "chat_first", "chat_only", "voice_only", "voice_and_chat"
    ] = "voice_first"
    # Only consulted when screening_mode == "chat_first". Two hours is long enough
    # for someone to see a text on a break and short enough that the call still
    # lands the same working day.
    chat_first_delay_minutes: int = 120
    # Under chat_first the outbound call is the FALLBACK when texts go unanswered:
    # a real screening call, using opening_message and the same questions, that
    # sounds like a phone call rather than "reply to our text". It follows the
    # ordinary voice flow — live screening if they pick up, voicemail_message if
    # not, and the normal retry cadence after. Nothing special is needed here;
    # the sequencing (arrival text → retry text → call) lives in the nudge
    # schedule and the dial delay, not in a separate script.
    voice: str = "Sarah"  # ElevenLabs voice display name
    voice_id: str = ""  # ElevenLabs voice_id (24-char) — required for sync to set TTS voice
    # TTS engine. eleven_v4_turbo (Oct 2026) is accepted on English agents and
    # starts speaking ~2x sooner than eleven_turbo_v2 — see voice_service.
    voice_model: str = "eleven_v4_turbo"
    # Valid ElevenLabs Convai LLM IDs (see GET /v1/convai/llm/list). Default
    # gemini-3.6-flash, synced with reasoning_effort "minimal" — fastest
    # replies in the Oct 2026 live test (voice_service.DEFAULT_VOICE_LLM).
    llm_model: str = "gemini-3.6-flash"
    elevenlabs_agent_id: str = ""  # your ElevenLabs agent id (Settings → Screen Call Agent)
    elevenlabs_phone_number_id: str = ""
    enable_appointment_booking: bool = True
    appointment_days_offered: int = 7
    # v32 — when an AI screening call lands a positive verdict + booked slot,
    # we schedule a delayed auto-promote to APPOINTMENT this many minutes later.
    # Recruiters can Approve early / Cancel during the window. Per-pipeline so
    # different offices can run different override SLAs (e.g. one office 120min,
    # another 240min on weekends).
    auto_promote_delay_minutes: int = 120
    # v34 — Native ElevenLabs voicemail detection. When enabled, the agent
    # automatically detects voicemail/answering machines via the platform's
    # built-in voicemail_detection system tool, leaves the configured message,
    # and hangs up. Per-pipeline so each office can run its own short script.
    voicemail_detection_enabled: bool = True
    voicemail_message: str = (
        "Hi, this is [Agent Name] from [Company] about your application — "
        "I'll try again shortly. Talk soon."
    )
    purpose: str = (
        "You are {{agent_name}}, a friendly, sharp recruiter on a screening call for the\n"
        f"**{_ROLE_TITLE}** role at **{{{{company}}}}** ({{{{city}}}} office).\n\n"
        "Your job is NOT to read a checklist. Your job is to have a real conversation\n"
        "that figures out three things:\n"
        "  1. Whether the candidate clears the four hard gates (age, work rights, hours, commute).\n"
        "  2. Whether they have the *raw ingredients* for face-to-face direct sales —\n"
        "     resilience, energy, people skills, self-motivation.\n"
        "  3. Whether they're a good cultural fit — confident, curious, coachable.\n\n"
        "# How to talk\n"
        "- Speak like a human on a phone — natural pauses, contractions, warm energy.\n"
        "- Use short affirmations: 'love that', 'got it', 'amazing', 'totally', '100%' — and vary "
        "them; never open two replies the same way. Over text: emoji at most once every few "
        "messages, never the same one twice.\n"
        "- Reference what they JUST said before moving on.\n"
        "- Match their energy. Quiet candidate? Slow down. Enthusiastic? Match it back.\n\n"
        "# Follow-ups — one question only\n"
        "Only dig deeper on Q6 (why sales — ask once what draws them to it). One follow-up,\n"
        "maximum. For every other question: acknowledge the answer and move on immediately.\n"
        "Do NOT probe, do NOT add 'and also...', do NOT ask multiple things in one turn.\n\n"
        "# Pacing\n"
        "You have about 3 minutes, and getting them booked matters more than getting a\n"
        "fuller answer. Hard gates (Q1-Q4): one word is enough, no extras. Q5-Q6: slightly\n"
        "warmer but still moving. Then book — do not invent extra questions to fill time.\n\n"
        "# No preamble\n"
        "Do not give an intro speech. No role description, no pitch about how the company\n"
        "works, no 'quick heads-up' paragraph, and never a second 'Ready?' — the role is\n"
        "explained at the interview, not here. Go straight into Q1 and keep moving. If the\n"
        "candidate asks about the role or whether you're an AI, answer honestly in one\n"
        "short sentence, then return to the questions."
    )
    opening_message: str = (
        "Hi {{first_name}}, this is {{agent_name}} from {{company}} — I'm calling about "
        f"your application for our {_ROLE_TITLE} role in {{{{city}}}}. Have I "
        "caught you at a good time for a quick 5-minute chat?"
    )
    screening_questions: List[Dict[str, Any]] = Field(default_factory=lambda: [
        {"question": "Awesome — first up, and I just have to ask: are you 18 or over?", "auto_screen": True},
        {"question": "And do you have the full right to work here? Just want to make sure you're not on a student visa.", "auto_screen": True},
        {"question": f"It's a full-time role - shifts run {_WORK_SCHEDULE}. Does that schedule work for you?", "auto_screen": True},
        {"question": "And we're recruiting specifically for our {{city}} office — can you reliably commute there 4+ days a week?", "auto_screen": True},
        {"question": "Great — so what are you looking for in your next role?", "auto_screen": False},
        {"question": "And what made you apply for a sales role in particular?", "auto_screen": False},
    ])
    # v37 — the screening was 11 questions and booking came after all of them, at
    # roughly turn 12. Candidates who sent only a couple of replies attended far
    # more often than those who sent eleven or more. A long conversation is
    # friction, not engagement.
    #
    # The four hard gates above are unchanged — they do real work and a failure
    # there should end things before a slot is spent. The two that follow are the
    # minimum needed to tell a serious applicant from a scattergun one.
    #
    # These five were removed from the call and now arrive as the post-booking form,
    # so nothing is lost — they simply stop standing between the candidate and a time:
    #   - Have you done anything customer-facing before?
    #   - Walk me through a time you faced real rejection.
    #   - How do you build rapport with a stranger?
    #   - What's driving you when you're at your best?
    #   - Describe yourself in three words.
    # The sixth removal, "this role is field-based ... is that the work style you're
    # looking for?", became a statement in the preamble above rather than a question.
    # It sets expectations and lets people self-select out, which is worth keeping —
    # but it never needed an answer recorded against it.
    booking_instructions: str = (
        "After question 6, transition straight into booking — DO NOT ask 'what day "
        "works for you'. Use this exact transition:\n\n"
        "  'Great stuff {{first_name}}, that's everything I needed! The next step "
        "is a quick 30-minute Zoom interview with our hiring manager — it's your chance "
        "to learn more about the role, meet the team, and ask any questions you have. "
        "Let me just grab the next available times for you…'\n\n"
        "Then immediately call `get_available_slots` and present the first two slots "
        "by their `label` field exactly as returned. Once the candidate confirms, say "
        "'Perfect, let me lock that in...' then call `book_slot` with the EXACT ISO string."
    )
    # No {{appointment_date}}/{{appointment_time}} here: the slot is booked
    # mid-call, so no such variable exists when the call starts. Olivia reads
    # the day and time back from the slot the candidate just confirmed.
    closing_message: str = (
        "Once the slot is booked, repeat the day and time back to them: "
        "'You're all booked in for <that day> at <that time> — brilliant! "
        "You'll receive a confirmation email and text within the next 5 minutes with the "
        "Zoom link, and we'll also send you a couple of reminders before your interview. "
        "Looking forward to meeting you, {{first_name}} — speak soon!'"
    )
    # The knowledge the agent answers questions from. Built from the company
    # profile; edit it per pipeline in Settings → Screen Call Agent.
    additional_context: str = Field(default_factory=lambda: _default_additional_context())
    agent_name: str = "Olivia"


# Canonical modes. The two legacy spellings both meant "the AI calls first", which
# is what the system did regardless of this field, so they map to voice_first.
ATTENDANCE_VALUES = ("attended_form", "attended_no_form", "no_show")


def parse_appointment_at(value: Any) -> Optional[datetime]:
    """Read an `appointment_at` whatever shape it was stored in, as tz-aware UTC.

    Bookings arrive from six code paths and the field is a naive ISO string in
    some and a real datetime in others, so anything reading it has to cope with
    both. Returns None rather than raising — an unparseable timestamp must not
    take down a sweep or block a stage move.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    # Naive values are Eastern-local by convention everywhere in this codebase,
    # but for "has this time passed?" the hour of slop that assuming UTC would
    # introduce doesn't change the answer — and guessing a timezone here would
    # be worse than not guessing one.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def local_slot_label(
    value: Any,
    tz_name: str = "",
    fmt: str = "%a %-d %b, %-I:%M %p",
) -> str:
    """An `appointment_at` written the way the people in the room say it.

    Slots are STORED in UTC (`availability_service` converts local → UTC before
    saving), so formatting one straight off `parse_appointment_at` prints the
    UTC clock: the outcome-due nudge announced a 9:15 AM session as the
    "1:15 PM interview", four hours after the room had emptied. Anything that
    shows a slot to a human goes through here.

    Naive values are left where they are rather than shifted — by convention
    those were written local — and an unparseable one returns "" so the caller
    can fall back to the raw string instead of losing the notification.
    """
    from zoneinfo import ZoneInfo

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return ""
    else:
        return ""
    if dt.tzinfo:
        try:
            dt = dt.astimezone(ZoneInfo(tz_name or default_tz_name()))
        except Exception:
            dt = dt.astimezone(app_zone())
    return dt.strftime(fmt)


def appointment_has_lapsed(value: Any, *, grace_minutes: int = 0) -> bool:
    """True when an appointment is far enough in the past to expect an outcome.

    Unreadable or missing timestamps return False: a candidate must never be
    blocked from moving because their appointment time couldn't be parsed.
    """
    dt = parse_appointment_at(value)
    if dt is None:
        return False
    return dt < datetime.now(timezone.utc) - timedelta(minutes=max(0, grace_minutes))


_LEGACY_SCREENING_MODES = {"voice_only": "voice_first", "voice_and_chat": "voice_first"}
SCREENING_MODES = ("voice_first", "chat_first", "chat_only")


def resolve_screening_mode(settings: Optional[Dict[str, Any]]) -> str:
    """The screening mode for a resolved settings doc, always one of SCREENING_MODES.

    Anything unrecognised — a typo, a value from a newer build, None — falls back
    to voice_first, i.e. exactly what the system did before this field was wired
    up. A settings doc that cannot be understood must never silently stop calling
    candidates; the safe failure here is the historic behaviour, not silence.
    """
    raw = ((settings or {}).get("screen_call_agent") or {}).get("screening_mode")
    mode = str(raw or "").strip().lower()
    mode = _LEGACY_SCREENING_MODES.get(mode, mode)
    return mode if mode in SCREENING_MODES else "voice_first"


class CommunicationTemplate(BaseModel):
    # Master enable (legacy v29 — when False both channels are off). The
    # per-channel flags below are the v30 source of truth and override this
    # field — older settings docs that only have `enabled` still work.
    enabled: bool = True
    # Per-channel enables. Default True so unconfigured templates still fire
    # over both channels (backwards-compatible).
    email_enabled: bool = True
    sms_enabled: bool = True
    use_custom: bool = False
    subject: str = ""
    body: str = ""
    sms_body: str = ""
    # For appointment-reminder templates only — minutes before the appointment
    # to fire. Ignored for non-reminder templates. Defaults: 60 for the 1h
    # reminder, 10 for the 10m reminders.
    lead_minutes_before_appointment: Optional[int] = None
    # When True the user added this entry via "Schedule another reminder" — it
    # has a custom display label and the auto-dialer schedules it as a one-off
    # appointment reminder using `lead_minutes_before_appointment`.
    is_custom_reminder: bool = False
    label: str = ""


COMM_TEMPLATE_KEYS = [
    # Pre-call
    "warmup",
    # v37 — the arrival message when screening_mode is chat_first. Distinct from
    # `warmup` because that one promises a call in ~10 minutes, which under
    # chat_first is untrue: the call is two hours away and only happens if they
    # don't reply. Leading with a promise the system won't keep is how you teach
    # candidates to ignore you.
    "warmup_chat_first",
    # Single template fires for ANY screening failure (no_answer, didn't connect,
    # incomplete info, no availability) — consolidated v34.5 from 4 separate
    # per-failure-mode templates that were never actually fired.
    "screening_retry",
    # One-shot revival of the voice-era backlog after a pipeline flips to
    # chat-first: candidates who exhausted the call cadence without ever
    # connecting get the "let's do it by text" opener (asks Q1 directly).
    # Fired manually from Settings → Text Screening, never automatically.
    "reengage_chat",
    # Stage moves
    # approval_unscreened: same booking confirmation, WITHOUT the "screening
    # was successful" claim — for the self-serve doors (portal slot grid,
    # reschedule page) where a candidate can grab a slot before any screening
    # has happened. It primes the gate-check text instead, so "four quick
    # checks ahead of your interview" reads as the plan, not a contradiction.
    "rejection", "approval", "approval_unscreened",
    "form", "form_decline", "close_success", "close_rejection",
    # Appointment lifecycle
    "appointment_reminder_1h", "appointment_reminder_10m", "appointment_no_show",
    "appointment_rescheduled",
    # Sent ONLY to candidates who haven't SMS-confirmed (reply Y) yet. Fires
    # ~3h before; shifts to 7 PM the evening before for morning appointments.
    "appointment_confirm_chaser",
    # Screening done but no slot agreed on call → send slot-picker link
    "slot_picker",
    # Told us by text/email they couldn't attend, then never picked a new time.
    # Distinct from slot_picker (which congratulates them on passing screening)
    # and from appointment_no_show (which implies they just didn't turn up).
    "appointment_cancelled_rebook",
    # No-show revival: first revival call hit voicemail/no-answer → email the
    # rebooking link. Fires at most once per candidate.
    "revival_followup",
    # Candidate started the retry-chat screening but went quiet mid-way —
    # nudged once after a few hours of inactivity. See dialer/retry_chat_nudge_sweep.py.
    "retry_chat_nudge",
]


class ApplicantCommsSettings(BaseModel):
    templates: Dict[str, CommunicationTemplate] = {}


class RegionLanguageSettings(BaseModel):
    timezone: str = Field(default_factory=lambda: default_tz_name())  # APP_TIMEZONE
    language: str = "English"
    date_format: str = "DD/MM/YYYY"


class AppointmentSettings(BaseModel):
    applicant_limit: int = 1
    appointment_type: Literal["virtual", "in_person"] = "virtual"
    email_reminder_minutes: int = 60
    sms_reminder_minutes: int = 30
    booking_days_offered: int = 7
    work_days_only: bool = True


class CustomFormQuestion(BaseModel):
    id: str = Field(default_factory=new_id)
    question: str
    answer_type: Literal["text", "multiple_choice", "date"] = "text"
    options: List[str] = []
    required: bool = True


class CustomFormSettings(BaseModel):
    questions: List[CustomFormQuestion] = Field(
        default_factory=lambda: [
            CustomFormQuestion(
                question="Confirm your full legal name (as it appears on government-issued ID).",
                answer_type="text",
                required=True,
            ),
            CustomFormQuestion(
                question="What's the best phone number to reach you on?",
                answer_type="text",
                required=True,
            ),
            CustomFormQuestion(
                question="What's your current address (street, town or city, postcode or ZIP)?",
                answer_type="text",
                required=True,
            ),
            CustomFormQuestion(
                question="Do you have reliable transportation to and from work?",
                answer_type="multiple_choice",
                options=["Yes — own vehicle", "Yes — public transport", "Yes — rideshare/other", "No"],
                required=True,
            ),
            CustomFormQuestion(
                question="Do you have the legal right to work in this country?",
                answer_type="multiple_choice",
                options=["Yes — citizen / permanent resident", "Yes — current work visa", "No / sponsorship needed"],
                required=True,
            ),
            CustomFormQuestion(
                question="Earliest date you could start if offered the role.",
                answer_type="date",
                required=True,
            ),
            CustomFormQuestion(
                question="Are you currently working another job? If yes, please describe.",
                answer_type="text",
                required=False,
            ),
            CustomFormQuestion(
                question="What attracted you to this role specifically?",
                answer_type="text",
                required=True,
            ),
            CustomFormQuestion(
                question="Any commitments or restrictions we should know about (school, second job, time off)?",
                answer_type="text",
                required=False,
            ),
            CustomFormQuestion(
                question="How did you originally hear about us?",
                answer_type="multiple_choice",
                options=["Indeed", "LinkedIn", "Instagram / Social media", "Referral", "Job fair", "Other"],
                required=False,
            ),
        ]
    )


class AutoDialerSettings(BaseModel):
    enabled: bool = True
    auto_dial_on_apply: bool = True
    max_retry_attempts: int = 3
    retry_delay_hours: int = 2  # fallback when retry_delays is not set
    retry_delays: List[int] = Field(default_factory=list)  # per-attempt delays in hours; index 0 = after 1st call
    batch_interval_seconds: int = 30  # seconds between calls in a batch
    call_window_start: str = "09:00"  # 24h
    call_window_end: str = "19:00"
    call_window_days: List[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5])  # 0=Mon..6=Sun
    sync_after_call_minutes: int = 8  # poll ElevenLabs after this many minutes
    pre_call_sms_enabled: bool = True
    pre_call_sms_minutes: int = 3  # send SMS this many minutes before the AI dial
    # v34.1 — Per-pipeline cap on simultaneous AI calls. Prevents bursts that
    # would exceed the ElevenLabs subscription's concurrency limit. The dialer
    # checks `count(screening_status="in_progress")` in this pipeline before
    # placing each queued call; if at cap it postpones and re-checks. Bump up
    # to whatever your plan supports (e.g. 20 on Business / 5 on Creator).
    max_concurrent_calls: int = 5


class BookingPreferences(BaseModel):
    slots_primary: int = 2       # how many slots AI offers up front
    slots_fallback: int = 1      # slots offered if all primary slots are rejected
    reschedule_link_days_ahead: int = 7   # days ahead shown on the public reschedule portal
    reschedule_retry_days: int = 3        # days before watchlist sweep re-notifies candidate
    # When the booking window (appointments.booking_days_offered) holds nothing
    # bookable, the candidate is not offered a distant slot — they are parked and
    # texted their booking page once the window has rolled far enough forward to
    # contain something. This is how long to wait before that send. Separate from
    # reschedule_retry_days: that one governs someone who HAD a slot and lost it.
    booking_retry_days: int = 2
    booking_retry_max_attempts: int = 3   # give up after this many silent rolls


class NoShowRevivalSettings(BaseModel):
    """No-show revival — AI courtesy calls to archived no-show candidates,
    offering to rebook them. Per-pipeline overridable. Ships disabled."""
    enabled: bool = False
    days_after_appointment: int = 7   # wait this long after the missed appointment
    max_attempts: int = 3             # total revival call attempts per candidate
    attempt_gap_hours: int = 24       # minimum hours between attempts
    lookback_days: int = 60           # never call no-shows older than this
    daily_cap: int = 15               # max revival calls per pipeline per day
    # Script controls — empty means use the built-in defaults. Both accept the
    # same [First Name]-style tokens as email templates, plus
    # [Original Appointment Date] and [Days Since Appointment].
    opener_line: str = ""             # overrides the agent's first message
    extra_instructions: str = ""      # appended to the system prompt verbatim
    # After the FIRST attempt ends in voicemail/no-answer, email the candidate
    # a rebooking link (template key: revival_followup). Fires once per candidate.
    followup_email_enabled: bool = True


class EmailIntakeSettings(BaseModel):
    enabled: bool = True
    inbound_domain: str = Field(default_factory=company_profile.inbound_parse_domain)
    default_pipeline_id: str = ""  # if no auto-detect match, fall back to this
    auto_dial: bool = True


class IntegrationsSettings(BaseModel):
    """Outbound integrations to push hired candidates to other apps."""
    cg1_webhook_url: str = ""  # POST URL receives {first_name, last_name, email, phone, appointment_at, hired_at, candidate_id}
    cg1_webhook_secret: str = ""  # sent as `X-Webhook-Secret` header; recipient should verify
    cg1_enabled: bool = False


class StageAiInstructions(BaseModel):
    """Per-stage AI instructions for SMS and email reply agents.
    Empty string = use the hardcoded default in training_sms.py / email_replies.py."""
    sms: str = ""
    email: str = ""


class AiStagePromptsSettings(BaseModel):
    APPLICANT: StageAiInstructions = Field(default_factory=StageAiInstructions)
    SCREENING: StageAiInstructions = Field(default_factory=StageAiInstructions)
    APPOINTMENT: StageAiInstructions = Field(default_factory=StageAiInstructions)
    FORM: StageAiInstructions = Field(default_factory=StageAiInstructions)
    CLOSE: StageAiInstructions = Field(default_factory=StageAiInstructions)
    TRAINING: StageAiInstructions = Field(default_factory=StageAiInstructions)


class Settings(BaseModel):
    user_id: str
    # v34.2 — Tenant-wide concurrent-call cap. This is the HARD ceiling matching
    # the user's ElevenLabs subscription (e.g. 20 on Business). Per-pipeline
    # `auto_dialer.max_concurrent_calls` is a SOFT pipeline-level cap that
    # cannot exceed this. The dialer enforces both: a queued call is only
    # placed when (a) per-pipeline cap not hit AND (b) tenant-wide live count
    # < tenant cap. Default 5 (Creator plan).
    tenant_max_concurrent_calls: int = 5
    recruiter_profile: RecruiterProfile = Field(default_factory=RecruiterProfile)
    screen_call_agent: ScreenCallAgentSettings = Field(default_factory=ScreenCallAgentSettings)
    applicant_comms: ApplicantCommsSettings = Field(default_factory=ApplicantCommsSettings)
    region_language: RegionLanguageSettings = Field(default_factory=RegionLanguageSettings)
    appointments: AppointmentSettings = Field(default_factory=AppointmentSettings)
    custom_form: CustomFormSettings = Field(default_factory=CustomFormSettings)
    auto_dialer: AutoDialerSettings = Field(default_factory=AutoDialerSettings)
    no_show_revival: NoShowRevivalSettings = Field(default_factory=NoShowRevivalSettings)
    email_intake: EmailIntakeSettings = Field(default_factory=EmailIntakeSettings)
    integrations: IntegrationsSettings = Field(default_factory=IntegrationsSettings)
    ai_stage_prompts: AiStagePromptsSettings = Field(default_factory=AiStagePromptsSettings)
    updated_at: str = Field(default_factory=now_iso)


# ===== Default email/sms templates =====
def get_default_templates() -> Dict[str, CommunicationTemplate]:
    return {
        "warmup": CommunicationTemplate(
            subject="Thanks for applying to [Role] at [Company]",
            body=(
                "Hi [First Name],\n\n"
                "Thanks for applying for the [Role Phrase] at [Company] in [City].\n\n"
                "You'll receive a call from [Agent Name], our AI screening assistant, in about [Call Delay Minutes] minutes.\n\n"
                "── We appreciate AI can feel a little impersonal. We use it because it means "
                "your application gets a response the same day you apply — no pile, no weeks of "
                "silence, and every candidate gets the exact same fair process. ──\n\n"
                "The call takes about 5 minutes. If all goes well, we'll book you straight into "
                "an interview with our hiring manager.\n\n"
                "Can't wait? Finish now by chat: [Retry Link]\n"
                "Prefer a different time? Pick one here: [Pick Time Link]\n\n"
                "Best,\n[Company]"
            ),
            sms_body="Hi [First Name], thanks for applying to [Role] at [Company]. Expect a call in about [Call Delay Minutes] min for a quick 5-min AI screening. Rather chat now or pick a time? [Retry Link]",
        ),
        # Chat-first arrival. The SMS is the one that matters — it is the first
        # thing the candidate sees and the only one carrying a link they can act
        # on from a phone in thirty seconds. Deliberately short: no AI disclaimer
        # paragraph, no role blurb, one action.
        "warmup_chat_first": CommunicationTemplate(
            subject="Thanks for applying to [Role] at [Company] — grab your interview slot",
            # Leads with the link, not "we've just texted you" — the same email
            # goes to candidates who have no phone (email intake), for whom no
            # text was ever sent. The link is true for everyone; the text is
            # mentioned as an alternative, not as a promise.
            body=(
                "Hi [First Name],\n\n"
                "Thanks for applying for the [Role Phrase] at [Company] in [City].\n\n"
                "Let's finish your application — a few quick questions and then you'll pick your "
                "interview slot. It takes about 3 minutes.\n\n"
                "[Retry Link]\n\n"
                "If you'd rather do it by text, check your phone — we've sent the same questions "
                "there too.\n\n"
                "── Those questions are asked by our AI assistant. We use it because it means "
                "your application gets a response the same day rather than sitting in a pile, "
                "and everyone gets the same process. ──\n\n"
                "Best,\n[Company]"
            ),
            # Opens with the first screening question rather than a link. Speed to
            # engagement is the whole point: a link is a decision to make later, a
            # question is something you answer on the spot, and answering it puts
            # the candidate inside the screening without them choosing to start one.
            # Kept strictly within the GSM-7 character set: no em dash, no curly
            # apostrophes, no emoji. A single character outside it switches the
            # whole message to UCS-2, which drops the segment size from 160 to 70
            # and turns one text into four. This is the message every applicant
            # gets, so that is four times the bill on the highest-volume send in
            # the system.
            sms_body=(
                "Hi [First Name], thanks for applying for the [Role Phrase] with "
                "[Company]. We'll just get through a few quick screening questions "
                "before booking you in for an interview. Just to confirm, are you 18 "
                "or over?"
            ),
        ),
        "warmup_offhours": CommunicationTemplate(
            subject="Thanks for applying to [Role] at [Company]",
            body=(
                "Hi [First Name],\n\n"
                "Thanks for applying for the [Role Phrase] at [Company] in [City].\n\n"
                "[Agent Name], our AI screening assistant, will give you a call between "
                "[Call Window Start] and [Call Window End]. The call takes about 5 minutes — "
                "if all goes well, we'll book you straight into an interview with our hiring manager.\n\n"
                "── We appreciate AI can feel a little impersonal. We use it because it means "
                "your application gets a response quickly — no pile, no weeks of silence, "
                "and every candidate gets the exact same fair process. ──\n\n"
                "Don't want to wait? Chat now: [Retry Link]\n"
                "Or request an instant callback right now: [Callback Link]\n"
                "Rather have us call at a specific time tomorrow? Pick one: [Pick Time Link]\n\n"
                "Best,\n[Company]"
            ),
            sms_body=(
                "Hi [First Name], thanks for applying to [Role] at [Company]. "
                "[Agent Name] will call you between [Call Window Start] and [Call Window End]. "
                "Chat now: [Retry Link] · Call me now instead: [Callback Link] · Pick a time: [Pick Time Link]"
            ),
        ),
        "rejection": CommunicationTemplate(
            subject="Your application to [Company]",
            body=(
                "Hi [First Name],\n\n"
                "Thank you for taking the time to speak with us about the [Role Phrase] at [Company].\n\n"
                "Unfortunately, after your screening call we're unable to progress your application — "
                "you don't currently meet our requirement for [Disqualification Reason].\n\n"
                "We appreciate your interest and wish you all the best in your search.\n\n"
                "Kind regards,\n[Company] Recruitment"
            ),
            sms_body="Hi [First Name], thanks for speaking with us. Unfortunately we can't progress your [Company] application due to our [Disqualification Reason] requirement. Best of luck!",
        ),
        "approval": CommunicationTemplate(
            subject="You're booked in — your interview at [Company]",
            body=(
                "Hi [First Name],\n\n"
                "Great news — you're booked in for your interview for the [Role Phrase] with [Company]!\n\n"
                "📅  Date: [Date]\n"
                "🕐  Time: [Time]\n"
                "💻  Join: [Zoom Link]\n\n"
                "Everything for the day in one place — countdown, add-to-calendar, and what "
                "to expect: [Portal Link]\n\n"
                "We'll send you a reminder with a few quick tips closer to the time.\n\n"
                "We're looking forward to meeting you, [First Name].\n\n"
                "The [Company] team\n\n"
                "Need to move it? No problem — pick a new time here: [Reschedule URL]"
            ),
            # One link, and it's the portal rather than the raw Zoom URL: the
            # portal shows the countdown and surfaces the join button when the
            # session opens, so the same link is right both a day before and a
            # minute before. The raw Zoom link still arrives via the email and
            # the calendar invite.
            sms_body="[First Name], you're booked in! [Company] interview on [Date] at [Time]. Everything you need is here: [Portal Link]. See you then 🎉",
        ),
        "approval_unscreened": CommunicationTemplate(
            subject="You're booked in — your interview at [Company]",
            body=(
                "Hi [First Name],\n\n"
                "You're booked in for your interview for the [Role Phrase] with [Company]!\n\n"
                "📅  Date: [Date]\n"
                "🕐  Time: [Time]\n"
                "💻  Join: [Zoom Link]\n\n"
                # The old copy promised "we'll text you a few quick eligibility
                # questions" — but gate checks by text are switched off
                # (GATE_CHECKS_BY_TEXT, killed 2026-07-30), so every self-serve
                # booker was promised a text that never came. Leading with a
                # promise the system won't keep is how you teach candidates to
                # ignore you. If the kill switch ever flips back on, restore the
                # promise WITH the mechanism.
                "Questions before the day? Just reply to this email — we're happy to help.\n\n"
                "Everything for the day in one place — countdown, add-to-calendar, and what "
                "to expect: [Portal Link]\n\n"
                "We're looking forward to meeting you, [First Name].\n\n"
                "The [Company] team\n\n"
                "Need to move it? No problem — pick a new time here: [Reschedule URL]"
            ),
            sms_body="[First Name], you're booked in! [Company] interview on [Date] at [Time]. Any questions, just reply here. Details: [Portal Link]",
        ),
        "appointment_rescheduled": CommunicationTemplate(
            subject="Your interview has been rescheduled — [Date] at [Time]",
            body=(
                "Hi [First Name],\n\n"
                "Your interview with [Company] has been rescheduled.\n\n"
                "New Date: [Date]\n"
                "New Time: [Time]\n"
                "Join Zoom: [Zoom Link]\n\n"
                "Countdown, calendar, and what to expect: [Portal Link]\n\n"
                "See you then,\nThe [Company] team\n\n"
                "If this new time doesn't work, you can pick a different one here: [Reschedule URL]"
            ),
            sms_body="[First Name], your [Company] interview has been moved to [Date] [Time]. Join: [Zoom Link]. To pick a different time: [Reschedule URL]",
        ),
        "form": CommunicationTemplate(
            subject="One last step — your [Company] questionnaire",
            body=(
                "Hi [First Name],\n\n"
                "Thanks for the great chat earlier. To wrap things up, please run through our short questionnaire (about 3–6 minutes):\n\n"
                "[Form Link]\n\n"
                "The sooner you complete it, the sooner we can finalise your application.\n\n"
                "[Website Line]\n"
                "Any questions? Reply to this email or text [Recruiter Phone].\n\n"
                "Cheers,\n[Company]"
            ),
            sms_body="Hi [First Name], one last step — please complete the [Company] questionnaire (3–6 min): [Form Link]",
        ),
        "form_reminder": CommunicationTemplate(
            subject="Reminder: your [Company] questionnaire is still waiting",
            body=(
                "Hi [First Name],\n\n"
                "Just a quick reminder — your [Company] questionnaire is still waiting to be completed.\n\n"
                "It only takes 3–6 minutes:\n\n"
                "[Form Link]\n\n"
                "Completing it is the final step before we can move your application forward.\n\n"
                "[Website Line]\n"
                "Any questions? Reply to this email or text [Recruiter Phone].\n\n"
                "Cheers,\n[Company]"
            ),
            sms_body="Hi [First Name], just a reminder — your [Company] questionnaire is still open: [Form Link] (3–6 min). Complete it to move your application forward!",
        ),
        "form_decline": CommunicationTemplate(
            subject="Application update",
            body="Hi [First Name],\n\nThanks for your time and interest in [Company]. We won't be progressing your application at this stage.\n\nBest of luck,\n[Company]",
            sms_body="[First Name], application update from [Company]: we won't be moving forward.",
        ),
        "close_success": CommunicationTemplate(
            subject="Congratulations on your successful application!",
            body="Hi [First Name],\n\nCongratulations! We'd like to move forward with your application for [Role] at [Company]. Welcome aboard.",
            sms_body="Congrats [First Name]! Your application for [Role] at [Company] is successful.",
        ),
        "close_rejection": CommunicationTemplate(
            subject="Update on your application to [Company]",
            body=(
                "Hi [First Name],\n\n"
                "Thank you for taking the time to go through our process for the [Role Phrase] at [Company].\n\n"
                "After careful consideration, we've decided not to move forward with your application at this stage. "
                "This was a difficult decision and is no reflection on the effort you put in.\n\n"
                "We genuinely appreciate your interest and wish you all the best in your search.\n\n"
                "Kind regards,\n[Company] Recruitment"
            ),
            sms_body="",
        ),
        "training": CommunicationTemplate(
            subject="You're hired — your [Company] start date is confirmed",
            body=(
                "Hi [First Name],\n\n"
                "Congratulations — we'd love to have you join the team at [Company]!\n\n"
                "📅  Start date: [Start Date]\n"
                "🕐  Start time: [Start Time]\n\n"
                "Please arrive a few minutes early so we can get you set up and introduce you to the team.\n\n"
                "If you have any questions before your first day, feel free to reply to this email or text [Recruiter Phone].\n\n"
                "We're looking forward to seeing you!\n\n"
                "The [Company] team"
            ),
            sms_body="Congrats [First Name]! You're starting at [Company] on [Start Date] at [Start Time]. See you then! 🎉",
        ),
        "reengage_chat": CommunicationTemplate(
            subject="Let's do it by text instead — your [Company] application",
            body=(
                "Hi [First Name],\n\n"
                "We tried to reach you by phone a few times about your application "
                "but couldn't catch you - no problem at all.\n\n"
                "You can finish everything by text or right here in your browser instead: "
                "a few quick questions, then you pick your interview slot. "
                "It takes about 3 minutes.\n\n"
                "[Retry Link]\n\n"
                "We've also just texted you the first question, so replying there works too.\n\n"
                "Best,\n[Company]"
            ),
            sms_body=(
                "Hi [First Name], we tried calling about your [Company] application but "
                "couldn't catch you - we can do the whole thing by text instead. "
                "Just to confirm, are you 18 or over?"
            ),
        ),
        "screening_retry": CommunicationTemplate(
            subject="Quick screening for [Role] at [Company]",
            body=(
                "Hi [First Name],\n\n"
                "We tried calling about your [Role Phrase] at [Company] but didn't manage to catch you. No problem at all.\n\n"
                "You can finish the screening online in about 3 minutes — chat or browser-voice, whichever you prefer:\n\n"
                "[Retry Link]\n\n"
                "Once you're done we'll book your interview right away.\n\n"
                "[Website Line]\n"
                "Any questions? Just reply to this email.\n\n"
                "Speak soon,\n[Company]"
            ),
            sms_body=(
                "Hi [First Name], we missed you on the [Company] screening call. Finish online in 3 min: [Retry Link]"
            ),
        ),
        "retry_chat_nudge": CommunicationTemplate(
            subject="Still there? Finish your [Company] screening",
            body=(
                "Hi [First Name],\n\n"
                "Looks like you started your screening chat for [Role Phrase] at [Company] but didn't get to finish — "
                "no worries, happens all the time.\n\n"
                "Jump back in right where you left off, it only takes a couple more minutes:\n\n"
                "[Retry Link]\n\n"
                "[Website Line]\n"
                "Any questions? Just reply to this email.\n\n"
                "Speak soon,\n[Company]"
            ),
            sms_body=(
                "Hi [First Name], you started your [Company] screening chat but didn't finish — pick up where you left off: [Retry Link]"
            ),
        ),
        "appointment_reminder_1h": CommunicationTemplate(
            subject="Your [Company] interview is in 1 hour — quick prep",
            body=(
                "Hi [First Name],\n\n"
                "Your interview for the [Role Phrase] with [Company] is in about an hour.\n\n"
                "When: [Date] at [Time]\n"
                "Join link: [Zoom Link]\n\n"
                "A few quick tips so you make the best impression:\n"
                "  • Test your camera, mic, and connection 5 minutes before — joining a couple of minutes early always looks good.\n"
                "  • Dress smart-casual; somewhere quiet and well-lit, plain wall behind you if possible.\n"
                "  • Have a glass of water and your CV/notes nearby.\n"
                "  • Have one or two thoughtful questions ready about the role or the company.\n"
                "  • Speak clearly, smile, and treat it as a two-way conversation.\n\n"
                "Best of luck — we're looking forward to meeting you.\n\n"
                "The [Company] team\n\n"
                "Need to reschedule? No problem — pick a new time here: [Reschedule URL]"
            ),
            sms_body=(
                "Hi [First Name], your [Company] interview is in 1 hour at [Time]. Join: [Zoom Link]. Test your camera/mic 5 min early. Good luck!"
            ),
        ),
        "appointment_confirm_chaser": CommunicationTemplate(
            subject="Quick confirmation needed — your [Company] interview",
            body=(
                "Hi [First Name],\n\n"
                "We haven't heard back from you yet and want to make sure your interview "
                "slot is still good:\n\n"
                "When: [Date] at [Time]\n"
                "Join link: [Zoom Link]\n\n"
                "Please reply YES to this email to confirm you're attending. "
                "If the time no longer works, you can pick a new one here: [Reschedule URL]\n\n"
                "See you soon,\nThe [Company] team"
            ),
            sms_body=(
                "Hi [First Name], just checking — are you still good for your [Company] "
                "interview on [Date] at [Time]? Reply Y to confirm your spot, or "
                "reschedule here: [Reschedule URL]"
            ),
        ),
        "appointment_reminder_10m": CommunicationTemplate(
            subject="Starting in 10 minutes — [Company] interview",
            body=(
                "Hi [First Name],\n\n"
                "Your interview is starting in 10 minutes.\n\n"
                "Join now: [Zoom Link]\n"
                "Time: [Date] at [Time]\n\n"
                "See you in there.\n\nThe [Company] team"
            ),
            sms_body=(
                "[First Name], your [Company] interview starts in 10 min. Join: [Zoom Link]"
            ),
        ),
        "appointment_no_show": CommunicationTemplate(
            subject="Reschedule your [Company] interview",
            body=(
                "Hi [First Name],\n\n"
                "We didn't manage to connect for your [Role Phrase] interview today. No worries — happens to the best of us.\n\n"
                "Pick a fresh time that works better for you here:\n\n"
                "[Reschedule URL]\n\n"
                "You'll see open slots over the next few days. Once you confirm we'll send a new confirmation and reminders right away.\n\n"
                "[Website Line]\n"
                "Any questions? Reply to this email or text [Recruiter Phone].\n\n"
                "Speak soon,\n[Company]"
            ),
            sms_body=(
                "Hi [First Name], we missed you for your [Company] interview. Pick a new time: [Reschedule URL]"
            ),
        ),
        "revival_followup": CommunicationTemplate(
            subject="We tried to reach you — rebook your [Company] interview",
            body=(
                "Hi [First Name],\n\n"
                "We just tried giving you a call about the [Role Phrase] interview you had booked "
                "with [Company] — sorry we missed each other!\n\n"
                "We completely get it, things come up. If you're still interested, you can grab a "
                "new time that suits you here:\n\n"
                "[Reschedule URL]\n\n"
                "Pick any open slot and we'll send a fresh confirmation and reminders right away.\n\n"
                "[Website Line]\n"
                "Any questions? Reply to this email or text [Recruiter Phone].\n\n"
                "Speak soon,\n[Company]"
            ),
            sms_body=(
                "Hi [First Name], we just tried calling about your missed [Company] interview — "
                "still interested? Rebook here: [Reschedule URL]"
            ),
        ),
        "slot_picker": CommunicationTemplate(
            subject="Pick your interview time — [Company]",
            body=(
                "Hi [First Name],\n\n"
                "Great news — you passed your [Company] screening! The next step is booking your interview.\n\n"
                "Pick a time that works for you here:\n\n"
                "[Reschedule URL]\n\n"
                "You'll see open slots over the next few days. If none of the times suit you, there's an option on that page to be notified when new slots open.\n\n"
                "[Website Line]\n"
                "Any questions? Reply to this email or text [Recruiter Phone].\n\n"
                "Speak soon,\n[Company]"
            ),
            sms_body=(
                "Hi [First Name], great news — you passed the [Company] screening! Book your interview here: [Reschedule URL]"
            ),
        ),
        "appointment_cancelled_rebook": CommunicationTemplate(
            subject="Still keen? Pick a new interview time — [Company]",
            body=(
                "Hi [First Name],\n\n"
                "You let us know you couldn't make your interview — no problem at all, "
                "and thank you for telling us rather than leaving us guessing.\n\n"
                "Your place is still here whenever you're ready. Pick whichever time suits you:\n\n"
                "[Reschedule URL]\n\n"
                "If none of the times on that page work, there's an option to be notified "
                "when new ones open up.\n\n"
                "[Website Line]\n"
                "Any questions? Reply to this email or text [Recruiter Phone].\n\n"
                "Speak soon,\n[Company]"
            ),
            sms_body=(
                "Hi [First Name], no problem about the interview — thanks for letting us know. "
                "Whenever you're ready, pick a new time here: [Reschedule URL]"
            ),
        ),
    }
