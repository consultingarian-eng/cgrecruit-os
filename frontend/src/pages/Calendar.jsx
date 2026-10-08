import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import api from "@/lib/api";
import { usePipeline } from "@/lib/pipeline";
import { fmtET, DEFAULT_TZ } from "@/lib/formatET";
import {
    Calendar as CalendarIcon, VideoCamera, User, Clock, GearSix,
    CaretDown, CaretRight, ClockClockwise, CheckCircle, XCircle,
    ClipboardText, Phone, Warning, Envelope, ChartBar,
} from "@phosphor-icons/react";
import AvailabilityManager from "@/components/AvailabilityManager";
import {
    AlertDialog, AlertDialogContent, AlertDialogHeader,
    AlertDialogTitle, AlertDialogFooter,
} from "@/components/ui/alert-dialog";
import { toast } from "sonner";

const TODAY = new Date().toLocaleDateString('en-CA', { timeZone: DEFAULT_TZ });

const ATTENDANCE_CONFIG = {
    attended_form: {
        title: "Attended — Send Form",
        desc: (name) => `Mark ${name} as attended and move them to the Form stage.`,
        bulkDesc: (n) => `Mark all ${n} candidates as attended and move them to the Form stage.`,
        // The attendance endpoint advances the candidate to FORM and fires the
        // questionnaire email + SMS itself. This is a statement of what will
        // happen, not an option the dialog is able to decline.
        note: "The Final Questionnaire email and SMS are sent automatically.",
        intent: "primary",
        badgeLabel: "Attended · Form sent",
        badgeBg: "rgba(16,185,129,0.14)",
        badgeColor: "#34D399",
    },
    attended_no_form: {
        title: "Attended — No Form",
        desc: (name) => `Mark ${name} as attended. No form email will be sent.`,
        note: null,
        intent: "secondary",
        badgeLabel: "Attended",
        badgeBg: "rgba(59,130,246,0.14)",
        badgeColor: "#60A5FA",
    },
    no_show: {
        title: "Mark as No-show",
        desc: (name) => `Mark ${name} as a no-show. A reschedule email and SMS will be sent automatically.`,
        // Deliberately no bulkDesc: set_attendance fans out reschedule outreach
        // per candidate, so a room-wide no-show is N regulated messages from one
        // click. No-show stays one candidate at a time.
        note: null,
        intent: "danger",
        badgeLabel: "No-show",
        badgeBg: "rgba(245,158,11,0.14)",
        badgeColor: "#FBBF24",
    },
};

function candidateName(c) {
    // Appointment rows carry the raw name fields; outcome-due rows carry a
    // pre-joined `name`.
    return `${c?.first_name || ""} ${c?.last_name || ""}`.trim() || c?.name || "";
}

function formatDayLabel(dateStr) {
    const [y, m, d] = dateStr.split("-").map(Number);
    const dt = new Date(y, m - 1, d);
    const weekday = dt.toLocaleDateString(undefined, { weekday: "short" }).toUpperCase();
    const day = dt.toLocaleDateString(undefined, { day: "numeric", month: "short" });
    return { weekday, day };
}

/* ---------- Attendance confirm dialog ---------- */
function AttendanceDialog({ open, onOpenChange, action, candidates, onConfirm }) {
    const { activePipelineId } = usePipeline() || {};

    if (!action || !candidates?.length) return null;
    const cfg = ATTENDANCE_CONFIG[action];
    const name = candidateName(candidates[0]) || "this candidate";
    const many = candidates.length > 1;
    // Every action here sends mail and a billable SMS, so the last gate before
    // it fires says out loud when it is reaching the other office's candidates.
    // No active pipeline means nothing here has been confirmed as this office's,
    // so every attributed candidate gets the warning rather than none of them.
    const otherOffice = candidates.filter((c) => c.pipeline_id && c.pipeline_id !== activePipelineId);
    const otherOfficeNames = [...new Set(otherOffice.map((c) => c.pipeline_name).filter(Boolean))];
    const isDanger = cfg.intent === "danger";
    const isSecondary = cfg.intent === "secondary";

    return (
        <AlertDialog open={open} onOpenChange={onOpenChange}>
            <AlertDialogContent className="bg-[#101015] border border-strokes text-ink rounded-xl max-w-md">
                <AlertDialogHeader>
                    <AlertDialogTitle className="flex items-center gap-2 text-base">
                        {isDanger
                            ? <Warning size={18} weight="bold" className="text-[#FBBF24]" />
                            : isSecondary
                                ? <CheckCircle size={18} weight="bold" className="text-[#60A5FA]" />
                                : <CheckCircle size={18} weight="bold" className="text-brand-primary" />
                        }
                        {cfg.title}
                    </AlertDialogTitle>
                </AlertDialogHeader>

                <div className="space-y-3 text-sm">
                    <p className="text-ink-muted">
                        {many && cfg.bulkDesc ? cfg.bulkDesc(candidates.length) : cfg.desc(name)}
                    </p>
                    {otherOffice.length > 0 && (
                        <p
                            className="text-xs text-[#FBBF24] bg-[rgba(245,158,11,0.08)] border border-[rgba(245,158,11,0.18)] rounded px-2.5 py-1.5"
                            data-testid="attendance-other-office-warning"
                        >
                            {many
                                ? `${otherOffice.length} of these ${otherOfficeNames.length ? `belong to ${otherOfficeNames.join(" · ")}` : "belong to another office"} — not the office you're viewing.`
                                : `${otherOfficeNames[0] || "Another office"} — not the office you're viewing.`}
                        </p>
                    )}
                    {cfg.note && (
                        <p className="text-xs text-ink-muted bg-surface border border-strokes rounded px-2.5 py-1.5">
                            {cfg.note}
                        </p>
                    )}
                    {isDanger && (
                        <p className="text-xs text-[#FBBF24]/80 bg-[rgba(245,158,11,0.08)] border border-[rgba(245,158,11,0.18)] rounded px-2.5 py-1.5">
                            The candidate will receive a link to choose a new slot within the next 4 days.
                        </p>
                    )}
                </div>

                <AlertDialogFooter className="gap-2 mt-4">
                    <button
                        onClick={() => onOpenChange(false)}
                        className="btn-secondary flex-1"
                    >
                        Cancel
                    </button>
                    <button
                        onClick={() => onConfirm()}
                        className={isDanger ? "btn-danger flex-1" : "btn-primary flex-1"}
                    >
                        {many ? `Confirm ${candidates.length}` : "Confirm"}
                    </button>
                </AlertDialogFooter>
            </AlertDialogContent>
        </AlertDialog>
    );
}

