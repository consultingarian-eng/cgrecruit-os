import {
    Calendar,
    CheckCircle,
    Envelope,
    ProhibitInset,
    X,
} from "@phosphor-icons/react";
import { confirmDialog } from "@/components/ConfirmDialog";
import { etNaiveToUtcMs, fmtET } from "@/lib/formatET";
import { usePipeline } from "@/lib/pipeline";

const ATTENDANCE_LABELS = {
    attended_form: "Attended + Form",
    attended_no_form: "Attended (form pending)",
    no_show: "No-show",
};

// Attended + Form and No-show each fire an email AND a billable SMS the moment
// they land, so they confirm here the way the same actions do on Calendar.
// attended_no_form sends nothing, so it stays a single click.
const ATTENDANCE_CONFIRMS = {
    attended_form: (name) => ({
        title: "Attended — send the form?",
        description: `Marks ${name} as attended and moves them to the Form stage. The final questionnaire email and SMS are sent automatically.`,
        confirmLabel: "Mark attended & send",
    }),
    no_show: (name) => ({
        title: "Mark as no-show?",
        description: `Marks ${name} as a no-show. A reschedule email and SMS with 4 days of new slots are sent automatically.`,
        confirmLabel: "Mark no-show",
        destructive: true,
    }),
};

/** How long after the slot the attendance was actually marked up. The backend
 *  stamps attendance_recorded_at precisely so a session reviewed on the day can
 *  be told apart from one reconstructed from memory a week later; nothing has
 *  ever rendered it. */
function markLagLabel(candidate, tz) {
    if (!candidate.attendance_recorded_at || !candidate.appointment_at) return null;
    const slotMs = etNaiveToUtcMs(candidate.appointment_at, tz);
    const recordedMs = new Date(candidate.attendance_recorded_at).getTime();
    if (!slotMs || !Number.isFinite(recordedMs)) return null;
    const mins = Math.round((recordedMs - slotMs) / 60000);
    const abs = Math.abs(mins);
    if (abs < 5) return "Marked at the slot time";
    const span = abs < 60 ? `${abs}m` : abs < 1440 ? `${Math.round(abs / 60)}h` : `${Math.round(abs / 1440)}d`;
    return `Marked ${span} ${mins < 0 ? "before" : "after"} the slot`;
}

/**
 * Attendance review for an APPOINTMENT-stage candidate. Shows three actions
 * (Attended+Form / Attended / No-show) plus a Reschedule pill when the slot
 * hasn't been resolved yet. Decoupled from CandidateDrawer so the drawer
 * stays under 1000 lines.
 */
