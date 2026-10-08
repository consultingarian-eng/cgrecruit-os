import { useEffect, useState } from "react";
import {
    AlertDialog,
    AlertDialogContent,
    AlertDialogHeader,
    AlertDialogTitle,
    AlertDialogFooter,
} from "@/components/ui/alert-dialog";
import { Envelope, ChatCircleText, ArrowRight, CheckCircle, XCircle, CalendarCheck } from "@phosphor-icons/react";
import SlotPicker from "@/components/SlotPicker";
import { fmtET, DEFAULT_TZ } from "@/lib/formatET";
import { usePipeline } from "@/lib/pipeline";

// Maps a destination stage / action to the email template that will fire
// and a human-friendly description of the consequences.
// Keep this in sync with backend default templates in /app/backend/models.py.
export const STAGE_ACTIONS = {
    APPOINTMENT: {
        label: "Appointment",
        templateKey: "approval",
        templateName: "Approval / Appointment Confirmed",
        sendsSms: true,
        intent: "primary",
        consequence: "Candidate will be sent the booking-confirmation email with the Zoom link, date and time.",
    },
    FORM: {
        label: "Form",
        templateKey: "form",
        templateName: "Final Questionnaire",
        sendsSms: true,
        intent: "primary",
        consequence: "Candidate will be emailed the link to your final questionnaire.",
    },
    CLOSE: {
        label: "To Close",
        templateKey: null,  // candidate gets the "Thanks, we'll call within 24h" page; no separate email needed
        templateName: null,
        sendsSms: false,
        intent: "primary",
        consequence: "Candidate will be moved to TO CLOSE — they'll see the 'we'll call you within 24 hours' confirmation page.",
    },
    SCREENING: {
        label: "Screening",
        templateKey: "warmup",
        templateName: "Screening Warmup",
        sendsSms: true,
        intent: "primary",
        consequence: "Candidate will get the warmup email/SMS, AND Olivia is auto-scheduled to call them ~5 minutes later. The card will show a live countdown until the dial.",
    },
    APPLICANT: { label: "Applicant", templateKey: null },
    TRAINING: { label: "Training", templateKey: null },
};

// Special action variants (overrides a stage's default mapping).
export const ACTION_VARIANTS = {
    deny: {
        label: "Deny candidate",
        destinationStage: "CLOSE",
        templateKey: "rejection",
        templateName: "Polite Rejection",
        sendsSms: true,
        intent: "danger",
        consequence: "Candidate will be moved to Close and sent the polite rejection email.",
    },
    approve: {
        label: "Approve for interview",
        destinationStage: "APPOINTMENT",
        templateKey: "approval",
        templateName: "Approval / Appointment Confirmed",
        sendsSms: true,
        intent: "primary",
        consequence: "Candidate will be moved to Appointment and sent the booking-confirmation email.",
    },
    ai_call: {
        label: "Start AI screening call",
        destinationStage: null,
        templateKey: null,
        sendsSms: false,
        intent: "primary",
        consequence: "Olivia will dial the candidate's phone now and conduct the screening conversation. The candidate will receive a pre-call SMS shortly before the AI dials.",
    },
    warmup_email: {
        label: "Send warmup email",
        destinationStage: null,
        templateKey: "warmup",
        templateName: "Screening Warmup",
        sendsSms: true,
        intent: "primary",
        consequence: "Candidate will receive the warmup email letting them know our AI assistant will call shortly.",
    },
    pencil_in: {
        label: "Pencil in interview",
        destinationStage: null,
        templateKey: "pencil_in",
        templateName: "Pencilled-In Appointment",
        sendsSms: true,
        intent: "primary",
        consequence: "Candidate will be emailed a tentative appointment with the proposed slot — they can confirm or reschedule from the email.",
    },
    hire: {
        label: "Hire candidate",
        destinationStage: "TRAINING",
        templateKey: "close_success",
        templateName: "Welcome / Hired",
        sendsSms: true,
        intent: "primary",
        consequence: "Candidate will be moved to TRAINING, sent the welcome email/SMS, and pushed to the configured CG1 webhook (if enabled). This is the final approval — make sure their form responses look good.",
    },
    decline: {
        label: "Decline candidate",
        destinationStage: "CLOSE",
        templateKey: "form_decline",
        templateName: "Final Decline",
        sendsSms: true,
        intent: "danger",
        consequence: "Candidate will be moved to CLOSE and sent the final decline email. They will NOT be re-contacted automatically.",
    },
};