/* ---------- Candidate mini-summary ---------- */
function MiniSummary({ a }) {
    const resume = a.parsed_resume || {};
    const experience = Array.isArray(resume.experience) ? resume.experience : [];
    const skills = Array.isArray(resume.skills) ? resume.skills : [];
    const summary = resume.summary || "";

    return (
        <div className="border-t border-strokes bg-[#0E0E11] px-4 pb-4 pt-3 text-sm space-y-3">
            {/* contact + score row */}
            <div className="flex flex-wrap gap-4 text-xs text-ink-muted">
                {a.email && (
                    <span className="flex items-center gap-1">
                        <Envelope size={11} /> {a.email}
                    </span>
                )}
                {a.phone && (
                    <span className="flex items-center gap-1">
                        <Phone size={11} /> {a.phone}
                    </span>
                )}
                {a.suitability_score != null && (
                    <span className="flex items-center gap-1">
                        <ChartBar size={11} /> Fit score: {a.suitability_score}/10
                    </span>
                )}
                {a.verdict && (
                    <span className="capitalize">{a.verdict} candidate</span>
                )}
            </div>

            {/* call summary */}
            {a.call_summary && (
                <div>
                    <div className="text-[10px] uppercase tracking-widest text-ink-muted mb-1">Screening notes</div>
                    <p className="text-xs text-ink leading-relaxed">{a.call_summary}</p>
                </div>
            )}

            {/* resume summary */}
            {summary && !a.call_summary && (
                <div>
                    <div className="text-[10px] uppercase tracking-widest text-ink-muted mb-1">Background</div>
                    <p className="text-xs text-ink leading-relaxed">{summary}</p>
                </div>
            )}

            {/* work experience */}
            {experience.length > 0 && (
                <div>
                    <div className="text-[10px] uppercase tracking-widest text-ink-muted mb-1.5">Work history</div>
                    <div className="space-y-1.5">
                        {experience.slice(0, 3).map((exp, i) => (
                            <div key={i} className="text-xs leading-snug">
                                <span className="font-medium text-ink">{exp.title || exp.role || exp.position || "Role"}</span>
                                {(exp.company || exp.employer) && (
                                    <span className="text-ink-muted"> · {exp.company || exp.employer}</span>
                                )}
                                {exp.dates && (
                                    <span className="text-ink-dim ml-1">({exp.dates})</span>
                                )}
                            </div>
                        ))}
                    </div>
                </div>
            )}

            {/* skills */}
            {skills.length > 0 && (
                <div className="flex flex-wrap gap-1">
                    {skills.slice(0, 8).map((s, i) => (
                        <span key={i} className="text-[10px] px-1.5 py-0.5 rounded bg-surface border border-strokes text-ink-muted">
                            {s}
                        </span>
                    ))}
                </div>
            )}

            {/* fallback if no parsed data */}
            {!a.call_summary && !summary && experience.length === 0 && skills.length === 0 && (
                <p className="text-xs text-ink-dim italic">No additional details available — screening call summary will appear here once complete.</p>
            )}
        </div>
    );
}

