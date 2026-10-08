import { useEffect, useState, useRef } from "react";
import { fmtET } from "@/lib/formatET";
import { formatAmPm, isBeforeBusinessHours, pmSuggestion } from "@/lib/timeFormat";
import { usePipeline } from "@/lib/pipeline";
import api, { API } from "@/lib/api";
import { Sheet, SheetContent } from "@/components/ui/sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { VerdictBadge } from "@/components/KanbanBoard";
import StageMoveConfirmDialog, {
    STAGE_ACTIONS,
    ACTION_VARIANTS,
    shouldSkipConfirm,
} from "@/components/StageMoveConfirmDialog";
import SlotPickerDialog from "@/components/SlotPickerDialog";
import AttendanceCard from "@/components/AttendanceCard";
import CallAudioPlayer from "@/components/CallAudioPlayer";
import ConversationSummaryCard from "@/components/ConversationSummaryCard";
import {
    Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import {
    Phone, Envelope, ChatText, FileText, Sparkle, X,
    Robot, ChartBar, Clock, ArrowsClockwise,
    ClipboardText, Download, ThumbsUp, ThumbsDown, Trash, XCircle,
    PaperPlaneRight,
} from "@phosphor-icons/react";
import { toast } from "sonner";
import { confirmDialog } from "@/components/ConfirmDialog";
import { useAuth } from "@/lib/auth";

// No APPLICANT chip: the move endpoint refuses it as a destination, and the
// board has no column for it either. A candidate still sitting in APPLICANT
// simply has no chip highlighted — every stage below is a valid move forward.
const STAGES = ["SCREENING", "APPOINTMENT", "FORM", "CLOSE", "TRAINING"];

// The archive sweep distinguishes three ways an appointment can lapse; only the
// first is a real no-show, so spell out which one this was.
const ARCHIVE_REASON_LABELS = {
    no_show_not_rescheduled: "No-show — never rescheduled",
    cancelled_not_rebooked: "Told us they couldn't attend, never picked a new time",
    attendance_unrecorded: "Interview passed with no attendance recorded — outcome unknown",
};

// Channel styling. Deliberately distinct rather than pretty: the whole point of
// this view is telling at a glance whether a candidate typed something or said
// it out loud, and a uniform list of grey bubbles would defeat it.
const CHANNEL_STYLE = {
    chat: { label: "Chat", tint: "rgba(59,130,246,0.14)", ink: "#60A5FA" },
    sms: { label: "Text", tint: "rgba(16,185,129,0.14)", ink: "#34D399" },
    call: { label: "Call", tint: "rgba(168,85,247,0.14)", ink: "#C084FC" },
    system: { label: "System", tint: "rgba(148,163,184,0.12)", ink: "#94A3B8" },
};

function UnifiedTranscript({ transcript, loading, timezone }) {
    if (loading) return <div className="text-sm text-ink-muted">Loading conversation…</div>;
    const turns = transcript?.turns || [];
    if (!turns.length) {
        return (
            <div className="text-sm text-ink-muted">
                Nothing said yet — no calls, texts or chat messages on this candidate.
            </div>
        );
    }
    return (
        <div>
            <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
                <div className="label-overline">Conversation</div>
                <div className="flex items-center gap-2 flex-wrap">
                    {(transcript.channels_used || []).map((ch) => (
                        <span
                            key={ch}
                            className="status-pill text-[10px]"
                            style={{ background: CHANNEL_STYLE[ch]?.tint, color: CHANNEL_STYLE[ch]?.ink }}
                        >
                            {CHANNEL_STYLE[ch]?.label || ch} · {transcript.by_channel?.[ch] || 0}
                        </span>
                    ))}
                    <span className="text-[11px] text-ink-dim">
                        {transcript.candidate_replies} repl{transcript.candidate_replies === 1 ? "y" : "ies"}
                    </span>
                </div>
            </div>

            <div className="flex flex-col gap-2">
                {turns.map((t, i) => {
                    const style = CHANNEL_STYLE[t.channel] || CHANNEL_STYLE.system;
                    const isCandidate = t.who === "candidate";
                    // System lines — "Resume received via email", the marker that
                    // opens each call — are kept but set apart. They explain a
                    // thread that would otherwise be inexplicable, and they must
                    // not read as something the assistant said to a person.
                    if (t.who === "system") {
                        return (
                            <div
                                key={i}
                                className="flex items-center gap-2 text-[11px] text-ink-dim py-1"
                                data-testid={`transcript-system-${i}`}
                            >
                                <span className="h-px flex-1" style={{ background: "var(--strokes, #2A2A2A)" }} />
                                <span style={{ color: style.ink }}>{t.text}</span>
                                <span>{t.at ? fmtET(t.at, { timeZone: timezone }) : ""}</span>
                                <span className="h-px flex-1" style={{ background: "var(--strokes, #2A2A2A)" }} />
                            </div>
                        );
                    }
                    return (
                        <div
                            key={i}
                            className={`flex ${isCandidate ? "justify-start" : "justify-end"}`}
                            data-testid={`transcript-turn-${i}`}
                        >
                            <div className="max-w-[78%]">
                                <div
                                    className={`flex items-center gap-1.5 mb-0.5 text-[10px] ${isCandidate ? "" : "justify-end"}`}
                                >
                                    <span
                                        className="status-pill !px-1.5 !py-0 text-[9px]"
                                        style={{ background: style.tint, color: style.ink }}
                                    >
                                        {style.label}
                                    </span>
                                    <span className="text-ink-dim">
                                        {isCandidate ? "Candidate" : "Assistant"}
                                        {t.at ? ` · ${fmtET(t.at, { timeZone: timezone })}` : ""}
                                    </span>
                                </div>
                                <div
                                    className="rounded-lg px-3 py-2 text-sm leading-relaxed whitespace-pre-wrap break-words"
                                    style={
                                        isCandidate
                                            ? { background: "var(--surface-2, #1B1B1B)", color: "var(--ink, #E8E8E8)" }
                                            : { background: style.tint, color: "var(--ink, #E8E8E8)" }
                                    }
                                >
                                    {t.text}
                                </div>
                            </div>
                        </div>
                    );
                })}
            </div>
        </div>
    );
}

export default function CandidateDrawer({ candidateId, onClose, onUpdated, jobs = [] }) {
    const { canMutate } = useAuth();
    const { timezone, pipelines = [], activePipelineId } = usePipeline() || {};
    const [candidate, setCandidate] = useState(null);
    const [conversations, setConversations] = useState([]);
    const [communications, setCommunications] = useState([]);
    const [transcript, setTranscript] = useState(null);
    const [transcriptLoading, setTranscriptLoading] = useState(false);
    const [expandedComms, setExpandedComms] = useState({});
    const [loading, setLoading] = useState(false);
    const [busyKey, setBusyKey] = useState(null);
    // Pending confirmation: { action, candidate, run: ({ sendEmail }) => Promise<void> }
    const [pending, setPending] = useState(null);
    // Reschedule modal — opens the SlotPickerDialog scoped to the candidate's pipeline.
    const [rescheduleOpen, setRescheduleOpen] = useState(false);
    // Delete confirm — separate from the stage-move confirm to avoid mixing UX patterns.
    const [confirmDelete, setConfirmDelete] = useState(false);
    // Hire dialog — collect training start date/time before firing the API.
    const [hireDialogOpen, setHireDialogOpen] = useState(false);
    const [trainingDate, setTrainingDate] = useState("");
    const [trainingTime, setTrainingTime] = useState("09:00");
    // SMS thread (TRAINING candidates only)
    const [smsThread, setSmsThread] = useState(null);
    const [smsLoading, setSmsLoading] = useState(false);
    const [smsSending, setSmsSending] = useState(false);
    const [smsDraft, setSmsDraft] = useState("");
    const smsScrollRef = useRef(null);
    // Email reply thread (all stages)
    const [emailThread, setEmailThread] = useState(null);
    const [emailLoading, setEmailLoading] = useState(false);
    const [emailError, setEmailError] = useState(null);
    const emailScrollRef = useRef(null);

    /** Hard-delete the candidate. Backend cancels pending reminders/retry/auto-promote,
     * removes the candidate doc, conversations, and communications. Closes the drawer
     * and refreshes the dashboard so the card disappears immediately. */
    const deleteCandidate = async () => {
        setBusyKey("delete");
        try {
            await api.delete(`/candidates/${candidateId}`);
            const fullName = `${candidate?.first_name || ""} ${candidate?.last_name || ""}`.trim() || "Candidate";
            toast.success(`${fullName} deleted`);
            setConfirmDelete(false);
            onClose?.();
            await onUpdated?.();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to delete");
        } finally { setBusyKey(null); }
    };

    /** Soft-reject (archive). Hides the candidate from the dashboard but keeps
     * the doc + transcripts + comms history. Recruiters can restore later
     * via the "Rejected" filter view → drawer → Restore.
     * Less destructive than delete — preferred path for "no thanks" outcomes. */
    const archiveCandidate = async () => {
        setBusyKey("archive");
        try {
            await api.post(`/candidates/${candidateId}/archive`, { reason: "manual" });
            const fullName = `${candidate?.first_name || ""} ${candidate?.last_name || ""}`.trim() || "Candidate";
            toast.success(`${fullName} archived — find them under Rejected filter to restore`);
            onClose?.();
            await onUpdated?.();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to archive");
        } finally { setBusyKey(null); }
    };

    /** Archive after attended interview but decided not to progress.
     * Preserves attendance_status="attended_no_form" so the candidate still
     * counts toward attended metrics, but removes them from the active board. */
    const archiveAttended = async () => {
        setBusyKey("archive-attended");
        try {
            await api.post(`/candidates/${candidateId}/archive`, { reason: "attended_no_form" });
            const fullName = `${candidate?.first_name || ""} ${candidate?.last_name || ""}`.trim() || "Candidate";
            toast.success(`${fullName} archived — attended but not progressed`);
            onClose?.();
            await onUpdated?.();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to archive");
        } finally { setBusyKey(null); }
    };

    /** Restore an archived candidate. Brings them back to the active pipeline
     * with all data intact. Doesn't auto-re-queue calls (would surprise users
     * if they restore a long-archived candidate). */
    const restoreCandidate = async () => {
        setBusyKey("restore");
        try {
            await api.post(`/candidates/${candidateId}/restore`);
            const fullName = `${candidate?.first_name || ""} ${candidate?.last_name || ""}`.trim() || "Candidate";
            toast.success(`${fullName} restored — back on the pipeline`);
            await onUpdated?.();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to restore");
        } finally { setBusyKey(null); }
    };

    /** Re-read the drawer after an action that changed the candidate.
     *
     * This replaces calls to a `load()` that was never defined anywhere in this
     * file — not a prop, not an import. Every `await load()` threw a
     * ReferenceError straight into the caller's catch, which reported the whole
     * action as failed even though its POST had returned 200. A recruiter
     * rescheduling a candidate saw "Reschedule failed" three times and
     * retried; all three succeeded, and the candidate got three "your interview
     * has been moved" emails and three texts.
     *
     * Errors are swallowed on purpose. The action already succeeded by the time
     * we get here, so a failed refresh means a stale panel — never a failure to
     * report to the user. Deliberately narrower than the mount effect too: no
     * transcript fetch and no conversation auto-sync, which are for opening the
     * drawer rather than reflecting a slot change. */
    const refresh = async () => {
        try {
            const [c, cv, cm] = await Promise.all([
                api.get(`/candidates/${candidateId}`),
                api.get(`/candidates/${candidateId}/conversations`),
                api.get(`/candidates/${candidateId}/communications`),
            ]);
            setCandidate(c.data);
            setConversations(cv.data);
            setCommunications(cm.data);
        } catch {
            /* stale panel only — the action itself already went through */
        }
    };

    /** Recruiter-initiated reschedule. Hits POST /api/candidates/{id}/reschedule
     * which updates appointment_at, cancels old reminders, schedules new ones,
     * and fires the `appointment_rescheduled` email + SMS template. */
    const reschedule = async (newAt) => {
        setBusyKey("reschedule");
        try {
            const r = await api.post(`/candidates/${candidateId}/reschedule`, {
                appointment_at: newAt,
                send_notification: true,
            });
            setRescheduleOpen(false);
            const emailStatus = r.data?.notification?.email?.status;
            const smsStatus = r.data?.notification?.sms?.status;
            const bits = [];
            if (emailStatus === "sent" || emailStatus === "queued") bits.push("email sent");
            if (smsStatus === "sent" || smsStatus === "queued") bits.push("SMS sent");
            toast.success(`Rescheduled${bits.length ? ` — ${bits.join(" + ")}` : ""}`);
            await refresh();
            await onUpdated?.();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Reschedule failed");
        } finally {
            setBusyKey(null);
        }
    };

    /**
     * Wrap any side-effecting action (call / email / stage move) in the same
     * confirmation dialog used by the Kanban. `run` receives `{ sendEmail }`
     * so the handler can decide whether to actually fire the email.
     * Honors the session-skip flag (set on Kanban) so the user isn't pestered
     * twice for the same workflow.
     */
    const requestAction = (variantKey, run) => {
        const variant = ACTION_VARIANTS[variantKey];
        if (!variant) {
            // Fallback: no confirmation, just run.
            run({ sendEmail: true });
            return;
        }
        // If the action lands on APPOINTMENT and the candidate doesn't already
        // have a slot, force the modal even when "skip-this-session" is on so
        // we never send a blank approval email.
        const needsSlot = variant.destinationStage === "APPOINTMENT";
        const hasSlot = !!candidate?.appointment_at;
        if (shouldSkipConfirm(variant) && !(needsSlot && !hasSlot)) {
            run({ sendEmail: true, appointmentAt: hasSlot ? candidate.appointment_at : null });
            return;
        }
        const action = {
            ...variant,
            destinationStageLabel: variant.destinationStage
                ? STAGE_ACTIONS[variant.destinationStage]?.label || variant.destinationStage
                : null,
        };
        setPending({ action, candidate, run });
    };

    const handleConfirm = async ({ sendEmail, appointmentAt }) => {
        if (!pending) return;
        const { run } = pending;
        setPending(null);
        try {
            await run({ sendEmail, appointmentAt });
        } catch (e) {
            // run() handlers already toast their own errors
            // eslint-disable-next-line no-console
            console.warn("confirmed action failed:", e);
        }
    };

    const loadSmsThread = async (id) => {
        setSmsLoading(true);
        try {
            const r = await api.get(`/candidates/${id}/sms-thread`);
            setSmsThread(r.data);
            setTimeout(() => smsScrollRef.current?.scrollTo({ top: 99999, behavior: "smooth" }), 60);
        } catch { /* no thread yet — leave null */ }
        finally { setSmsLoading(false); }
    };

    const loadEmailThread = async (id) => {
        setEmailLoading(true);
        setEmailError(null);
        try {
            const r = await api.get(`/candidates/${id}/email-thread`);
            setEmailThread(r.data);
            setTimeout(() => emailScrollRef.current?.scrollTo({ top: 99999, behavior: "smooth" }), 60);
        } catch (e) {
            // A swallowed failure here used to render as "No emails yet" —
            // indistinguishable from an empty thread. Say what happened.
            setEmailThread(null);
            setEmailError(e?.response?.status ? `Couldn't load emails (HTTP ${e.response.status})` : "Couldn't load emails — network error");
        }
        finally { setEmailLoading(false); }
    };

    const resendSms = async () => {
        if (smsSending || !candidateId) return;
        setSmsSending(true);
        try {
            await api.post(`/candidates/${candidateId}/sms/resend`);
            toast.success("Confirmation SMS sent");
            await loadSmsThread(candidateId);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Send failed");
        } finally { setSmsSending(false); }
    };

    /** Send a free-text SMS reply to this candidate from their office's SMS line. */
    const sendSmsReply = async () => {
        const body = smsDraft.trim();
        if (smsSending || !candidateId || !body) return;
        setSmsSending(true);
        try {
            await api.post(`/candidates/${candidateId}/sms`, { body });
            setSmsDraft("");
            await loadSmsThread(candidateId);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Send failed");
        } finally { setSmsSending(false); }
    };


    useEffect(() => {
        if (!candidateId) { setCandidate(null); setSmsThread(null); setEmailThread(null); setEmailError(null); setTranscript(null); return; }
        setLoading(true);
        // Reset per-candidate threads immediately — switching cards without
        // closing the drawer must never show the previous candidate's emails.
        setEmailThread(null);
        setEmailError(null);
        Promise.all([
            api.get(`/candidates/${candidateId}`),
            api.get(`/candidates/${candidateId}/conversations`),
            api.get(`/candidates/${candidateId}/communications`),
        ])
            .then(async ([c, cv, cm]) => {
                setCandidate(c.data);
                setConversations(cv.data);
                setCommunications(cm.data);
                loadSmsThread(candidateId);
                // Eager-load the email thread too. It was lazy (first click on
                // the Email tab) and a failed fetch was silent, so the tab
                // could sit on "No emails yet" over a thread that existed.
                loadEmailThread(candidateId);
                // Fetched separately rather than added to the Promise.all above:
                // it merges three collections server-side and is the slowest of
                // the four, and nothing else on the drawer waits on it.
                setTranscriptLoading(true);
                api.get(`/candidates/${candidateId}/transcript`)
                    .then((t) => setTranscript(t.data))
                    .catch(() => setTranscript(null))
                    .finally(() => setTranscriptLoading(false));
                // Auto-sync the latest conversation if it has an ElevenLabs ID but no transcript yet.
                // This makes the call audio + summary appear immediately when opening the card,
                // without the user having to click "Sync".
                const latest = (cv.data || [])[0];
                if (latest && latest.elevenlabs_conversation_id && (!latest.transcript || latest.transcript.length === 0)) {
                    try {
                        await api.post(`/candidates/${candidateId}/sync-conversation`);
                        const [c2, cv2] = await Promise.all([
                            api.get(`/candidates/${candidateId}`),
                            api.get(`/candidates/${candidateId}/conversations`),
                        ]);
                        setCandidate(c2.data);
                        setConversations(cv2.data);
                    } catch { /* call may not be complete yet — silent */ }
                }
            })
            .catch(() => toast.error("Failed to load candidate"))
            .finally(() => setLoading(false));
    }, [candidateId]);

    // Auto-refresh while a call is live — poll every 5s until the conversation
    // reaches a terminal state (completed / no_answer / etc.).
    const _latestConvStatus = (conversations || [])[0]?.status;
    useEffect(() => {
        const LIVE = ["in_progress", "initiated", "ringing", "queued"];
        if (!candidateId || !LIVE.includes(_latestConvStatus)) return;
        const iv = setInterval(async () => {
            try {
                const [c, cv] = await Promise.all([
                    api.get(`/candidates/${candidateId}`),
                    api.get(`/candidates/${candidateId}/conversations`),
                ]);
                setCandidate(c.data);
                setConversations(cv.data);
            } catch { /* silent — will retry next tick */ }
        }, 5000);
        return () => clearInterval(iv);
    }, [candidateId, _latestConvStatus]);

    if (!candidateId) return null;

    /** Move candidate to a new stage. Honors `send_email_template` if provided.
     * `appointmentAt` (ISO string) is set when the destination is APPOINTMENT
     * — we always carry it through so the approval email renders the real date. */
    const moveStage = async (stage, sendEmailTemplate, appointmentAt) => {
        setBusyKey(`stage-${stage}`);
        try {
            const payload = { stage };
            if (sendEmailTemplate) payload.send_email_template = sendEmailTemplate;
            if (appointmentAt) payload.appointment_at = appointmentAt;
            const r = await api.post(`/candidates/${candidateId}/move`, payload);
            setCandidate(r.data.candidate);
            // Refresh comms log so newly-sent emails appear without a manual reload.
            try {
                const cm = await api.get(`/candidates/${candidateId}/communications`);
                setCommunications(cm.data);
            } catch { /* non-fatal */ }
            onUpdated?.();
            toast.success(sendEmailTemplate ? `Moved to ${stage} & email sent` : `Moved to ${stage}`);
        } catch { toast.error("Failed to move"); }
        finally { setBusyKey(null); }
    };

    /** Stage-pill click handler: confirms when the destination triggers an email
     * OR when the destination is APPOINTMENT and the candidate doesn't have a
     * slot yet (we force the picker so the email doesn't go out blank). */
    const updateStage = (stage) => {
        if (stage === candidate?.stage) return;
        const stageAction = STAGE_ACTIONS[stage] || {};
        const needsSlot = stage === "APPOINTMENT";
        const hasSlot = !!candidate?.appointment_at;
        const forceModal = needsSlot && !hasSlot;

        if (!stageAction.templateKey && !forceModal) {
            // No automated email, no slot needed — silently move.
            moveStage(stage);
            return;
        }
        if (shouldSkipConfirm(stageAction) && !forceModal) {
            moveStage(
                stage,
                stageAction.templateKey,
                hasSlot ? candidate.appointment_at : undefined,
            );
            return;
        }
        const action = {
            ...stageAction,
            label: `Move to ${stageAction.label}`,
            destinationStageLabel: stageAction.label,
            destinationStage: stage,
        };
        setPending({
            action,
            candidate,
            run: ({ sendEmail, appointmentAt }) => moveStage(
                stage,
                sendEmail ? stageAction.templateKey : undefined,
                appointmentAt,
            ),
        });
    };

    /** Mark attendance for an interview appointment. */
    const setAttendance = async (status) => {
        const labels = {
            attended_form: "Attended (form completed)",
            attended_no_form: "Attended (form pending)",
            no_show: "No-show — sending reschedule link",
        };
        setBusyKey(`attendance-${status}`);
        try {
            const r = await api.post(`/candidates/${candidateId}/attendance`, { status });
            setCandidate(r.data.candidate);
            try {
                const cm = await api.get(`/candidates/${candidateId}/communications`);
                setCommunications(cm.data);
            } catch { /* non-fatal */ }
            onUpdated?.();
            toast.success(labels[status] || "Attendance recorded");
        } catch { toast.error("Failed to record attendance"); }
        finally { setBusyKey(null); }
    };

    /** Manually trigger an immediate retry call for a stuck candidate. */
    const retryCallNow = async () => {
        setBusyKey("retry-call");
        try {
            await api.post(`/candidates/${candidateId}/call`);
            onUpdated?.();
            toast.success("Call triggered");
        } catch { toast.error("Failed to trigger call"); }
        finally { setBusyKey(null); }
    };

    /** Manually trigger a no-show revival call — the dedicated agent hears out
     * why they missed and rebooks them if still interested. Archived-only. */
    const revivalCallNow = async () => {
        setBusyKey("revival-call");
        try {
            const r = await api.post(`/candidates/${candidateId}/revival-call`);
            onUpdated?.();
            await refresh();
            toast.success(r.data?.status === "initiated" ? "Revival call placed" : `Revival call: ${r.data?.reason || r.data?.status}`);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to place revival call");
        } finally { setBusyKey(null); }
    };

    /** Resend the finish-your-screening link to a candidate whose screening is unfinished. */
    const sendScreeningLink = async () => {
        setBusyKey("screening-link");
        try {
            await api.post(`/candidates/${candidateId}/send-screening-link`);
            try {
                const cm = await api.get(`/candidates/${candidateId}/communications`);
                setCommunications(cm.data);
            } catch { /* non-fatal */ }
            toast.success("Screening link sent");
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Couldn't send");
        } finally {
            setBusyKey(null);
        }
    };

    /** Manually send the slot-picker scheduling link to a candidate stuck in appointment_pending. */
    const sendSlotPickerLink = async () => {
        setBusyKey("slot-picker");
        try {
            await api.post(`/candidates/${candidateId}/send-slot-picker`);
            try {
                const cm = await api.get(`/candidates/${candidateId}/communications`);
                setCommunications(cm.data);
            } catch { /* non-fatal */ }
            toast.success("Scheduling link sent");
        } catch { toast.error("Failed to send scheduling link"); }
        finally { setBusyKey(null); }
    };

    /** Send the form link via email/SMS to a candidate already in APPOINTMENT. */
    const sendFormLink = async () => {
        setBusyKey("email-form");
        try {
            await api.post(`/candidates/${candidateId}/email`, { template_key: "form" });
            try {
                const cm = await api.get(`/candidates/${candidateId}/communications`);
                setCommunications(cm.data);
            } catch { /* non-fatal */ }
            toast.success("Form link sent");
        } catch { toast.error("Failed to send form link"); }
        finally { setBusyKey(null); }
    };

    /** Hire the candidate — collects start date/time, fires training email + CG1 new-starter. */
    const hireCandidate = async () => {
        setBusyKey("hire");
        setHireDialogOpen(false);
        try {
            // Combine date + time into an ISO string in local time.
            let training_start_at = "";
            if (trainingDate) {
                const dt = new Date(`${trainingDate}T${trainingTime || "09:00"}:00`);
                training_start_at = dt.toISOString();
            }
            const r = await api.post(`/candidates/${candidateId}/hire`, { training_start_at });
            setCandidate(r.data.candidate);
            try {
                const cm = await api.get(`/candidates/${candidateId}/communications`);
                setCommunications(cm.data);
            } catch { /* non-fatal */ }
            onUpdated?.();
            const cg1 = r.data.cg1_new_starter_id;
            if (cg1) toast.success("Hired — start date confirmed & pushed to CG1");
            else toast.success("Hired! (Set CG1 integration in Settings to push automatically)");
        } catch (e) { toast.error(e?.response?.data?.detail || "Failed to hire candidate"); }
        finally { setBusyKey(null); }
    };

    /** Decline the candidate post-form — moves to CLOSE and always fires form_decline email + SMS. */
    const declineCandidate = async () => {
        setBusyKey("decline");
        try {
            const payload = { stage: "CLOSE", send_email_template: "form_decline" };
            const r = await api.post(`/candidates/${candidateId}/move`, payload);
            setCandidate(r.data.candidate);
            try {
                const cm = await api.get(`/candidates/${candidateId}/communications`);
                setCommunications(cm.data);
            } catch { /* non-fatal */ }
            onUpdated?.();
            toast.success("Declined & email sent");
        } catch { toast.error("Failed to decline"); }
        finally { setBusyKey(null); }
    };

    /** Reject a TO CLOSE candidate — confirm, send the rejection email (no SMS),
     *  and soft-archive them so they drop off the board. */
    const rejectCandidate = async () => {
        const name = candidate?.first_name || "this candidate";
        const ok = await confirmDialog({
            title: "Reject candidate?",
            description: `Sends ${name} a rejection email and archives them — they won't move forward.`,
            confirmLabel: "Reject & email",
            destructive: true,
        });
        if (!ok) return;
        setBusyKey("reject");
        try {
            await api.post(`/candidates/${candidateId}/reject`);
            toast.success("Rejected — email sent");
            onUpdated?.();
            onClose?.();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to reject");
        } finally { setBusyKey(null); }
    };

    const syncConversation = async () => {
        setBusyKey("sync");
        try {
            const r = await api.post(`/candidates/${candidateId}/sync-conversation`);
            const cv = await api.get(`/candidates/${candidateId}/conversations`);
            setConversations(cv.data);
            toast.success("Transcript synced");
        } catch { toast.error("Sync failed (call may not be complete)"); }
        finally { setBusyKey(null); }
    };

    const rescore = async () => {
        if (!candidate?.job_id) { toast.warning("Assign a job first"); return; }
        setBusyKey("score");
        try {
            const r = await api.post(`/candidates/${candidateId}/score`);
            toast.success(`Smart Score: ${r.data.score}`);
            const c = await api.get(`/candidates/${candidateId}`);
            setCandidate(c.data);
            onUpdated?.();
        } catch { toast.error("Scoring failed"); }
        finally { setBusyKey(null); }
    };

    const job = jobs.find((j) => j.id === candidate?.job_id);
    const initials = `${candidate?.first_name?.[0] || ""}${candidate?.last_name?.[0] || ""}`.toUpperCase();
    // The bell and the needs-attention panel deep-link straight into this drawer
    // for candidates in the other office, on top of whichever board is behind it
    // — and Archive, Delete and the stage pills are one click away. Name the
    // office whenever it isn't the one the recruiter thinks they're looking at.
    const otherOffice = candidate?.pipeline_id && candidate.pipeline_id !== activePipelineId
        ? pipelines.find((p) => p.id === candidate.pipeline_id)
        : null;

    return (
        <Sheet open={!!candidateId} onOpenChange={(o) => !o && onClose()}>
            <SheetContent className="w-full sm:max-w-2xl bg-[#0E0E11] border-strokes p-0 overflow-y-auto" side="right" data-testid="candidate-drawer">
                {loading || !candidate ? (
                    <div className="p-8 text-ink-muted text-sm">Loading…</div>
                ) : (
                    <div className="flex flex-col h-full">
                        {/* Header */}
                        <div className="px-6 py-5 border-b border-strokes bg-[#141519]">
                            <div className="flex items-start justify-between gap-4">
                                <div className="flex items-center gap-3 min-w-0">
                                    <div className="w-12 h-12 rounded-full bg-surface-active border border-strokes flex items-center justify-center text-base font-semibold">
                                        {initials || "?"}
                                    </div>
                                    <div className="min-w-0">
                                        <div className="flex items-center gap-2 min-w-0">
                                            <h2 className="font-heading text-2xl font-bold tracking-tight truncate" data-testid="drawer-candidate-name">
                                                {candidate.first_name} {candidate.last_name}
                                            </h2>
                                            {otherOffice && (
                                                <span
                                                    className="status-pill text-[10px] shrink-0"
                                                    style={{ background: "rgba(245,158,11,0.15)", color: "#FBBF24" }}
                                                    data-testid="drawer-other-office"
                                                    title="This candidate belongs to another office — the board behind this drawer is a different one"
                                                >
                                                    {otherOffice.name}
                                                </span>
                                            )}
                                        </div>
                                        <div className="flex items-center gap-3 text-xs text-ink-muted mt-0.5">
                                            {candidate.email && <span className="flex items-center gap-1"><Envelope size={11} /> {candidate.email}</span>}
                                            {candidate.phone && <span className="flex items-center gap-1"><Phone size={11} /> {candidate.phone}</span>}
                                        </div>
                                        <ReferredByEditor candidate={candidate} canMutate={canMutate} onUpdated={setCandidate} />
                                        <ContactEditor candidate={candidate} canMutate={canMutate} onUpdated={setCandidate} />
                                    </div>
                                </div>
                                <div className="flex items-center gap-1">
                                    {canMutate && (candidate.archived_at ? (
                                        // Archived state — show Restore button instead of Archive.
                                        <button
                                            onClick={restoreCandidate}
                                            disabled={busyKey === "restore"}
                                            data-testid="drawer-restore-btn"
                                            title="Restore — bring this candidate back to the active pipeline"
                                            className="px-2 py-1 text-[11px] font-medium bg-[rgba(34,197,94,0.15)] text-emerald-400 hover:bg-[rgba(34,197,94,0.25)] rounded transition-colors disabled:opacity-50"
                                        >
                                            {busyKey === "restore" ? "Restoring…" : "↻ Restore"}
                                        </button>
                                    ) : (
                                        <button
                                            onClick={archiveCandidate}
                                            disabled={busyKey === "archive"}
                                            data-testid="drawer-archive-btn"
                                            title="Archive — soft reject. Hides from dashboard but keeps the data so you can restore later"
                                            className="px-2 py-1 text-[11px] font-medium text-ink-muted hover:text-ink hover:bg-surface-hover rounded transition-colors disabled:opacity-50"
                                        >
                                            {busyKey === "archive" ? "Archiving…" : "Archive"}
                                        </button>
                                    ))}
                                    {canMutate && (
                                        <button
                                            onClick={() => setConfirmDelete(true)}
                                            data-testid="drawer-delete-btn"
                                            title="Permanently delete — wipes all data. Use Archive instead if you might restore later."
                                            className="p-1 hover:bg-brand-danger/15 rounded text-ink-muted hover:text-brand-danger transition-colors"
                                        >
                                            <Trash size={15} />
                                        </button>
                                    )}
                                    <button onClick={onClose} className="p-1 hover:bg-surface-hover rounded text-ink-muted">
                                        <X size={18} />
                                    </button>
                                </div>
                            </div>

                            {/* Stage selector — hidden for viewers (read-only role). */}
                            {canMutate && <div className="flex flex-wrap gap-1 mt-4">
                                {(() => {
                                    const currentIdx = STAGES.indexOf(candidate.stage);
                                    return STAGES.map((s, idx) => {
                                        const isCurrent = candidate.stage === s;
                                        const isPast = currentIdx >= 0 && idx < currentIdx;
                                        return (
                                            <button
                                                key={s}
                                                onClick={() => !isPast && updateStage(s)}
                                                disabled={busyKey === `stage-${s}` || isPast}
                                                data-testid={`drawer-stage-${s}`}
                                                title={isPast ? "Earlier stages are locked — move forward only" : undefined}
                                                className={`text-[11px] px-2.5 py-1 rounded border transition-colors ${
                                                    isCurrent
                                                        ? "bg-brand-primary border-brand-primary text-white"
                                                        : isPast
                                                            ? "bg-transparent border-strokes text-ink-muted/40 opacity-50 cursor-not-allowed"
                                                            : "bg-transparent border-strokes text-ink-muted hover:border-strokes-focus hover:text-ink"
                                                }`}
                                            >
                                                {s}
                                            </button>
                                        );
                                    });
                                })()}
                            </div>}

                            {/* Archived state banner — when soft-rejected, show
                                a high-contrast banner so the recruiter knows
                                this candidate is hidden from the kanban + all
                                pending dialer/retry jobs were cancelled.
                                Restore button is also up in the header. */}
                            {candidate.archived_at && (
                                <div
                                    data-testid="drawer-archived-banner"
                                    className="mt-3 px-3 py-2.5 rounded-md border border-strokes bg-[rgba(244,63,94,0.08)] text-xs flex items-start gap-2.5"
                                >
                                    <span className="text-rose-400 font-semibold">⚠ Archived</span>
                                    <span className="flex-1 text-ink-muted leading-relaxed">
                                        Hidden from the dashboard, all pending calls/SMS cancelled.
                                        Restore via the button at the top — data + transcripts kept.
                                        {candidate.archived_reason && candidate.archived_reason !== "manual" && (
                                            <span className="block mt-1 italic">Reason: {ARCHIVE_REASON_LABELS[candidate.archived_reason] || candidate.archived_reason}</span>
                                        )}
                                        {(candidate.revival_call_attempts > 0 || candidate.revival_outcome) && (
                                            <span className="block mt-1">
                                                Revival: {candidate.revival_call_attempts || 0} call{(candidate.revival_call_attempts || 0) !== 1 ? "s" : ""}
                                                {candidate.revival_outcome && <> · outcome: <span className="font-semibold text-ink">{candidate.revival_outcome.replace(/_/g, " ")}</span></>}
                                                {candidate.revival_feedback && <span className="block italic mt-0.5">"{candidate.revival_feedback}"</span>}
                                            </span>
                                        )}
                                    </span>
                                    {canMutate && candidate.archived_reason === "no_show_not_rescheduled" && candidate.phone &&
                                        !["rebooked", "declined"].includes(candidate.revival_outcome) && (
                                        <button
                                            onClick={revivalCallNow}
                                            disabled={busyKey === "revival-call"}
                                            data-testid="drawer-revival-call-btn"
                                            title="Call this no-show now with the revival agent — hears them out and rebooks if still interested"
                                            className="shrink-0 px-2 py-1 text-[11px] font-medium bg-brand-primary/15 text-brand-primary hover:bg-brand-primary/25 rounded transition-colors disabled:opacity-50"
                                        >
                                            {busyKey === "revival-call" ? "Calling…" : "Revival call now"}
                                        </button>
                                    )}
                                </div>
                            )}

                            {/* AI verdict + call summary — only when there's no full conversation card yet */}
                            {candidate.verdict && (!conversations.length || !conversations[0]?.transcript?.length) && (
                                <div className="mt-4 surface p-4" data-testid="candidate-verdict-card">
                                    <div className="flex items-center gap-2 mb-2">
                                        <VerdictBadge verdict={candidate.verdict} size="lg" />
                                        <span className="text-[10px] uppercase tracking-widest text-ink-muted font-semibold">AI Assessment</span>
                                    </div>
                                    {candidate.call_summary && (
                                        <p className="text-xs text-ink leading-relaxed">{candidate.call_summary}</p>
                                    )}
                                </div>
                            )}

                            {/* Smart Score */}
                            {candidate.smart_score != null && (
                                <div className="mt-4 surface p-3 flex items-center gap-3" data-testid="smart-score-card">
                                    <div className="w-12 h-12 rounded-md flex items-center justify-center font-heading text-xl font-bold flex-shrink-0"
                                        style={{
                                            background: candidate.smart_score >= 80 ? "rgba(16,185,129,0.15)" : candidate.smart_score >= 60 ? "rgba(59,130,246,0.15)" : "rgba(245,158,11,0.15)",
                                            color: candidate.smart_score >= 80 ? "#10B981" : candidate.smart_score >= 60 ? "#3B82F6" : "#F59E0B",
                                        }}>
                                        {candidate.smart_score}
                                    </div>
                                    <div className="flex-1 min-w-0">
                                        <div className="text-[10px] uppercase tracking-widest text-ink-muted font-semibold mb-0.5">Smart Score · {job?.title || "—"}</div>
                                        <div className="text-xs text-ink leading-snug">{candidate.smart_score_rationale || "—"}</div>
                                    </div>
                                    <button
                                        onClick={rescore}
                                        disabled={busyKey === "score"}
                                        data-testid="rescore-btn"
                                        className="btn-secondary !py-1 !px-2 text-xs flex items-center gap-1"
                                    >
                                        <ArrowsClockwise size={11} weight="bold" /> Rescore
                                    </button>
                                </div>
                            )}

                            {/* Retry call — shown when stuck in screening with no active call */}
                            {["no_answer", "incomplete_info", "in_progress"].includes(candidate.screening_status) && candidate.stage === "SCREENING" && (
                                <div className="mt-4 surface p-3 flex items-center justify-between gap-3">
                                    <div className="flex-1 min-w-0">
                                        <div className="text-[10px] uppercase tracking-widest text-ink-muted font-semibold mb-0.5">No call scheduled</div>
                                        <div className="text-xs text-ink-muted">Retry call now or wait for the next automatic attempt.</div>
                                    </div>
                                    <button
                                        onClick={retryCallNow}
                                        disabled={busyKey === "retry-call"}
                                        className="btn-secondary !py-1 !px-2 text-xs flex items-center gap-1 shrink-0"
                                    >
                                        {busyKey === "retry-call" ? "Calling…" : "Retry call now"}
                                    </button>
                                </div>
                            )}


                            {/* Send scheduling link — ONLY when screening actually completed.
                                incomplete_info used to share this card, telling recruiters
                                "Screening passed" over an INCOMPLETE badge — and the button
                                invited an unscreened booking through the slot picker. */}
                            {(
                                candidate.screening_status === "appointment_pending" ||
                                (["good", "strong", "borderline"].includes(candidate.verdict) && !candidate.appointment_at && candidate.screening_status !== "incomplete_info")
                            ) && candidate.stage === "SCREENING" && (
                                <div className="mt-4 surface p-3 flex items-center justify-between gap-3">
                                    <div className="flex-1 min-w-0">
                                        <div className="text-[10px] uppercase tracking-widest text-ink-muted font-semibold mb-0.5">Awaiting slot selection</div>
                                        <div className="text-xs text-ink-muted">Screening passed — candidate needs to pick an interview time.</div>
                                    </div>
                                    <button
                                        onClick={sendSlotPickerLink}
                                        disabled={busyKey === "slot-picker"}
                                        className="btn-secondary !py-1 !px-2 text-xs flex items-center gap-1 shrink-0"
                                    >
                                        {busyKey === "slot-picker" ? "Sending…" : "Send scheduling link"}
                                    </button>
                                </div>
                            )}

                            {/* Screening unfinished — honest card, and the right link: back
                                into the conversation, not a booking page. */}
                            {candidate.screening_status === "incomplete_info" && candidate.stage === "SCREENING" && !candidate.appointment_at && (
                                <div className="mt-4 surface p-3 flex items-center justify-between gap-3">
                                    <div className="flex-1 min-w-0">
                                        <div className="text-[10px] uppercase tracking-widest text-ink-muted font-semibold mb-0.5">Screening unfinished</div>
                                        <div className="text-xs text-ink-muted">They started but didn&apos;t complete the questions. Send them back into the conversation.</div>
                                    </div>
                                    <button
                                        onClick={sendScreeningLink}
                                        disabled={busyKey === "screening-link"}
                                        data-testid="send-screening-link-btn"
                                        className="btn-secondary !py-1 !px-2 text-xs flex items-center gap-1 shrink-0"
                                    >
                                        {busyKey === "screening-link" ? "Sending…" : "Send screening link"}
                                    </button>
                                </div>
                            )}

                            {/* (Action buttons removed in v28 — Approve/Deny live on the kanban card now,
                                AI calls auto-fire on stage moves, and Warmup/Pencil-In emails are
                                triggered automatically by stage transitions.) */}

                            {/* Attendance: only show in APPOINTMENT stage */}
                            {candidate.stage === "APPOINTMENT" && candidate.appointment_at && (
                                <AttendanceCard
                                    candidate={candidate}
                                    busyKey={busyKey}
                                    onSet={setAttendance}
                                    onSendForm={sendFormLink}
                                    onReschedule={() => setRescheduleOpen(true)}
                                    onArchive={archiveAttended}
                                />
                            )}
                        </div>

                        {/* Cube-style "Conversation with [Name]" auto-loading card */}
                        <ConversationSummaryCard
                            candidate={candidate}
                            conversations={conversations}
                            onSync={syncConversation}
                            syncing={busyKey === "sync"}
                        />

                        {/* Tabs */}
                        <Tabs defaultValue="resume" className="flex-1 flex flex-col">
                            <TabsList className="bg-transparent border-b border-strokes rounded-none px-6 h-10 justify-start gap-4">
                                <TabsTrigger value="resume" data-testid="tab-resume" className="data-[state=active]:bg-transparent data-[state=active]:text-ink data-[state=active]:border-b-2 data-[state=active]:border-brand-primary rounded-none px-1 text-ink-muted">
                                    <Sparkle size={12} className="mr-1.5" /> Summary
                                </TabsTrigger>
                                <TabsTrigger value="transcript" data-testid="tab-transcript" className="data-[state=active]:bg-transparent data-[state=active]:text-ink data-[state=active]:border-b-2 data-[state=active]:border-brand-primary rounded-none px-1 text-ink-muted">
                                    <Robot size={12} className="mr-1.5" /> Transcript
                                </TabsTrigger>
                                {candidate.form_responses && Object.keys(candidate.form_responses).length > 0 && (
                                    <TabsTrigger value="form" data-testid="tab-form" className="data-[state=active]:bg-transparent data-[state=active]:text-ink data-[state=active]:border-b-2 data-[state=active]:border-brand-primary rounded-none px-1 text-ink-muted">
                                        <ClipboardText size={12} className="mr-1.5" /> Form
                                    </TabsTrigger>
                                )}
                                <TabsTrigger value="comms" data-testid="tab-comms" className="data-[state=active]:bg-transparent data-[state=active]:text-ink data-[state=active]:border-b-2 data-[state=active]:border-brand-primary rounded-none px-1 text-ink-muted">
                                    <ChatText size={12} className="mr-1.5" /> Comms ({communications.length})
                                </TabsTrigger>
                                <TabsTrigger value="sms" data-testid="tab-sms" className="data-[state=active]:bg-transparent data-[state=active]:text-ink data-[state=active]:border-b-2 data-[state=active]:border-brand-primary rounded-none px-1 text-ink-muted" onClick={() => !smsThread && loadSmsThread(candidateId)}>
                                    <Phone size={12} className="mr-1.5" /> SMS
                                    {smsThread?.sms_confirmation_status === "confirmed" && <span className="ml-1.5 text-[10px] text-emerald-500 font-bold">✓</span>}
                                    {smsThread?.sms_confirmation_status === "opted_out" && <span className="ml-1.5 text-[10px] text-red-500 font-bold">⊘</span>}
                                </TabsTrigger>
                                <TabsTrigger value="email-replies" data-testid="tab-email-replies" className="data-[state=active]:bg-transparent data-[state=active]:text-ink data-[state=active]:border-b-2 data-[state=active]:border-brand-primary rounded-none px-1 text-ink-muted">
                                    <Envelope size={12} className="mr-1.5" /> Email
                                    {emailThread?.email_reply_status === "declined" && <span className="ml-1.5 text-[10px] text-red-500 font-bold">⊘</span>}
                                    {emailThread?.messages?.length > 0 && !emailThread?.email_reply_status && <span className="ml-1 text-[10px] bg-brand-primary text-white rounded-full px-1">{emailThread.messages.length}</span>}
                                </TabsTrigger>
                            </TabsList>

                            <TabsContent value="resume" className="p-6 m-0 flex-1">
                                {candidate.resume_url && (
                                    <div className="flex items-center gap-2 mb-4 pb-4 border-b border-strokes">
                                        <FileText size={14} weight="duotone" className="text-brand-primary" />
                                        <span className="text-xs text-ink-muted flex-1 truncate" title={candidate.resume_url}>
                                            {candidate.resume_url.split("/").pop() || "Resume on file"}
                                        </span>
                                        <a
                                            href={candidate.resume_url}
                                            target="_blank"
                                            rel="noreferrer"
                                            data-testid="resume-view-btn"
                                            className="btn-secondary !py-1 !px-2 text-[11px] flex items-center gap-1 whitespace-nowrap"
                                        >
                                            <FileText size={11} /> View
                                        </a>
                                        <a
                                            href={candidate.resume_url}
                                            download
                                            data-testid="resume-download-btn"
                                            className="btn-secondary !py-1 !px-2 text-[11px] flex items-center gap-1 whitespace-nowrap"
                                        >
                                            <Download size={11} weight="bold" /> Download
                                        </a>
                                    </div>
                                )}
                                {candidate.parsed_resume ? (
                                    <div className="space-y-4 text-sm">
                                        <div>
                                            <div className="label-overline mb-2">Summary</div>
                                            <p className="text-ink leading-relaxed">{candidate.parsed_resume.summary || "—"}</p>
                                        </div>
                                        {!!(candidate.parsed_resume.skills?.length) && (
                                            <div>
                                                <div className="label-overline mb-2">Skills</div>
                                                <div className="flex flex-wrap gap-1.5">
                                                    {candidate.parsed_resume.skills.map((s, i) => (
                                                        <span key={i} className="text-[11px] px-2 py-0.5 rounded-md bg-surface-hover border border-strokes">{s}</span>
                                                    ))}
                                                </div>
                                            </div>
                                        )}
                                        {!!(candidate.parsed_resume.experience?.length) && (
                                            <div>
                                                <div className="label-overline mb-2">Experience</div>
                                                <div className="space-y-2">
                                                    {candidate.parsed_resume.experience.map((e, i) => (
                                                        <div key={i} className="surface p-3">
                                                            <div className="font-medium">{e.title} <span className="text-ink-muted font-normal">at {e.company}</span></div>
                                                            <div className="text-xs text-ink-muted">{e.start} — {e.end}</div>
                                                            {e.description && <div className="text-xs text-ink-muted mt-1">{e.description}</div>}
                                                        </div>
                                                    ))}
                                                </div>
                                            </div>
                                        )}
                                        {!!(candidate.parsed_resume.education?.length) && (
                                            <div>
                                                <div className="label-overline mb-2">Education</div>
                                                <div className="space-y-1 text-xs text-ink">
                                                    {candidate.parsed_resume.education.map((e, i) => (
                                                        <div key={i}>{e.degree} · {e.institution} ({e.year})</div>
                                                    ))}
                                                </div>
                                            </div>
                                        )}
                                    </div>
                                ) : (
                                    <div className="text-sm text-ink-muted">No resume parsed yet. Upload a CV to extract structured data.</div>
                                )}
                            </TabsContent>

                            <TabsContent value="transcript" className="p-6 m-0 flex-1">
                                {/* Everything the candidate said, on every channel, in one
                                    timeline. Their words live in three separate stores and
                                    only calls were ever shown here — a recruiter could listen
                                    back to a call but had no way at all to read a chat. */}
                                <UnifiedTranscript
                                    transcript={transcript}
                                    loading={transcriptLoading}
                                    timezone={timezone}
                                />

                                <div className="flex items-center justify-between mb-3 mt-8 pt-6 border-t border-strokes">
                                    <div className="label-overline">Call recordings</div>
                                    <button
                                        onClick={syncConversation}
                                        disabled={busyKey === "sync"}
                                        data-testid="sync-conversation-btn"
                                        className="btn-secondary !py-1 !px-2 text-xs flex items-center gap-1"
                                    >
                                        <ArrowsClockwise size={11} weight="bold" /> Sync from ElevenLabs
                                    </button>
                                </div>
                                {conversations.length === 0 && (
                                    <div className="text-sm text-ink-muted">No calls yet. Screening calls are placed automatically once the candidate reaches SCREENING — "Retry call now" at the top of the drawer dials immediately.</div>
                                )}
                                {conversations.length > 0 && (
                                    <div className="text-xs text-ink-muted mb-3">
                                        Latest call shown at the top of the drawer. Older attempts listed below.
                                    </div>
                                )}
                                {conversations.slice(1).map((cv) => (
                                    <div key={cv.id} className="surface p-4 mb-3" data-testid={`conversation-${cv.id}`}>
                                        <div className="flex items-center justify-between mb-2">
                                            <div className="flex items-center gap-2 text-xs">
                                                <span className="status-pill" style={{ background: cv.status === "completed" ? "rgba(16,185,129,0.14)" : "rgba(245,158,11,0.12)", color: cv.status === "completed" ? "#34D399" : "#FBBF24" }}>
                                                    {cv.status}
                                                </span>
                                                {cv.duration_seconds && <span className="text-ink-muted"><Clock size={10} className="inline mr-1" />{cv.duration_seconds}s</span>}
                                                {cv.suitability_score != null && <span className="text-ink-muted"><ChartBar size={10} className="inline mr-1" />Score {cv.suitability_score}</span>}
                                            </div>
                                            <span className="text-[11px] text-ink-dim">{fmtET(cv.created_at, { timeZone: timezone })}</span>
                                        </div>
                                        {cv.summary && <p className="text-sm text-ink leading-relaxed mb-3">{cv.summary}</p>}
                                        {cv.elevenlabs_conversation_id && cv.status === "completed" && (
                                            <CallAudioPlayer conversationId={cv.elevenlabs_conversation_id} testid={`audio-${cv.id}`} />
                                        )}
                                        {cv.transcript?.length > 0 && (
                                            <div className="space-y-1.5 max-h-64 overflow-y-auto pr-2">
                                                {cv.transcript.map((m, i) => (
                                                    <div key={i} className="text-xs">
                                                        <span className={`font-semibold ${m.role === "agent" || m.role === "assistant" ? "text-brand-primary" : "text-brand-success"}`}>
                                                            {m.role}:{" "}
                                                        </span>
                                                        <span className="text-ink">{m.text}</span>
                                                    </div>
                                                ))}
                                            </div>
                                        )}
                                    </div>
                                ))}
                            </TabsContent>

                            <TabsContent value="form" className="p-6 m-0 flex-1">
                                <FormResponsesTab
                                    candidate={candidate}
                                    busyKey={busyKey}
                                    timezone={timezone}
                                    onHire={() => setHireDialogOpen(true)}
                                    onDecline={() => requestAction("decline", () => declineCandidate())}
                                    onReject={rejectCandidate}
                                />
                            </TabsContent>

                            <TabsContent value="comms" className="p-6 m-0 flex-1">
                                <div className="label-overline mb-3">Communication Log</div>
                                {communications.length === 0 && <div className="text-sm text-ink-muted">No communications yet.</div>}
                                {communications.map((cm) => {
                                    const expanded = !!expandedComms[cm.id];
                                    return (
                                        <div
                                            key={cm.id}
                                            className="surface p-3 mb-2 cursor-pointer select-none"
                                            data-testid={`comm-${cm.id}`}
                                            onClick={() => setExpandedComms(p => ({ ...p, [cm.id]: !p[cm.id] }))}
                                        >
                                            <div className="flex items-center justify-between mb-1">
                                                <div className="flex items-center gap-2 text-xs">
                                                    {cm.type === "email" ? <Envelope size={11} /> : <ChatText size={11} />}
                                                    <span className="font-medium">{cm.subject || cm.template_key}</span>
                                                    <span className="status-pill" style={{ background: cm.status === "sent" ? "rgba(16,185,129,0.14)" : "rgba(239,68,68,0.12)", color: cm.status === "sent" ? "#34D399" : "#F87171" }}>{cm.status}</span>
                                                </div>
                                                <div className="flex items-center gap-2">
                                                    <span className="text-[11px] text-ink-dim">{fmtET(cm.created_at, { timeZone: timezone })}</span>
                                                    <span className="text-[11px] text-ink-muted">{expanded ? "▲" : "▼"}</span>
                                                </div>
                                            </div>
                                            <div className={`text-xs text-ink-muted whitespace-pre-line ${expanded ? "" : "line-clamp-2"}`}>{cm.body}</div>
                                            {cm.error && <div className="text-xs text-brand-danger mt-1">{cm.error}</div>}
                                        </div>
                                    );
                                })}
                            </TabsContent>

                                <TabsContent value="email-replies" className="m-0 flex-1 flex flex-col" style={{ minHeight: 0 }}>
                                    {emailThread?.email_reply_status === "declined" && (
                                        <div className="mx-6 mt-4 px-3 py-2 rounded-lg border text-xs font-semibold bg-red-50 border-red-200 dark:bg-red-900/20 text-red-700 dark:text-red-400">
                                            ⊘ Candidate withdrew / not interested
                                        </div>
                                    )}
                                    {emailThread?.email_reschedule_requested && (
                                        <div className="mx-6 mt-4 px-3 py-2 rounded-lg border text-xs font-semibold bg-amber-50 border-amber-200 dark:bg-amber-900/20 text-amber-700 dark:text-amber-400">
                                            📅 Reschedule requested for {emailThread.email_reschedule_requested}
                                        </div>
                                    )}
                                    <div
                                        ref={emailScrollRef}
                                        className="flex-1 overflow-y-auto px-6 py-4 space-y-2"
                                        style={{ minHeight: 0 }}
                                    >
                                        {emailLoading && (
                                            <div className="flex justify-center py-8">
                                                <ArrowsClockwise size={18} className="animate-spin text-ink-muted" />
                                            </div>
                                        )}
                                        {!emailLoading && emailError && (
                                            <div className="text-center text-sm py-8">
                                                <span className="text-brand-danger font-semibold">{emailError}</span><br />
                                                <button onClick={() => loadEmailThread(candidateId)} className="text-xs underline text-ink-muted mt-1">Try again</button>
                                            </div>
                                        )}
                                        {!emailLoading && !emailError && (!emailThread?.messages?.length) && (
                                            <div className="text-center text-sm text-ink-muted py-8">
                                                No emails yet.<br />
                                                <span className="text-xs text-ink-dim">Emails sent to and received from this candidate appear here.</span>
                                            </div>
                                        )}
                                        {(emailThread?.messages || []).map((m, i) => {
                                            const isOut = m.direction === "outbound";
                                            return (
                                                <div key={i} className={`flex ${isOut ? "justify-end" : "justify-start"}`}>
                                                    <div className={`max-w-[80%] rounded-2xl px-3 py-2 text-sm ${isOut ? "bg-brand-primary text-white rounded-tr-sm" : "bg-surface border border-strokes text-ink rounded-tl-sm"}`}>
                                                        {m.ai_generated && (
                                                            <div className={`text-[10px] font-bold mb-0.5 ${isOut ? "text-blue-200" : "text-ink-muted"}`}>🤖 AI</div>
                                                        )}
                                                        {m.subject && <div className={`text-[10px] font-semibold mb-1 ${isOut ? "text-blue-200" : "text-ink-muted"}`}>{m.subject}</div>}
                                                        <div className="leading-snug whitespace-pre-wrap">{m.body}</div>
                                                        <div className={`text-[10px] mt-1 ${isOut ? "text-blue-200" : "text-ink-dim"}`}>
                                                            {m.sent_at ? fmtET(m.sent_at, { timeZone: timezone, month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : ""}
                                                        </div>
                                                    </div>
                                                </div>
                                            );
                                        })}
                                    </div>
                                    <div className="px-6 pb-5 pt-3 border-t border-strokes flex gap-2">
                                        <button
                                            onClick={() => loadEmailThread(candidateId)}
                                            disabled={emailLoading}
                                            className="btn-secondary !px-3 flex items-center gap-2 text-sm"
                                        >
                                            <ArrowsClockwise size={14} className={emailLoading ? "animate-spin" : ""} />
                                            Refresh
                                        </button>
                                    </div>
                                </TabsContent>

                                <TabsContent value="sms" className="m-0 p-0">
                                    {/* Status banner */}
                                    {smsThread && (() => {
                                        const s = smsThread.sms_confirmation_status;
                                        const cfg = s === "confirmed" ? { bg: "bg-emerald-900/30", text: "text-emerald-400", label: "✓ Confirmed" }
                                            : s === "rescheduled" ? { bg: "bg-amber-900/30", text: "text-amber-400", label: "⟳ Rescheduled — awaiting re-confirm" }
                                            : s === "declined" ? { bg: "bg-red-900/30", text: "text-red-400", label: "✕ Declined" }
                                            : s === "opted_out" ? { bg: "bg-red-900/30", text: "text-red-400", label: "⊘ Opted out" }
                                            : s === "sent" ? { bg: "bg-blue-900/30", text: "text-blue-400", label: "📤 Awaiting reply" }
                                            : null;
                                        return cfg ? (
                                            <div className={`mx-6 mt-4 px-3 py-2 rounded-lg border border-white/10 text-xs font-semibold ${cfg.bg} ${cfg.text}`}>
                                                {cfg.label}
                                            </div>
                                        ) : null;
                                    })()}

                                    {/* Message bubbles */}
                                    <div
                                        ref={smsScrollRef}
                                        className="overflow-y-auto px-6 py-4 space-y-2"
                                        style={{ maxHeight: "420px" }}
                                    >
                                        {smsLoading && (
                                            <div className="flex justify-center py-8">
                                                <ArrowsClockwise size={18} className="animate-spin text-ink-muted" />
                                            </div>
                                        )}
                                        {!smsLoading && (!smsThread?.messages?.length) && (
                                            <div className="text-center text-sm text-ink-muted py-8">No messages yet.</div>
                                        )}
                                        {(smsThread?.messages || []).map((m, i) => {
                                            const isOut = m.direction === "out";
                                            return (
                                                <div key={i} className={`flex ${isOut ? "justify-end" : "justify-start"}`}>
                                                    <div className={`max-w-[78%] rounded-2xl px-3 py-2 text-sm ${isOut ? "bg-brand-primary text-white rounded-tr-sm" : "bg-surface border border-strokes text-ink rounded-tl-sm"}`}>
                                                        {m.was_llm_reply && (
                                                            <div className={`text-[10px] font-bold mb-0.5 ${isOut ? "text-blue-200" : "text-ink-muted"}`}>🤖 AI</div>
                                                        )}
                                                        <div className="leading-snug">{m.body}</div>
                                                        <div className={`text-[10px] mt-1 ${isOut ? "text-blue-200" : "text-ink-dim"}`}>
                                                            {m.timestamp ? fmtET(m.timestamp, { timeZone: timezone, month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : ""}
                                                        </div>
                                                    </div>
                                                </div>
                                            );
                                        })}
                                    </div>

                                    {/* Footer */}
                                    <div className="px-6 pb-5 pt-3 border-t border-strokes space-y-2">
                                        {/* Free-text reply */}
                                        <div className="flex gap-2 items-end">
                                            <textarea
                                                value={smsDraft}
                                                onChange={(e) => setSmsDraft(e.target.value)}
                                                onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); sendSmsReply(); } }}
                                                placeholder={smsThread?.sms_opted_out ? "This candidate opted out (STOP)" : "Reply…  (⌘/Ctrl + Enter to send)"}
                                                rows={2}
                                                disabled={smsSending || smsThread?.sms_opted_out}
                                                data-testid="sms-reply-input"
                                                className="flex-1 resize-none rounded-lg bg-surface border border-strokes px-3 py-2 text-sm text-ink placeholder:text-ink-dim focus:outline-none focus:border-brand-primary disabled:opacity-50"
                                            />
                                            <button
                                                onClick={sendSmsReply}
                                                disabled={smsSending || !smsDraft.trim() || smsThread?.sms_opted_out}
                                                data-testid="sms-reply-send"
                                                className="btn-primary h-10 !px-4 flex items-center gap-2 text-sm disabled:opacity-50"
                                            >
                                                {smsSending ? <ArrowsClockwise size={14} className="animate-spin" /> : <PaperPlaneRight size={14} weight="fill" />}
                                                Send
                                            </button>
                                        </div>
                                        <div className="flex gap-2">
                                            <button
                                                onClick={resendSms}
                                                disabled={smsSending}
                                                className="btn-secondary flex-1 flex items-center justify-center gap-2 text-sm disabled:opacity-50"
                                            >
                                                <Phone size={14} />
                                                Resend Confirmation SMS
                                            </button>
                                            <button
                                                onClick={() => loadSmsThread(candidateId)}
                                                disabled={smsLoading}
                                                className="btn-secondary !px-3"
                                                title="Refresh thread"
                                            >
                                                <ArrowsClockwise size={14} className={smsLoading ? "animate-spin" : ""} />
                                            </button>
                                        </div>
                                    </div>
                                </TabsContent>
                        </Tabs>
                    </div>
                )}
            </SheetContent>
            <StageMoveConfirmDialog
                open={!!pending}
                onOpenChange={(o) => { if (!o) setPending(null); }}
                action={pending?.action}
                candidate={pending?.candidate}
                onConfirm={handleConfirm}
            />
            <SlotPickerDialog
                open={rescheduleOpen}
                onOpenChange={setRescheduleOpen}
                candidate={candidate}
                pipelineId={candidate?.pipeline_id}
                onPick={reschedule}
                mode="reschedule"
            />
            <Dialog open={confirmDelete} onOpenChange={setConfirmDelete}>
                <DialogContent className="bg-[#0E0E11] border-strokes max-w-sm" data-testid="delete-candidate-dialog">
                    <DialogHeader>
                        <DialogTitle className="font-heading text-lg flex items-center gap-2">
                            <Trash size={16} weight="duotone" className="text-brand-danger" />
                            Delete candidate?
                        </DialogTitle>
                        <DialogDescription className="text-xs text-ink-muted leading-relaxed pt-2">
                            This permanently removes <strong className="text-ink">{candidate?.first_name} {candidate?.last_name}</strong>{" "}
                            and all their data — call recordings, transcripts, emails, SMS history, form responses, and any pending reminders or queued retry calls. This cannot be undone.
                        </DialogDescription>
                    </DialogHeader>
                    <DialogFooter className="gap-2">
                        <button
                            type="button"
                            onClick={() => setConfirmDelete(false)}
                            data-testid="cancel-delete-btn"
                            className="btn-secondary !py-1.5 !px-3 text-xs"
                        >
                            Cancel
                        </button>
                        <button
                            type="button"
                            onClick={deleteCandidate}
                            disabled={busyKey === "delete"}
                            data-testid="confirm-delete-btn"
                            className="text-xs font-semibold px-3 py-1.5 rounded bg-brand-danger text-white hover:bg-brand-danger/90 transition-colors flex items-center gap-1.5 disabled:opacity-60"
                        >
                            <Trash size={11} weight="bold" />
                            {busyKey === "delete" ? "Deleting…" : "Delete forever"}
                        </button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            {/* Hire confirmation dialog — collects training start date/time for CG1 */}
            <Dialog open={hireDialogOpen} onOpenChange={setHireDialogOpen}>
                <DialogContent className="bg-[#0E0E11] border-strokes max-w-sm">
                    <DialogHeader>
                        <DialogTitle className="font-heading text-lg">Confirm Hire</DialogTitle>
                        <DialogDescription className="text-ink-muted text-sm">
                            Set the training start date. CG1 will send the confirmation email automatically.
                        </DialogDescription>
                    </DialogHeader>
                    <div className="space-y-3 py-1">
                        <div>
                            <label className="label-overline block mb-1">Start Date</label>
                            <input
                                type="date"
                                value={trainingDate}
                                onChange={(e) => setTrainingDate(e.target.value)}
                                className="input-dark w-full"
                            />
                        </div>
                        <div>
                            <label className="label-overline block mb-1">Start Time</label>
                            <input
                                type="time"
                                value={trainingTime}
                                onChange={(e) => setTrainingTime(e.target.value)}
                                className={`input-dark w-full ${isBeforeBusinessHours(trainingTime) ? "!border-brand-danger/70" : ""}`}
                            />
                            {/* AM/PM read-back — the native input renders 24h on some systems
                                and "2:00" typed there means 2:00 AM. */}
                            {trainingTime && (
                                <div
                                    className={`mt-1 text-[11px] ${isBeforeBusinessHours(trainingTime) ? "text-brand-danger font-semibold" : "text-ink-muted"}`}
                                    data-testid="training-time-ampm"
                                >
                                    {isBeforeBusinessHours(trainingTime)
                                        ? `${formatAmPm(trainingTime)} is earlier than 8:30 AM${pmSuggestion(trainingTime) ? ` — did you mean ${pmSuggestion(trainingTime)}?` : "."}`
                                        : `= ${formatAmPm(trainingTime)}`}
                                </div>
                            )}
                        </div>
                    </div>
                    <DialogFooter className="gap-2 mt-2">
                        <button onClick={() => setHireDialogOpen(false)} className="btn-secondary flex-1">Cancel</button>
                        <button
                            onClick={hireCandidate}
                            disabled={busyKey === "hire"}
                            className="btn-primary flex-1"
                        >
                            {busyKey === "hire" ? "Hiring…" : "Confirm Hire"}
                        </button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
        </Sheet>
    );
}

function FormResponsesTab({ candidate, busyKey, timezone, onHire, onDecline, onReject }) {
    const { canMutate } = useAuth();
    const responses = candidate.form_responses || {};
    const ids = Object.keys(responses);
    const submittedAt = candidate.form_submitted_at;
    const alreadyHired = candidate.stage === 'TRAINING' || candidate.hired;
    const alreadyDeclined = candidate.stage === 'CLOSE' && candidate.form_decline_at;
    // Reject (send rejection email + archive) is only offered at the TO CLOSE
    // decision point, alongside Hire.
    const canReject = candidate.stage === 'CLOSE';

    return (
        <div className="space-y-5" data-testid="form-responses-tab">
            <div className="flex items-center justify-between gap-2">
                <div>
                    <div className="label-overline mb-1">Candidate Form Responses</div>
                    {submittedAt && (
                        <div className="text-[11px] text-ink-muted">
                            Submitted {fmtET(submittedAt, { timeZone: timezone })}
                        </div>
                    )}
                </div>
                {canMutate && <div className="flex gap-2">
                    {canReject && (
                        <button
                            onClick={onReject}
                            disabled={busyKey === 'reject'}
                            data-testid="form-reject-btn"
                            title="Send rejection email and archive"
                            className="text-xs px-3 py-1.5 rounded border border-brand-danger/40 text-brand-danger hover:bg-brand-danger/10 disabled:opacity-40 flex items-center gap-1.5"
                        >
                            <XCircle size={11} weight="bold" />
                            {busyKey === 'reject' ? '…' : 'Reject'}
                        </button>
                    )}
                    <button
                        onClick={onDecline}
                        disabled={busyKey === 'decline' || alreadyDeclined}
                        data-testid="form-decline-btn"
                        className="text-xs px-3 py-1.5 rounded border border-brand-danger/40 text-brand-danger hover:bg-brand-danger/10 disabled:opacity-40 flex items-center gap-1.5"
                    >
                        <ThumbsDown size={11} weight="bold" />
                        {alreadyDeclined ? 'Declined' : (busyKey === 'decline' ? '…' : 'Decline')}
                    </button>
                    <button
                        onClick={onHire}
                        disabled={busyKey === 'hire' || alreadyHired}
                        data-testid="form-hire-btn"
                        className="btn-primary !py-1.5 text-xs flex items-center gap-1.5 disabled:opacity-40"
                    >
                        <ThumbsUp size={11} weight="bold" />
                        {alreadyHired ? 'Hired' : (busyKey === 'hire' ? '…' : 'Hire')}
                    </button>
                </div>}
            </div>

            {ids.length === 0 ? (
                <div className="surface p-6 text-center">
                    <ClipboardText size={20} weight="duotone" className="text-ink-muted mx-auto mb-2" />
                    <div className="text-sm text-ink-muted">Form not submitted yet.</div>
                </div>
            ) : (
                <div className="space-y-3" data-testid="form-responses-list">
                    {ids.map((qid, i) => {
                        const r = responses[qid];
                        const question = (r && typeof r === 'object' && r.question) ? r.question : `Question ${i + 1}`;
                        const answer = (r && typeof r === 'object' && r.answer) ? r.answer : (typeof r === 'string' ? r : '');
                        return (
                            <div key={qid} className="surface p-3" data-testid={`form-response-${qid}`}>
                                <div className="text-[11px] uppercase tracking-wider text-ink-muted mb-1">
                                    {question}
                                </div>
                                <div className="text-sm text-ink whitespace-pre-line">
                                    {answer || <span className="text-ink-dim italic">(blank)</span>}
                                </div>
                            </div>
                        );
                    })}
                </div>
            )}
        </div>
    );
}

function ContactEditor({ candidate, canMutate, onUpdated }) {
    const [editing, setEditing] = useState(false);
    const [fields, setFields] = useState({});
    const [busy, setBusy] = useState(false);

    const open = () => {
        setFields({
            first_name: candidate.first_name || "",
            last_name: candidate.last_name || "",
            email: candidate.email || "",
            phone: candidate.phone || "",
        });
        setEditing(true);
    };

    const save = async () => {
        setBusy(true);
        try {
            const r = await api.patch(`/candidates/${candidate.id}`, fields);
            onUpdated(r.data);
            setEditing(false);
        } catch {
            toast.error("Failed to update contact details");
        } finally { setBusy(false); }
    };

    if (!canMutate) return null;

    if (editing) {
        return (
            <div className="mt-2 p-2 bg-surface border border-strokes rounded-lg text-xs space-y-1.5">
                <div className="flex gap-1.5">
                    <input
                        autoFocus
                        value={fields.first_name}
                        onChange={(e) => setFields(f => ({ ...f, first_name: e.target.value }))}
                        placeholder="First name"
                        className="w-1/2 input-dark !py-1 !text-xs"
                    />
                    <input
                        value={fields.last_name}
                        onChange={(e) => setFields(f => ({ ...f, last_name: e.target.value }))}
                        placeholder="Last name"
                        className="w-1/2 input-dark !py-1 !text-xs"
                    />
                </div>
                <input
                    value={fields.email}
                    onChange={(e) => setFields(f => ({ ...f, email: e.target.value }))}
                    placeholder="Email address"
                    type="email"
                    className="w-full input-dark !py-1 !text-xs"
                />
                <input
                    value={fields.phone}
                    onChange={(e) => setFields(f => ({ ...f, phone: e.target.value }))}
                    placeholder="Phone number"
                    type="tel"
                    className="w-full input-dark !py-1 !text-xs"
                />
                <div className="flex gap-2 pt-0.5">
                    <button onClick={save} disabled={busy} className="text-[10px] text-brand-primary hover:underline">Save</button>
                    <button onClick={() => setEditing(false)} className="text-[10px] text-ink-muted hover:text-ink">Cancel</button>
                </div>
            </div>
        );
    }

    return (
        <button
            onClick={open}
            className="mt-1 text-[10px] text-ink-muted hover:text-brand-primary"
            title="Edit name, email, or phone"
        >
            Edit contact details
        </button>
    );
}

function ReferredByEditor({ candidate, canMutate, onUpdated }) {
    const [editing, setEditing] = useState(false);
    const [value, setValue] = useState(candidate.referred_by || "");
    const [busy, setBusy] = useState(false);

    const save = async () => {
        setBusy(true);
        try {
            const r = await api.patch(`/candidates/${candidate.id}`, { referred_by: value.trim() || null });
            onUpdated(r.data);
            setEditing(false);
        } catch {
            toast.error("Failed to update referred by");
        } finally { setBusy(false); }
    };

    if (editing) {
        return (
            <div className="flex items-center gap-1 mt-1">
                <input
                    autoFocus
                    value={value}
                    onChange={(e) => setValue(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter") save(); if (e.key === "Escape") setEditing(false); }}
                    placeholder="Referred by…"
                    className="text-xs bg-surface border border-strokes rounded px-2 py-0.5 outline-none focus:border-brand-primary text-ink w-40"
                />
                <button onClick={save} disabled={busy} className="text-[10px] text-brand-primary hover:underline">Save</button>
                <button onClick={() => setEditing(false)} className="text-[10px] text-ink-muted hover:text-ink">Cancel</button>
            </div>
        );
    }

    return (
        <div className="flex items-center gap-1 mt-1">
            <span className="text-[11px] text-ink-muted">
                {candidate.referred_by ? <>Referred by <span className="text-ink font-medium">{candidate.referred_by}</span></> : <span className="italic">No referral</span>}
            </span>
            {canMutate && (
                <button
                    onClick={() => { setValue(candidate.referred_by || ""); setEditing(true); }}
                    className="text-[10px] text-ink-muted hover:text-brand-primary ml-1"
                    title="Edit referred by"
                >
                    Edit
                </button>
            )}
        </div>
    );
}