const SESSION_SKIP_KEY = "kanban_skip_confirm_until_reload";
// The grant is stored as a timestamp rather than a flag: "the rest of this
// session" meant "until the tab closes", so a box ticked while clearing the 9am
// backlog was still sending emails and texts unasked at 5pm. Ten minutes covers
// the run of moves it was ticked for and nothing else. A legacy "1" parses to 1,
// which is already long expired.
const SKIP_TTL_MS = 10 * 60 * 1000;

/** @param action the STAGE_ACTIONS / ACTION_VARIANTS entry about to run. */
export function shouldSkipConfirm(action) {
    // Irreversible sends (Decline, Deny) always confirm — no checkbox disarms
    // a rejection email and SMS to a real person.
    if (action?.intent === "danger") return false;
    try {
        const grantedAt = Number(sessionStorage.getItem(SESSION_SKIP_KEY));
        if (!grantedAt) return false;
        if (Date.now() - grantedAt > SKIP_TTL_MS) {
            sessionStorage.removeItem(SESSION_SKIP_KEY);
            return false;
        }
        return true;
    } catch { return false; }
}
export function setSkipConfirmForSession(v) {
    try { v ? sessionStorage.setItem(SESSION_SKIP_KEY, String(Date.now())) : sessionStorage.removeItem(SESSION_SKIP_KEY); } catch { /* ignore */ }
}