/* ---------- Attendance action buttons ---------- */
function AttendanceButtons({ candidate, onAttendanceAction }) {
    // flex-shrink-0 pinned the cluster at its intrinsic width, so under ~470px
    // the row's overflow-hidden clipped No-show off the card entirely. Below sm
    // it takes its own full-width line instead, at a thumb-sized height.
    return (
        <div className="flex items-center gap-1.5 flex-wrap justify-end w-full sm:w-auto sm:flex-shrink-0">
            <button
                onClick={() => onAttendanceAction(candidate, "attended_form")}
                className="text-[11px] px-3 py-2 min-h-[44px] sm:min-h-0 sm:px-2 sm:py-1 rounded border border-[rgba(16,185,129,0.3)] text-[#34D399] hover:bg-[rgba(16,185,129,0.08)] transition-colors whitespace-nowrap"
                data-testid={`attend-form-btn-${candidate.id}`}
            >
                <ClipboardText size={10} className="inline mr-0.5" />
                Attended + Form
            </button>
            <button
                onClick={() => onAttendanceAction(candidate, "attended_no_form")}
                className="text-[11px] px-3 py-2 min-h-[44px] sm:min-h-0 sm:px-2 sm:py-1 rounded border border-[rgba(59,130,246,0.3)] text-[#60A5FA] hover:bg-[rgba(59,130,246,0.08)] transition-colors whitespace-nowrap"
                data-testid={`attend-no-form-btn-${candidate.id}`}
            >
                <CheckCircle size={10} className="inline mr-0.5" />
                Attended
            </button>
            <button
                onClick={() => onAttendanceAction(candidate, "no_show")}
                className="text-[11px] px-3 py-2 min-h-[44px] sm:min-h-0 sm:px-2 sm:py-1 rounded border border-[rgba(245,158,11,0.3)] text-[#FBBF24] hover:bg-[rgba(245,158,11,0.08)] transition-colors whitespace-nowrap"
                data-testid={`no-show-btn-${candidate.id}`}
            >
                <XCircle size={10} className="inline mr-0.5" />
                No-show
            </button>
        </div>
    );
}

/* ---------- Appointment row ---------- */
function AppointmentRow({ a, onAttendanceAction }) {
    const { timezone } = usePipeline() || {};
    const [expanded, setExpanded] = useState(false);
    // The day column is already keyed on DEFAULT_TZ (REACT_APP_TIMEZONE) — render the time in
    // the same zone or a recruiter abroad reads an ET date beside a local clock.
    const time = fmtET(a.appointment_at, {
        timeZone: timezone || DEFAULT_TZ, hour: "2-digit", minute: "2-digit",
    });
    const attendedStatus = a.attendance_status;
    const cfg = attendedStatus ? ATTENDANCE_CONFIG[attendedStatus] : null;

    return (
        <div className="surface overflow-hidden" data-testid={`appointment-${a.id}`}>
            <div className="p-4 flex flex-wrap items-center gap-x-4 gap-y-3">
                {/* time */}
                <div className="flex-shrink-0 w-14">
                    <div className="text-[10px] uppercase tracking-widest text-ink-muted">Time</div>
                    <div className="font-heading font-bold text-base">{time}</div>
                </div>

                <div className="w-px h-10 bg-strokes flex-shrink-0" />

                {/* candidate info */}
                <div className="flex-1 min-w-0">
                    <button
                        onClick={() => setExpanded((v) => !v)}
                        className="font-medium hover:text-brand-primary transition-colors text-left flex items-center gap-1"
                    >
                        {a.first_name} {a.last_name}
                        <CaretDown
                            size={11}
                            weight="bold"
                            className={`text-ink-muted transition-transform ${expanded ? "rotate-180" : ""}`}
                        />
                    </button>
                    <div className="flex items-center gap-3 text-xs text-ink-muted mt-0.5 flex-wrap">
                        {a.appointment_cancelled_at && !a.attendance_status && (
                            <span
                                className="px-1.5 py-0.5 rounded bg-[rgba(248,113,113,0.12)] text-[#F87171] font-semibold"
                                title={a.appointment_cancel_reason || "The candidate said they can't attend this slot."}
                                data-testid="cant-attend-chip"
                            >
                                Can't attend — told us in advance
                            </span>
                        )}
                        {a.appointment_recruiter && (
                            <span className="flex items-center gap-1">
                                <User size={11} /> {a.appointment_recruiter}
                            </span>
                        )}
                        {a.appointment_link && (
                            <a
                                href={a.appointment_link}
                                target="_blank"
                                rel="noreferrer"
                                className="text-brand-primary flex items-center gap-1 hover:underline"
                                onClick={(e) => e.stopPropagation()}
                            >
                                <VideoCamera size={11} /> Join
                            </a>
                        )}
                        {a.appointment_sms_confirmed && (
                            <span className="flex items-center gap-1" style={{ color: "#10B981" }}>
                                <CheckCircle size={11} weight="fill" /> SMS confirmed
                            </span>
                        )}
                    </div>
                </div>

                {/* status / action buttons */}
                {cfg ? (
                    <span
                        className="status-pill flex-shrink-0"
                        style={{ background: cfg.badgeBg, color: cfg.badgeColor }}
                    >
                        <CheckCircle size={9} /> {cfg.badgeLabel}
                    </span>
                ) : (
                    <AttendanceButtons candidate={a} onAttendanceAction={onAttendanceAction} />
                )}
            </div>

            {expanded && <MiniSummary a={a} />}
        </div>
    );
}