export default function AttendanceCard({ candidate, busyKey, onSet, onSendForm, onReschedule, onArchive }) {
    const { timezone } = usePipeline() || {};
    const status = candidate.attendance_status;
    const fmtAt = (iso) => fmtET(iso, { timeZone: timezone, weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) || iso;
    const noShowMode = status === "no_show";
    const attendedNoForm = status === "attended_no_form";
    const attendedForm = status === "attended_form";
    const markLag = status ? markLagLabel(candidate, timezone) : null;

    // Always confirms — deliberately not routed through the stage-move
    // skip-this-session flag, matching Calendar, where attendance is the one
    // review step that asks every time.
    const setAttendance = async (next) => {
        const cfg = ATTENDANCE_CONFIRMS[next];
        if (cfg && !(await confirmDialog(cfg(candidate.first_name || "this candidate")))) return;
        onSet(next);
    };

    return (
        <div
            className={`mt-3 rounded border p-3 ${
                noShowMode
                    ? "border-brand-danger/40 bg-brand-danger/5"
                    : attendedForm
                        ? "border-brand-success/30 bg-brand-success/5"
                        : "border-strokes bg-surface"
            }`}
            data-testid="attendance-card"
        >
            <div className="flex items-center justify-between gap-2 mb-2">
                <div className="flex items-center gap-1.5">
                    <Calendar size={12} weight="duotone" className="text-brand-primary" />
                    <span className="text-[10px] uppercase tracking-widest text-ink-muted font-semibold">Attendance</span>
                </div>
                {status && (
                    <span
                        data-testid="attendance-current-status"
                        className={`text-[10px] font-semibold px-2 py-0.5 rounded-full ${
                            noShowMode
                                ? "bg-brand-danger/20 text-brand-danger"
                                : attendedForm
                                    ? "bg-brand-success/20 text-brand-success"
                                    : "bg-brand-primary/15 text-brand-primary"
                        }`}
                    >
                        {ATTENDANCE_LABELS[status]}
                    </span>
                )}
            </div>

            {markLag && (
                <div className="text-[11px] text-ink-muted mb-2" data-testid="attendance-mark-lag">
                    {markLag}
                </div>
            )}

            {candidate.previous_appointment_at && candidate.rescheduled && (
                <div className="text-[11px] text-ink-muted mb-2" data-testid="attendance-rescheduled-from">
                    Rescheduled from {fmtAt(candidate.previous_appointment_at)} → {fmtAt(candidate.appointment_at)}
                </div>
            )}

            {/* Recruiter-initiated reschedule. Available before attendance review AND
                after a no-show (rebooking clears the no-show + re-arms reminders).
                Hidden once marked attended — the next step there is the form, not
                slot-shuffling. */}
            {(!status || noShowMode) && onReschedule && (
                <div className="flex items-center justify-between gap-2 mb-2 pb-2 border-b border-strokes/60">
                    <div className="text-[11px] text-ink-muted leading-snug min-w-0">
                        {noShowMode
                            ? "No-show — book them straight into a new slot (clears the no-show), or let them pick via the emailed link."
                            : "Need to move this slot? Pick a new time and the candidate will get an updated email + SMS."}
                    </div>
                    <button
                        type="button"
                        onClick={onReschedule}
                        data-testid="reschedule-appointment-btn"
                        className="btn-secondary !py-1 !px-2 text-[11px] flex items-center gap-1 whitespace-nowrap"
                    >
                        <Calendar size={11} weight="bold" /> Reschedule
                    </button>
                </div>
            )}

            <div className="grid grid-cols-3 gap-2">
                <ActionBtn
                    icon={<CheckCircle size={11} weight={attendedForm ? "fill" : "regular"} />}
                    label="Attended + Form"
                    onClick={() => setAttendance("attended_form")}
                    busy={busyKey === "attendance-attended_form"}
                    disabled={!!busyKey}
                    testid="attendance-attended-form-btn"
                />
                <ActionBtn
                    icon={<CheckCircle size={11} weight={attendedNoForm ? "fill" : "regular"} />}
                    label="Attended"
                    onClick={() => setAttendance("attended_no_form")}
                    busy={busyKey === "attendance-attended_no_form"}
                    disabled={!!busyKey}
                    testid="attendance-attended-no-form-btn"
                />
                <ActionBtn
                    icon={<X size={11} weight={noShowMode ? "fill" : "regular"} />}
                    label="No-show"
                    onClick={() => setAttendance("no_show")}
                    busy={busyKey === "attendance-no_show"}
                    disabled={!!busyKey}
                    tone="warn"
                    testid="attendance-no-show-btn"
                />
            </div>

            {attendedNoForm && (
                <div className="mt-2 pt-2 border-t border-strokes space-y-2">
                    <div className="text-[11px] text-ink-muted">
                        Attended — send the form to progress, or archive if not proceeding.
                    </div>
                    <div className="flex items-center gap-2">
                        <button
                            onClick={onSendForm}
                            disabled={!!busyKey}
                            data-testid="send-form-link-btn"
                            className="btn-primary !py-1 !px-2 text-[11px] flex items-center gap-1 whitespace-nowrap"
                        >
                            <Envelope size={11} /> {busyKey === "email-form" ? "Sending…" : "Send form"}
                        </button>
                        <button
                            onClick={onArchive}
                            disabled={!!busyKey}
                            data-testid="archive-attended-no-form-btn"
                            className="btn-secondary !py-1 !px-2 text-[11px] flex items-center gap-1 whitespace-nowrap text-brand-danger border-brand-danger/30 hover:bg-brand-danger/10"
                        >
                            <ProhibitInset size={11} /> {busyKey === "archive-attended" ? "Archiving…" : "Not progressing"}
                        </button>
                    </div>
                </div>
            )}

            {noShowMode && (
                <div className="mt-2 pt-2 border-t border-strokes text-[11px] text-ink-muted leading-relaxed" data-testid="attendance-no-show-note">
                    Reschedule email + SMS sent. The candidate has 4 days of new slots to choose from.
                </div>
            )}
        </div>
    );
}

// `tone="warn"` paints No-show amber, the colour Calendar gives the same action.
// Left plain, the button that texts the candidate a rebooking link looks exactly
// as safe as the one beside it that sends nothing.
function ActionBtn({ icon, label, onClick, busy, disabled, tone, testid }) {
    return (
        <button
            onClick={onClick}
            disabled={busy || disabled}
            data-testid={testid}
            className={`btn-secondary !py-2 !px-2 text-[11px] flex items-center justify-center gap-1.5 whitespace-nowrap ${
                tone === "warn" ? "text-amber-400 border-amber-400/30 hover:bg-amber-400/10" : ""
            }`}
        >
            {icon}
            <span>{busy ? "…" : label}</span>
        </button>
    );
}