export default function StageMoveConfirmDialog({ open, onOpenChange, action, candidate, onConfirm }) {
    const { timezone } = usePipeline() || {};
    const tz = timezone || DEFAULT_TZ;
    const [sendEmail, setSendEmail] = useState(true);
    const [skipFuture, setSkipFuture] = useState(false);
    // Slot picker state — only relevant when destination stage is APPOINTMENT.
    const [slotIso, setSlotIso] = useState(null);

    // Does this action involve booking an appointment slot? (Drag→APPOINTMENT,
    // Approve variant, or any STAGE_ACTIONS["APPOINTMENT"] click.)
    const needsSlot = !!action && (
        action.destinationStage === "APPOINTMENT" ||
        action.destinationStageLabel === "Appointment"
    );
    // Pre-existing slot from AI screening call (book_appointment tool) or a
    // previous reschedule. We pre-select it so the recruiter can keep or change.
    const existingSlot = candidate?.appointment_at || null;

    useEffect(() => {
        if (open) {
            setSendEmail(true);
            setSkipFuture(false);
            setSlotIso(existingSlot);
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [open, candidate?.id]);

    if (!action || !candidate) return null;

    const isDanger = action.intent === "danger";
    const fullName = `${candidate.first_name || ""} ${candidate.last_name || ""}`.trim() || "this candidate";
    const Icon = isDanger ? XCircle : CheckCircle;

    // Block confirm when an appointment is being booked but no slot is selected —
    // otherwise the approval email goes out with a blank "[Appointment Date/Time]"
    // placeholder, which is exactly what the user complained about.
    const slotMissing = needsSlot && !slotIso;

    const handleConfirm = () => {
        if (slotMissing) return;
        if (skipFuture) setSkipConfirmForSession(true);
        onConfirm({
            sendEmail: !!action.templateKey && sendEmail,
            appointmentAt: needsSlot ? slotIso : null,
        });
        onOpenChange(false);
    };

    return (
        <AlertDialog open={open} onOpenChange={onOpenChange}>
            <AlertDialogContent
                data-testid="stage-confirm-dialog"
                className="bg-[#101015] border border-strokes text-ink rounded-xl"
            >
                <AlertDialogHeader>
                    <AlertDialogTitle className="flex items-center gap-2 text-base">
                        <Icon
                            size={18}
                            weight="bold"
                            className={isDanger ? "text-[#F87171]" : "text-brand-primary"}
                        />
                        <span data-testid="stage-confirm-title">{action.label || "Confirm move"}</span>
                    </AlertDialogTitle>
                </AlertDialogHeader>

                <div className="space-y-3">
                    <div className="flex items-center gap-2 text-sm">
                        <span className="font-medium text-ink">{fullName}</span>
                        <ArrowRight size={14} className="text-ink-muted" />
                        <span
                            className="px-2 py-0.5 rounded text-xs font-semibold uppercase tracking-wider"
                            style={{
                                background: isDanger ? "rgba(239,68,68,0.14)" : "rgba(139,92,246,0.14)",
                                color: isDanger ? "#F87171" : "#A78BFA",
                            }}
                        >
                            {action.destinationStageLabel || action.label}
                        </span>
                    </div>

                    {action.consequence && (
                        <p className="text-sm text-ink-muted leading-relaxed">{action.consequence}</p>
                    )}

                    {needsSlot && existingSlot && (
                        <div
                            className="flex items-start gap-2 rounded-md border border-[rgba(16,185,129,0.30)] bg-[rgba(16,185,129,0.08)] px-3 py-2"
                            data-testid="ai-captured-slot-banner"
                        >
                            <CalendarCheck size={14} weight="bold" className="text-[#34D399] mt-0.5 flex-shrink-0" />
                            <div className="text-xs leading-relaxed">
                                <div className="font-semibold text-[#34D399]">Slot captured during AI screening call</div>
                                <div className="text-ink-muted">
                                    {fmtET(existingSlot, {
                                        timeZone: tz,
                                        weekday: "short", month: "short", day: "numeric",
                                        hour: "numeric", minute: "2-digit",
                                    })}
                                    {" "}— change below if you want a different time.
                                </div>
                            </div>
                        </div>
                    )}

                    {needsSlot && (
                        <SlotPicker
                            pipelineId={candidate.pipeline_id}
                            value={slotIso}
                            onChange={setSlotIso}
                        />
                    )}

                    {action.templateKey && (
                        <label className="flex items-start gap-2.5 p-3 rounded-md border border-strokes bg-[#0B0B0F] cursor-pointer hover:bg-[#0E0E13] transition-colors">
                            <input
                                type="checkbox"
                                checked={sendEmail}
                                onChange={(e) => setSendEmail(e.target.checked)}
                                data-testid="stage-confirm-send-email"
                                className="mt-0.5 accent-brand-primary"
                            />
                            <div className="flex-1">
                                <div className="flex items-center gap-1.5 text-sm">
                                    <Envelope size={13} weight="bold" className="text-brand-primary" />
                                    <span className="font-medium">Send the “{action.templateName}” email</span>
                                </div>
                                <div className="text-[11px] text-ink-muted mt-0.5">
                                    Candidate · {candidate.email || "no email on file"}
                                    {action.sendsSms && candidate.phone && (
                                        <span className="ml-2 inline-flex items-center gap-1">
                                            <ChatCircleText size={11} /> SMS will be sent to {candidate.phone}
                                        </span>
                                    )}
                                </div>
                            </div>
                        </label>
                    )}

                    {/* Not offered on danger actions — those confirm every time, so
                        the checkbox would promise something it can't deliver. */}
                    {!isDanger && (
                        <label className="flex items-center gap-2 text-[11px] text-ink-muted cursor-pointer select-none">
                            <input
                                type="checkbox"
                                checked={skipFuture}
                                onChange={(e) => setSkipFuture(e.target.checked)}
                                data-testid="stage-confirm-skip-session"
                                className="accent-brand-primary"
                            />
                            Don&apos;t show this confirmation again for the next 10 minutes
                        </label>
                    )}
                </div>

                <AlertDialogFooter className="gap-2">
                    <button
                        type="button"
                        onClick={() => onOpenChange(false)}
                        data-testid="stage-confirm-cancel"
                        className="btn-secondary !py-1.5 !px-3 text-xs"
                    >
                        Cancel
                    </button>
                    <button
                        type="button"
                        onClick={handleConfirm}
                        disabled={slotMissing}
                        data-testid="stage-confirm-submit"
                        className={`!py-1.5 !px-3 text-xs rounded-md font-medium disabled:opacity-50 disabled:cursor-not-allowed ${
                            isDanger ? "bg-[#DC2626] hover:bg-[#B91C1C] text-white" : "btn-primary"
                        }`}
                    >
                        {slotMissing
                            ? "Pick a slot to continue"
                            : isDanger ? "Deny & notify" : action.templateKey && sendEmail ? "Confirm & send email" : "Confirm move"}
                    </button>
                </AlertDialogFooter>
            </AlertDialogContent>
        </AlertDialog>
    );
}