/* ---------- Time slot section (inside a day) ---------- */
function TimeSection({ time, appointments, defaultOpen = false, onAttendanceAction }) {
    const [open, setOpen] = useState(defaultOpen);
    const count = appointments.length;
    return (
        <div className="border border-strokes rounded-xl overflow-hidden">
            <button
                onClick={() => setOpen((v) => !v)}
                className="w-full flex items-center gap-3 px-4 py-3 hover:bg-surface/40 transition-colors text-left"
            >
                <div className="flex items-center gap-2 flex-1">
                    <Clock size={13} className="text-brand-primary flex-shrink-0" />
                    <span className="font-heading font-bold text-sm">{time}</span>
                    <span className="text-xs text-ink-muted">{count} candidate{count !== 1 ? "s" : ""}</span>
                </div>
                {open ? <CaretDown size={11} weight="bold" className="text-ink-muted" /> : <CaretRight size={11} weight="bold" className="text-ink-muted" />}
            </button>
            {open && (
                <div className="grid gap-2 p-2 border-t border-strokes">
                    {appointments.map((a) => (
                        <AppointmentRow key={a.id} a={a} onAttendanceAction={onAttendanceAction} />
                    ))}
                </div>
            )}
        </div>
    );
}

/* ---------- Date section ---------- */
function DateSection({ dateStr, appointments, defaultOpen = false, isToday = false, onAttendanceAction }) {
    const { timezone } = usePipeline() || {};
    const tz = timezone || DEFAULT_TZ;
    const [open, setOpen] = useState(defaultOpen);
    const { weekday, day } = formatDayLabel(dateStr);
    const count = appointments.length;

    const timeGroups = useMemo(() => {
        const map = new Map();
        for (const a of appointments) {
            const t = a.appointment_at
                ? fmtET(a.appointment_at, { timeZone: tz, hour: "2-digit", minute: "2-digit" })
                : "Unknown";
            if (!map.has(t)) map.set(t, { raw: a.appointment_at || "", items: [] });
            map.get(t).items.push(a);
        }
        return Array.from(map.entries(), ([time, v]) => ({ time, raw: v.raw, items: v.items }))
            .sort((a, b) => a.raw.localeCompare(b.raw));
    }, [appointments]);

    return (
        <div data-testid={`appointments-${dateStr}`}>
            <button
                onClick={() => setOpen((v) => !v)}
                className="w-full flex items-center gap-3 mb-2 group text-left"
            >
                <div className={`surface px-3 py-1.5 min-w-[72px] ${isToday ? "border-brand-primary/40" : ""}`}>
                    <div className={`text-[10px] uppercase tracking-widest ${isToday ? "text-brand-primary font-semibold" : "text-ink-muted"}`}>
                        {isToday ? "Today" : weekday}
                    </div>
                    <div className="font-heading font-bold text-lg">{day}</div>
                </div>
                <div className="flex-1 h-px bg-strokes group-hover:bg-strokes/80" />
                <span className="text-xs text-ink-muted mr-1">{count} interview{count !== 1 ? "s" : ""}</span>
                <span className="text-ink-muted">
                    {open ? <CaretDown size={13} weight="bold" /> : <CaretRight size={13} weight="bold" />}
                </span>
            </button>

            {open && (
                <div className="grid gap-2 mb-2">
                    {timeGroups.map(({ time, items }) => (
                        <TimeSection
                            key={time}
                            time={time}
                            appointments={items}
                            // A session still owing outcomes is the work on this
                            // screen; collapsed it puts no attendance button on it.
                            defaultOpen={items.some((a) => !a.attendance_status)}
                            onAttendanceAction={onAttendanceAction}
                        />
                    ))}
                </div>
            )}
        </div>
    );
}

/* /calendar/outcome-due answers for every pipeline the account can see, so
   scoping to the office happens here. A session counts as this office's if
   anyone in the room belongs to the active pipeline — that keeps the joint
   two-office rooms whole for whoever is hosting, which is the entire
   reason the backend groups by room rather than by pipeline — while a room that
   is purely the other office's is not this account's to mark off. A session no
   candidate attributes to a pipeline is nobody's other office, so it stays in
   view rather than being filed under one.

   With no active pipeline — the account has none, or the context has not
   resolved yet while this panel's own fetch already has — nothing is this
   office's. The buttons here mail and text people, so the unresolved state
   hides them behind the peek toggle rather than handing over every office. */
function sessionIsActiveOffice(session, activePipelineId) {
    if (!activePipelineId) return false;
    const pids = new Set((session.candidates || []).map((c) => c.pipeline_id).filter(Boolean));
    return pids.size === 0 || pids.has(activePipelineId);
}

/* ---------- Outcome-due session ---------- */
function OutcomeDueSession({ session, tz, activePipelineId, onAttendanceAction, onBulkAttended }) {
    const when = fmtET(session.appointment_at, {
        timeZone: tz, weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
    }) || session.appointment_at;
    const mine = sessionIsActiveOffice(session, activePipelineId);

    return (
        <div
            className={`border rounded-xl overflow-hidden ${mine ? "border-strokes" : "border-[rgba(245,158,11,0.45)]"}`}
            data-testid="outcome-due-session"
        >
            <div className="flex items-center gap-2 flex-wrap px-3 py-2 bg-surface/40 border-b border-strokes">
                <Clock size={13} className="text-brand-primary flex-shrink-0" />
                <span className="font-heading font-bold text-sm">{when}</span>
                <span className="text-xs text-ink-muted">{session.unmarked} unmarked</span>
                {/* Only reachable through the panel's local peek toggle, and it stays
                    labelled the whole time it is open — the grey pipeline name is far
                    too quiet to sit beside buttons that email and text people. */}
                {!mine && (
                    <span
                        className="status-pill"
                        style={{ background: "rgba(245,158,11,0.14)", color: "#FBBF24" }}
                        data-testid="outcome-due-other-office"
                    >
                        <Warning size={9} weight="bold" /> Another office
                    </span>
                )}
                {/* A shared room is the case this grouping exists for: the host marks
                    off the whole room, not just their own office's half of it. */}
                {session.shared && (
                    <span className="status-pill" style={{ background: "rgba(245,158,11,0.14)", color: "#FBBF24" }}>
                        Shared room
                    </span>
                )}
                {session.pipeline_names?.length > 0 && (
                    <span className="text-xs text-ink-muted">{session.pipeline_names.filter(Boolean).join(" · ")}</span>
                )}
                {session.appointment_link && (
                    <a
                        href={session.appointment_link}
                        target="_blank"
                        rel="noreferrer"
                        className="text-xs text-brand-primary flex items-center gap-1 hover:underline"
                    >
                        <VideoCamera size={11} /> Room
                    </a>
                )}
                {/* Attended-only, and never on a session the recruiter is merely
                    peeking at. The usual answer to a room is "everyone bar two", so
                    the two go first per row and this clears the rest. */}
                {mine && session.candidates.length > 1 && (
                    <button
                        onClick={() => onBulkAttended(session.candidates)}
                        className="ml-auto text-[11px] px-3 py-2 min-h-[44px] sm:min-h-0 sm:px-2 sm:py-1 rounded border border-[rgba(16,185,129,0.3)] text-[#34D399] hover:bg-[rgba(16,185,129,0.08)] transition-colors whitespace-nowrap"
                        data-testid="outcome-due-bulk-attended"
                    >
                        <ClipboardText size={10} className="inline mr-0.5" />
                        All {session.candidates.length} attended + form
                    </button>
                )}
            </div>
            <div className="divide-y divide-strokes">
                {session.candidates.map((c) => {
                    // In a shared room most rows are the host's own; the ones that are
                    // not get the loud pill, because the green button beside them sends
                    // the questionnaire and a billable SMS to that office's candidate.
                    const cOther = !!c.pipeline_id && c.pipeline_id !== activePipelineId;
                    return (
                        <div key={c.id} className="px-3 py-2 flex flex-wrap items-center gap-x-3 gap-y-2">
                            <div className="flex-1 min-w-0">
                                <div className="text-sm font-medium truncate">{c.name || "Unnamed candidate"}</div>
                                <div className="flex items-center gap-2 text-xs text-ink-muted flex-wrap">
                                    {cOther ? (
                                        <span
                                            className="status-pill"
                                            style={{ background: "rgba(245,158,11,0.14)", color: "#FBBF24" }}
                                            data-testid={`outcome-due-other-office-row-${c.id}`}
                                        >
                                            <Warning size={9} weight="bold" /> {c.pipeline_name || "Another office"}
                                        </span>
                                    ) : c.pipeline_name ? <span>{c.pipeline_name}</span> : null}
                                    {/* An archived candidate still owes an outcome — say so
                                        before the form email goes out to them. */}
                                    {c.archived && <span className="text-ink-dim">Archived</span>}
                                    {c.confirmed && (
                                        <span className="flex items-center gap-1" style={{ color: "#10B981" }}>
                                            <CheckCircle size={11} weight="fill" /> SMS confirmed
                                        </span>
                                    )}
                                </div>
                            </div>
                            <AttendanceButtons candidate={c} onAttendanceAction={onAttendanceAction} />
                        </div>
                    );
                })}
            </div>
        </div>
    );
}

/* ---------- Outcome-due panel ---------- */
function OutcomeDuePanel({ sessions, scrollIntoView, onAttendanceAction, onBulkAttended }) {
    const { timezone, activePipelineId } = usePipeline() || {};
    const tz = timezone || DEFAULT_TZ;
    const ref = useRef(null);
    const scrolledRef = useRef(false);
    // Peeking at the other office is local to this panel on purpose: the shared
    // pipeline context is persisted and app-wide, so switching it from here would
    // follow the recruiter onto every other screen.
    const [showOtherOffices, setShowOtherOffices] = useState(false);

    // A peek does not survive changing office — the new office gets its own default.
    useEffect(() => { setShowOtherOffices(false); }, [activePipelineId]);

    const [collapsed, setCollapsed] = useState(
        () => localStorage.getItem("cgrecruit_outcome_due_collapsed") === "1",
    );
    const toggleCollapsed = () => setCollapsed((v) => {
        localStorage.setItem("cgrecruit_outcome_due_collapsed", v ? "" : "1");
        return !v;
    });

    // "Clear for now" hides sessions older than a recruiter-chosen day. Display
    // only — nothing is marked, so an old interview still shows on its Calendar
    // day and comes back here the moment the cutoff is removed. Stored as a
    // wall-clock date so it means "before today" in the office's own timezone.
    const [cutoff, setCutoff] = useState(() => localStorage.getItem("cgrecruit_outcome_due_cutoff") || "");
    const clearBacklog = () => {
        const today = fmtET(new Date(), { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" });
        const iso = today.replace(/^(\d{2})\/(\d{2})\/(\d{4})$/, "$3-$1-$2");
        localStorage.setItem("cgrecruit_outcome_due_cutoff", iso);
        setCutoff(iso);
    };
    const sessionDay = (s) => {
        const d = fmtET(s.appointment_at, { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" });
        return d.replace(/^(\d{2})\/(\d{2})\/(\d{4})$/, "$3-$1-$2");
    };
    const current = useMemo(
        () => (cutoff ? sessions.filter((s) => sessionDay(s) >= cutoff) : sessions),
        [sessions, cutoff, tz],
    );
    const hiddenCount = sessions.length - current.length;

    const mine = useMemo(
        () => current.filter((s) => sessionIsActiveOffice(s, activePipelineId)),
        [current, activePipelineId],
    );
    const otherCount = current.length - mine.length;
    const shown = showOtherOffices ? current : mine;
    const shownUnmarked = shown.reduce((n, s) => n + (s.unmarked || 0), 0);

    useEffect(() => {
        // The hourly nudge links to /calendar?outcome_due=1 — land on the sessions
        // it is asking about. Once only, or clearing a row scrolls the page back.
        // The nudge also overrides a collapsed panel: arriving via that link means
        // the recruiter came here to mark outcomes, so show them.
        if (!scrollIntoView || scrolledRef.current || !ref.current) return;
        scrolledRef.current = true;
        setCollapsed(false);
        ref.current.scrollIntoView({ block: "start" });
    }, [scrollIntoView, shown.length]);

    if (!sessions.length) return null;

    return (
        <div
            ref={ref}
            className={`surface p-4 mb-6 ${shown.length ? "border-[rgba(245,158,11,0.35)]" : ""}`}
            data-testid="outcome-due-panel"
        >
            <div className={`flex items-center gap-2 flex-wrap ${shown.length && !collapsed ? "mb-3" : ""}`}>
                <button
                    onClick={toggleCollapsed}
                    className="flex items-center gap-2 text-left"
                    data-testid="outcome-due-collapse-toggle"
                    aria-expanded={!collapsed}
                >
                    <CaretRight
                        size={12}
                        weight="bold"
                        className={`text-ink-muted transition-transform ${collapsed ? "" : "rotate-90"}`}
                    />
                    <Warning size={14} weight="bold" className={shown.length ? "text-[#FBBF24]" : "text-ink-muted"} />
                    <span className="font-heading font-bold text-sm">Needs an outcome</span>
                </button>
                <span className="text-xs text-ink-muted">
                    {shown.length
                        ? `${shownUnmarked} unmarked across ${shown.length} finished session${shown.length !== 1 ? "s" : ""}`
                        : "Nothing outstanding in the office you're viewing"}
                </span>
                <div className="flex items-center gap-3 ml-auto">
                    {hiddenCount > 0 && (
                        <button
                            onClick={() => { localStorage.removeItem("cgrecruit_outcome_due_cutoff"); setCutoff(""); }}
                            className="text-[11px] text-ink-muted/70 hover:text-ink transition-colors"
                            data-testid="outcome-due-show-older"
                            title="Older sessions are hidden, not marked — their candidates are still unresolved."
                        >
                            {hiddenCount} older hidden — show
                        </button>
                    )}
                    {shown.length > 0 && !collapsed && (
                        <button
                            onClick={clearBacklog}
                            className="text-xs text-ink-muted hover:text-ink transition-colors"
                            data-testid="outcome-due-clear-backlog"
                            title="Hide sessions from before today. Nothing is marked — they stay on their Calendar days."
                        >
                            Clear for now
                        </button>
                    )}
                    {otherCount > 0 && !collapsed && (
                        <button
                            onClick={() => setShowOtherOffices((v) => !v)}
                            className="text-xs text-ink-muted hover:text-ink transition-colors"
                            data-testid="outcome-due-other-office-toggle"
                        >
                            {showOtherOffices
                                ? "Hide other offices"
                                : `Show ${otherCount} session${otherCount !== 1 ? "s" : ""} from another office`}
                        </button>
                    )}
                </div>
            </div>
            {shown.length > 0 && !collapsed && (
                <div className="grid gap-3">
                    {shown.map((s) => (
                        <OutcomeDueSession
                            key={`${s.appointment_at}|${s.appointment_link || ""}`}
                            session={s}
                            tz={tz}
                            activePipelineId={activePipelineId}
                            onAttendanceAction={onAttendanceAction}
                            onBulkAttended={onBulkAttended}
                        />
                    ))}
                </div>
            )}
        </div>
    );
}

/* Drop candidates from the outcome-due sessions as their outcomes land, so the
   panel shrinks on confirm rather than at the next 30s poll. */
function pruneOutcomeDue(sessions, doneIds) {
    const out = [];
    for (const s of sessions) {
        const candidates = s.candidates.filter((c) => !doneIds.has(c.id));
        if (candidates.length) out.push({ ...s, candidates, unmarked: candidates.length });
    }
    return out;
}

/* ---------- Calendar page ---------- */
export default function CalendarPage() {
    const { activePipelineId, pipelines: ctxPipelines, refresh: refreshPipelines, timezone } = usePipeline();
    const tz = timezone || DEFAULT_TZ;
    const [searchParams] = useSearchParams();
    const [appointments, setAppointments] = useState([]);
    // Only the sessions are kept: the endpoint's total spans every office, and
    // the panel counts what it actually shows.
    const [outcomeDue, setOutcomeDue] = useState([]);
    const [loading, setLoading] = useState(true);
    const [loadError, setLoadError] = useState(null);
    const [showAvailability, setShowAvailability] = useState(false);
    const [showPast, setShowPast] = useState(false);
    // Pending attendance action: { candidates, action }
    const [pending, setPending] = useState(null);
    // A room of twelve takes a while to fan out, and the rows stay on screen the
    // whole time — without this a second click re-posts candidates the first
    // pass has not reached yet and sends them the questionnaire twice.
    const [submitting, setSubmitting] = useState(false);

    const reload = async () => {
        const params = activePipelineId ? { pipeline_id: activePipelineId } : {};
        try {
            // outcome-due takes no pipeline filter — it groups by room so a joint
            // two-office session stays whole, which a server-side pipeline
            // filter would cut in half. The panel scopes to the active office
            // itself. Its failure must not take the schedule down, and the last
            // known sessions beat a clean-looking zero.
            const [a, o] = await Promise.all([
                api.get("/calendar/appointments", { params }),
                api.get("/calendar/outcome-due").catch(() => null),
            ]);
            setAppointments(a.data);
            if (o) setOutcomeDue(o.data.sessions || []);
            setLoadError(null);
        } catch (e) {
            // Without this the page falls back to "No appointments scheduled" and
            // a dead API looks exactly like a genuinely clear day.
            setLoadError(e?.response?.data?.detail || "The request failed. Check your connection and try again.");
        }
    };

    useEffect(() => {
        reload().finally(() => setLoading(false));
        const id = setInterval(reload, 30_000);
        return () => clearInterval(id);
    }, [activePipelineId]); // eslint-disable-line react-hooks/exhaustive-deps

    const retryLoad = () => {
        setLoading(true);
        reload().finally(() => setLoading(false));
    };

    // Re-read the pipelines the manager was editing, or switching back to a tab
    // re-seeds its draft from the pre-save rules and the saved work looks lost.
    const handleAvailabilitySaved = async () => {
        await refreshPipelines();
        await reload();
    };

    const { grouped, pastDates, todayDates, futureDates } = useMemo(() => {
        const grp = appointments.reduce((acc, a) => {
            const d = a.appointment_at
                ? new Date(a.appointment_at).toLocaleDateString('en-CA', { timeZone: DEFAULT_TZ })
                : "";
            if (!d) return acc;
            (acc[d] = acc[d] || []).push(a);
            return acc;
        }, {});
        for (const d of Object.keys(grp)) {
            grp[d].sort((a, b) => (a.appointment_at || "").localeCompare(b.appointment_at || ""));
        }
        const all = Object.keys(grp).sort();
        return {
            grouped: grp,
            pastDates: all.filter((d) => d < TODAY),
            todayDates: all.filter((d) => d === TODAY),
            futureDates: all.filter((d) => d > TODAY),
        };
    }, [appointments]);

    const openAttendance = (candidates, action) => {
        if (submitting) {
            toast.info("Still recording the last outcome — one moment");
            return;
        }
        setPending({ candidates, action });
    };

    const handleAttendanceAction = (candidate, action) => openAttendance([candidate], action);

    const handleBulkAttended = (candidates) => openAttendance(candidates, "attended_form");

    const handleAttendanceConfirm = async () => {
        if (!pending) return;
        const { candidates, action } = pending;
        setPending(null);
        setSubmitting(true);

        const done = [];
        let failure = null;
        // One at a time: each POST fans out an email and a billable SMS, and a
        // failure part-way has to leave the rows it never reached untouched.
        for (const c of candidates) {
            try {
                // Sets attendance, advances attended_form to FORM and sends the
                // questionnaire email + SMS — a follow-up /move sends both twice.
                const r = await api.post(`/candidates/${c.id}/attendance`, { status: action });
                done.push(r.data.candidate);
            } catch (e) {
                failure = e;
                break;
            }
        }
        setSubmitting(false);

        if (done.length) {
            // Update local state so the rows reflect the new status instantly
            const byId = new Map(done.map((c) => [c.id, c]));
            setAppointments((prev) =>
                prev.map((a) => (byId.has(a.id) ? { ...a, ...byId.get(a.id) } : a))
            );
            setOutcomeDue((prev) => pruneOutcomeDue(prev, new Set(byId.keys())));
        }

        if (failure) {
            const detail = failure?.response?.data?.detail || "Failed to update attendance";
            toast.error(done.length ? `${detail} — ${done.length} of ${candidates.length} recorded` : detail);
        } else if (done.length > 1) {
            toast.success(`${done.length} marked attended — questionnaire emails sent`);
        } else if (action === "attended_form") {
            toast.success("Moved to Form stage — questionnaire email sent");
        } else if (action === "attended_no_form") {
            toast.success("Marked as attended");
        } else if (action === "no_show") {
            toast.success("Marked as no-show — reschedule email sent");
        }
    };

    const hasPast = pastDates.length > 0;
    const isEmpty = appointments.length === 0;

    return (
        <div className="p-6 overflow-auto h-[calc(100vh-3.5rem)]" data-testid="calendar-page">
            <div className="max-w-4xl">
            <div className="mb-6 flex items-start justify-between gap-4">
                <div>
                    <h1 className="font-heading text-3xl font-bold tracking-tight">Calendar</h1>
                    <p className="text-sm text-ink-muted mt-1">Candidate interview schedule. Olivia reads availability live during screening calls.</p>
                </div>
                <button
                    onClick={() => setShowAvailability((v) => !v)}
                    data-testid="toggle-availability-btn"
                    className="btn-secondary flex items-center gap-1.5"
                >
                    <GearSix size={14} weight="bold" /> {showAvailability ? "Hide" : "Manage"} availability
                </button>
            </div>

            <OutcomeDuePanel
                sessions={outcomeDue}
                scrollIntoView={searchParams.get("outcome_due") === "1"}
                onAttendanceAction={handleAttendanceAction}
                onBulkAttended={handleBulkAttended}
            />

            {showAvailability && (
                <div className="mb-6">
                    <AvailabilityManager
                        pipelines={ctxPipelines}
                        onClose={() => setShowAvailability(false)}
                        onSaved={handleAvailabilitySaved}
                    />
                </div>
            )}

            {loading && <div className="text-ink-muted text-sm">Loading…</div>}

            {!loading && loadError && (
                <div className="surface p-4 mb-4 flex flex-wrap items-center gap-3" data-testid="calendar-load-error">
                    <Warning size={18} weight="bold" className="text-[#FBBF24] flex-shrink-0" />
                    <div className="flex-1 min-w-0">
                        <div className="font-medium text-sm">Couldn't load the schedule</div>
                        <div className="text-xs text-ink-muted mt-0.5">{loadError}</div>
                    </div>
                    <button onClick={retryLoad} className="btn-secondary flex-shrink-0" data-testid="calendar-retry-btn">
                        Retry
                    </button>
                </div>
            )}

            {!loading && !loadError && isEmpty && (
                <div className="surface p-12 text-center" data-testid="no-appointments">
                    <CalendarIcon size={36} weight="duotone" className="mx-auto mb-3 text-ink-muted" />
                    <div className="font-heading text-lg">No appointments scheduled</div>
                    <div className="text-sm text-ink-muted mt-1">When candidates book interview slots, they'll appear here.</div>
                </div>
            )}

            {!loading && !isEmpty && (
                <div className="space-y-4">
                    {/* Past slots toggle */}
                    {hasPast && (
                        <div>
                            <button
                                onClick={() => setShowPast((v) => !v)}
                                className="flex items-center gap-1.5 text-xs text-ink-muted hover:text-ink transition-colors mb-3"
                                data-testid="toggle-past-btn"
                            >
                                <ClockClockwise size={13} />
                                {showPast
                                    ? "Hide previous slots"
                                    : `View previous slots (${pastDates.length} day${pastDates.length !== 1 ? "s" : ""})`}
                            </button>
                            {showPast && (
                                <div className="space-y-4 mb-4">
                                    {pastDates.map((d) => {
                                        // Dim only the days that are done. A past day still
                                        // owing outcomes is the actionable work on this page.
                                        const resolved = grouped[d].every((a) => a.attendance_status);
                                        return (
                                            <div key={d} className={resolved ? "opacity-60" : ""}>
                                                <DateSection
                                                    dateStr={d}
                                                    appointments={grouped[d]}
                                                    defaultOpen={false}
                                                    onAttendanceAction={handleAttendanceAction}
                                                />
                                            </div>
                                        );
                                    })}
                                </div>
                            )}
                        </div>
                    )}

                    {/* Today */}
                    {todayDates.map((d) => (
                        <DateSection
                            key={d}
                            dateStr={d}
                            appointments={grouped[d]}
                            defaultOpen={true}
                            isToday={true}
                            onAttendanceAction={handleAttendanceAction}
                        />
                    ))}
                    {todayDates.length === 0 && (
                        <div className="flex items-center gap-3 mb-2">
                            <div className="surface px-3 py-1.5 min-w-[72px] border-strokes/40">
                                <div className="text-[10px] uppercase tracking-widest text-brand-primary font-semibold">Today</div>
                                <div className="font-heading font-bold text-lg text-ink-muted">
                                    {fmtET(new Date(), { timeZone: tz, day: "numeric", month: "short" })}
                                </div>
                            </div>
                            <div className="flex-1 h-px bg-strokes" />
                            <span className="text-xs text-ink-muted italic">No interviews today</span>
                        </div>
                    )}

                    {/* Upcoming */}
                    {futureDates.length > 0 && (
                        <div className="space-y-3">
                            {futureDates.map((d) => (
                                <DateSection
                                    key={d}
                                    dateStr={d}
                                    appointments={grouped[d]}
                                    defaultOpen={false}
                                    onAttendanceAction={handleAttendanceAction}
                                />
                            ))}
                        </div>
                    )}
                </div>
            )}

            </div>{/* /max-w-4xl */}

            {/* Attendance confirmation dialog */}
            <AttendanceDialog
                open={!!pending}
                onOpenChange={(o) => { if (!o) setPending(null); }}
                action={pending?.action}
                candidates={pending?.candidates}
                onConfirm={handleAttendanceConfirm}
            />
        </div>
    );
}
