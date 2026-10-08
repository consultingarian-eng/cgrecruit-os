import { memo, useEffect, useMemo, useState } from "react";
import { Phone, CalendarBlank, ClipboardText, CheckCircle, XCircle, GraduationCap, Star, ArrowsClockwise, X, EnvelopeSimple, Plus } from "@phosphor-icons/react";
import api from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { usePipeline } from "@/lib/pipeline";
import DirectBookModal from "./DirectBookModal";
import QuickAddTrainingModal from "./QuickAddTrainingModal";
import StageMoveConfirmDialog, {
    STAGE_ACTIONS,
    ACTION_VARIANTS,
    shouldSkipConfirm,
} from "./StageMoveConfirmDialog";
import { etNaiveToUtcMs, fmtET } from "@/lib/formatET";
import { confirmDialog } from "@/components/ConfirmDialog";
import { toast } from "sonner";

const STAGES = [
    { key: "SCREENING", label: "Screening", icon: <Phone size={13} weight="bold" /> },
    { key: "APPOINTMENT", label: "Appointment", icon: <CalendarBlank size={13} weight="bold" /> },
    { key: "FORM", label: "Form", icon: <ClipboardText size={13} weight="bold" /> },
    { key: "CLOSE", label: "To Close", icon: <CheckCircle size={13} weight="bold" /> },
    { key: "TRAINING", label: "Training", icon: <GraduationCap size={13} weight="bold" /> },
];

const STATUS_COLORS = {
    pending: { bg: "rgba(156,163,175,0.12)", color: "#9CA3AF", label: "Pending" },
    // Deliberately pulled out of the dialer. Without its own entry it falls back
    // to "Pending", which reads as "nothing has happened yet" — the opposite.
    paused: { bg: "rgba(148,163,184,0.18)", color: "#94A3B8", label: "Paused — AI stopped" },
    queued: { bg: "rgba(59,130,246,0.12)", color: "#60A5FA", label: "Queued" },
    in_progress: { bg: "rgba(245,158,11,0.12)", color: "#FBBF24", label: "Calling…" },
    approved: { bg: "rgba(16,185,129,0.14)", color: "#34D399", label: "Approved" },
    appointment_pending: { bg: "rgba(59,130,246,0.14)", color: "#60A5FA", label: "Passed — needs a slot" },
    strong: { bg: "rgba(16,185,129,0.18)", color: "#10B981", label: "Strong" },
    no_answer: { bg: "rgba(156,163,175,0.12)", color: "#9CA3AF", label: "No Answer" },
    didnt_connect: { bg: "rgba(239,68,68,0.12)", color: "#F87171", label: "Didn't Connect" },
    incomplete_info: { bg: "rgba(245,158,11,0.18)", color: "#FBBF24", label: "Incomplete" },
    rejected: { bg: "rgba(239,68,68,0.18)", color: "#F87171", label: "Auto-rejected" },
    review: { bg: "rgba(139,92,246,0.14)", color: "#A78BFA", label: "Review" },
};

function nextMondayISO() {
    const d = new Date();
    const day = d.getDay(); // 0=Sun,1=Mon,...,6=Sat
    const daysUntilMonday = day === 1 ? 7 : (8 - day) % 7 || 7;
    d.setDate(d.getDate() + daysUntilMonday);
    return d.toISOString().slice(0, 10);
}

export default function KanbanBoard({ candidates, onCardClick, onMove, selectionMode, selectedIds, onToggleSelect, pipelineId, jobs = [], onRefresh }) {
    const { canMutate } = useAuth();
    const [dragOverStage, setDragOverStage] = useState(null);
    // pending = { action, candidate, basePayload }
    const [pending, setPending] = useState(null);
    const [directBookOpen, setDirectBookOpen] = useState(false);
    const [quickTrainingOpen, setQuickTrainingOpen] = useState(false);
    // Training start modal (initial booking)
    const [trainingModal, setTrainingModal] = useState(null); // { candidate, basePayload }
    const [trainingDate, setTrainingDate] = useState(nextMondayISO);
    const [trainingTime, setTrainingTime] = useState("13:00");
    // Per-person training time overrides (pre-filled from pipeline template)
    const [tMonStart, setTMonStart] = useState("");
    const [tMonEnd, setTMonEnd] = useState("");
    const [tTueStart, setTTueStart] = useState("");
    const [tTueEnd, setTTueEnd] = useState("");
    const [tplLoading, setTplLoading] = useState(false);
    // The starter template as fetched, kept so the four time boxes can be
    // re-derived when the start date changes (dated exceptions are keyed on it).
    const [tplBase, setTplBase] = useState(null);
    const [templateConfirmed, setTemplateConfirmed] = useState(false);
    // Reschedule modal (for candidates already in TRAINING)
    const [rescheduleModal, setRescheduleModal] = useState(null); // { candidate }
    const [rescheduleDate, setRescheduleDate] = useState(nextMondayISO);
    const [rescheduleTime, setRescheduleTime] = useState("13:00");
    const [rescheduleBusy, setRescheduleBusy] = useState(false);

    const grouped = useMemo(() => {
        const map = Object.fromEntries(STAGES.map((s) => [s.key, []]));
        for (const c of candidates) {
            map[c.stage] = map[c.stage] || [];
            map[c.stage].push(c);
        }
        return map;
    }, [candidates]);

    // Reject a TO CLOSE candidate: always confirm, then send the rejection email
    // and soft-archive them (they drop off the board on refresh).
    const handleReject = async (candidate) => {
        const name = candidate.first_name || "this candidate";
        const ok = await confirmDialog({
            title: "Reject candidate?",
            description: `Sends ${name} a rejection email and archives them — they won't move forward.`,
            confirmLabel: "Reject & email",
            destructive: true,
        });
        if (!ok) return;
        try {
            await api.post(`/candidates/${candidate.id}/reject`);
            toast.success("Rejected — email sent");
            onRefresh?.();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to reject");
        }
    };

    // Pausing clears next_call_at and the dialer stands down, so a plain
    // status move would mint a Queued row nothing ever dials. This is the same
    // call the Call Queue console makes — and the only one on the board.
    const handleResumeQueue = async (candidate) => {
        try {
            await api.post(`/candidates/${candidate.id}/queue/resume`, { delay_minutes: 0 });
            toast.success("Back in the dial queue");
            onRefresh?.();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Couldn't resume calling");
        }
    };

    // Both training modals are hand-rolled overlays rather than Radix dialogs,
    // so Escape has to be wired by hand or they have no keyboard exit at all.
    useEffect(() => {
        if (!trainingModal && !rescheduleModal) return;
        const onKey = (e) => {
            if (e.key !== "Escape") return;
            if (rescheduleModal) {
                if (!rescheduleBusy) setRescheduleModal(null);
            } else {
                setTrainingModal(null);
            }
        };
        window.addEventListener("keydown", onKey);
        return () => window.removeEventListener("keydown", onKey);
    }, [trainingModal, rescheduleModal, rescheduleBusy]);

    // Fill the four training-time boxes from the template, with any exception
    // recorded for the date being picked layered on top. Re-runs on date change
    // because the exceptions are keyed on the start date — opening the modal
    // fetches once, then moving the date off (or onto) an exception day swaps
    // the hours to match what the starter email will actually say.
    useEffect(() => {
        if (!tplBase) return;
        const t = { ...tplBase, ...((tplBase.date_overrides || {})[trainingDate] || {}) };
        setTMonStart(t.monday_start || "");
        setTMonEnd(t.monday_end || "");
        setTTueStart(t.tuesday_start || "");
        setTTueEnd(t.tuesday_end || "");
    }, [tplBase, trainingDate]);

    // Approve / Deny pills always confirm (with session skip).
    // Drag-and-drop only confirms when destination triggers an automated email,
    // OR when the destination is APPOINTMENT without a captured slot — in which
    // case we MUST show the slot picker so the approval email isn't sent blank.
    const requestMove = (candidate, basePayload, { variantKey, destinationLabel, alwaysConfirm } = {}) => {
        const variant = variantKey ? ACTION_VARIANTS[variantKey] : null;
        const targetStage = basePayload.stage || candidate.stage;
        const stageAction = STAGE_ACTIONS[targetStage] || {};
        const action = variant
            ? { ...variant, destinationStageLabel: STAGE_ACTIONS[variant.destinationStage]?.label || variant.destinationStage }
            : { ...stageAction, destinationStage: targetStage, destinationStageLabel: destinationLabel || stageAction.label };

        // Moving to TRAINING always shows the start-date modal first.
        if (targetStage === "TRAINING" && (candidate.stage !== "TRAINING")) {
            setTrainingDate(nextMondayISO());
            setTrainingTime("13:00");
            setTMonStart(""); setTMonEnd(""); setTTueStart(""); setTTueEnd("");
            setTplBase(null);
            setTemplateConfirmed(false);
            setTrainingModal({ candidate, basePayload });
            // Pre-fill training times from the pipeline's stored starter template.
            if (pipelineId) {
                setTplLoading(true);
                api.get("/settings/starter-template", { params: { pipeline_id: pipelineId } })
                    .then((r) => setTplBase(r.data || {}))
                    .catch(() => {})
                    .finally(() => setTplLoading(false));
            }
            return;
        }

        const hasConsequence = !!action.templateKey;
        const needsSlot = action.destinationStage === "APPOINTMENT";
        const hasSlot = !!candidate.appointment_at;
        const forceModal = needsSlot && !hasSlot;
        const skip = shouldSkipConfirm(action);

        // Drag-and-drop with no automated email AND no slot requirement → silently move.
        if (!alwaysConfirm && !hasConsequence && !forceModal) {
            onMove(candidate.id, basePayload);
            return;
        }
        // Skip-this-session bypasses the modal except when we need to pick a slot.
        if (skip && !forceModal) {
            const merged = { ...basePayload };
            if (hasConsequence) merged.send_email_template = action.templateKey;
            if (needsSlot && hasSlot) merged.appointment_at = candidate.appointment_at;
            onMove(candidate.id, merged);
            return;
        }
        setPending({ action, candidate, basePayload });
    };

    const handleConfirm = ({ sendEmail, appointmentAt }) => {
        if (!pending) return;
        const { candidate, basePayload, action } = pending;
        const merged = { ...basePayload };
        if (sendEmail && action.templateKey) merged.send_email_template = action.templateKey;
        // When the destination is APPOINTMENT, the dialog's slot picker hands us
        // the user-selected ISO. Persist it so the email/booking page render
        // with a real date instead of a blank placeholder.
        if (appointmentAt) merged.appointment_at = appointmentAt;
        onMove(candidate.id, merged);
        setPending(null);
    };

    const handleDragStart = (e, candidateId, fromStage) => {
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", JSON.stringify({ candidateId, fromStage }));
    };

    const handleDragOver = (e, stageKey) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        if (dragOverStage !== stageKey) setDragOverStage(stageKey);
    };

    const handleDrop = (e, stageKey) => {
        e.preventDefault();
        setDragOverStage(null);
        try {
            const { candidateId, fromStage } = JSON.parse(e.dataTransfer.getData("text/plain") || "{}");
            if (!candidateId || fromStage === stageKey) return;
            const candidate = candidates.find((c) => c.id === candidateId);
            if (!candidate) return;
            const stageInfo = STAGES.find((s) => s.key === stageKey);
            requestMove(candidate, { stage: stageKey }, { destinationLabel: stageInfo?.label });
        } catch { /* malformed payload */ }
    };

    return (
        <>
            {/* Mobile and tablet: flex row with snap-scroll so each column fills
                the screen and you swipe left/right between stages. The grid only
                starts at lg, where all five columns fit on one row — a
                three-column grid wraps To Close and Training into a second row
                this container clips (overflow-y-hidden), making them unreachable.
                Vertical scrolling lives INSIDE each column; the board itself
                scrolls horizontally only. */}
            <div className="flex lg:grid lg:grid-cols-5 h-full overflow-x-auto overflow-y-hidden snap-x snap-mandatory lg:snap-none" data-testid="kanban-board">
                {STAGES.map((s) => (
                    <div
                        key={s.key}
                        className={`flex-none w-[88vw] md:w-[340px] lg:w-auto snap-start border-r border-strokes last:border-r-0 flex flex-col min-h-full transition-colors ${
                            dragOverStage === s.key ? "bg-[rgba(139,92,246,0.06)]" : ""
                        }`}
                        data-testid={`kanban-column-${s.key}`}
                        onDragOver={(e) => handleDragOver(e, s.key)}
                        onDragLeave={() => setDragOverStage((cur) => (cur === s.key ? null : cur))}
                        onDrop={(e) => handleDrop(e, s.key)}
                    >
                        <div className="sticky top-0 px-4 h-11 bg-[#0E0E11] border-b border-strokes flex items-center justify-between z-10">
                            <div className="flex items-center gap-2 text-ink-muted">
                                <span className="text-brand-primary">{s.icon}</span>
                                <span className="text-[11px] font-semibold uppercase tracking-[0.18em]">{s.label}</span>
                            </div>
                            <div className="flex items-center gap-2">
                                {s.key === "APPOINTMENT" && canMutate && (
                                    <button
                                        onClick={() => setDirectBookOpen(true)}
                                        title="Book someone directly into an interview slot (skip screening)"
                                        className="flex items-center justify-center w-5 h-5 rounded border border-strokes text-ink-muted hover:border-brand-primary hover:text-brand-primary transition-colors"
                                    >
                                        <Plus size={10} weight="bold" />
                                    </button>
                                )}
                                {s.key === "TRAINING" && canMutate && (
                                    <button
                                        onClick={() => setQuickTrainingOpen(true)}
                                        title="Add someone directly to Training (with or without sending comms)"
                                        className="flex items-center justify-center w-5 h-5 rounded border border-strokes text-ink-muted hover:border-brand-primary hover:text-brand-primary transition-colors"
                                    >
                                        <Plus size={10} weight="bold" />
                                    </button>
                                )}
                                <span className="text-xs text-ink-muted tabular-nums">{grouped[s.key]?.length || 0}</span>
                            </div>
                        </div>
                        <div className="flex-1 overflow-y-auto overscroll-contain p-3 space-y-2" style={{ scrollbarGutter: "stable" }}>
                            {(grouped[s.key] || []).map((c) => (
                                <KanbanCard
                                    key={c.id}
                                    candidate={c}
                                    onClick={() => {
                                        if (selectionMode) onToggleSelect?.(c.id);
                                        else onCardClick(c.id);
                                    }}
                                    onRequestMove={requestMove}
                                    onResumeQueue={handleResumeQueue}
                                    stageKey={s.key}
                                    onDragStart={handleDragStart}
                                    selected={selectionMode && selectedIds?.has(c.id)}
                                    selectionMode={selectionMode}
                                    onRescheduleTraining={(candidate) => {
                                        setRescheduleDate(nextMondayISO());
                                        setRescheduleTime("13:00");
                                        setRescheduleModal({ candidate });
                                    }}
                                    onReject={canMutate ? handleReject : undefined}
                                />
                            ))}
                            {(grouped[s.key] || []).length === 0 && (
                                <div className={`text-xs text-ink-dim text-center py-8 border border-dashed rounded-md transition-colors ${
                                    dragOverStage === s.key ? "border-brand-primary text-brand-primary" : "border-strokes"
                                }`}>
                                    {dragOverStage === s.key ? "Drop to move here" : "No candidates"}
                                </div>
                            )}
                        </div>
                    </div>
                ))}
            </div>

            <StageMoveConfirmDialog
                open={!!pending}
                onOpenChange={(o) => { if (!o) setPending(null); }}
                action={pending?.action}
                candidate={pending?.candidate}
                onConfirm={handleConfirm}
            />

            {/* Training start date/time modal */}
            {trainingModal && (
                <div
                    role="dialog"
                    aria-modal="true"
                    onClick={() => setTrainingModal(null)}
                    className="fixed inset-0 z-50 flex items-start sm:items-center justify-center bg-black/60 px-4 py-8 overflow-y-auto"
                >
                    <div onClick={(e) => e.stopPropagation()} className="surface p-6 rounded-xl max-w-md w-full space-y-5 shadow-xl">
                        <div>
                            <div className="label-overline mb-0.5">Move to Training</div>
                            <h3 className="font-heading text-lg font-semibold">
                                Set start date & time for {trainingModal.candidate.first_name}
                            </h3>
                            <p className="text-xs text-ink-muted mt-1">
                                We'll send the starter email and schedule the 3-hour reminder text automatically.
                            </p>
                        </div>
                        <div className="space-y-3">
                            <div>
                                <label className="text-xs font-medium text-ink-muted uppercase tracking-widest mb-1 block">Start Date</label>
                                {/* Native date input value is always YYYY-MM-DD; display format is controlled by browser locale.
                                    We render three numeric selects to guarantee MM/DD/YYYY regardless of locale. */}
                                {(() => {
                                    const [y, m, d] = trainingDate.split("-");
                                    const selClass = "bg-surface-active border border-strokes rounded px-2 py-2 text-sm text-ink focus:outline-none focus:border-brand-primary";
                                    const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
                                    const daysInMonth = new Date(Number(y), Number(m), 0).getDate();
                                    return (
                                        <div className="flex gap-2">
                                            <select value={m} onChange={e => setTrainingDate(`${y}-${e.target.value}-${d}`)} className={selClass}>
                                                {months.map((mn, i) => <option key={i} value={String(i+1).padStart(2,"0")}>{mn}</option>)}
                                            </select>
                                            <select value={d} onChange={e => setTrainingDate(`${y}-${m}-${e.target.value}`)} className={selClass}>
                                                {Array.from({length: daysInMonth}, (_, i) => String(i+1).padStart(2,"0")).map(dd => <option key={dd} value={dd}>{dd}</option>)}
                                            </select>
                                            <select value={y} onChange={e => setTrainingDate(`${e.target.value}-${m}-${d}`)} className={selClass}>
                                                {[2025,2026,2027].map(yr => <option key={yr} value={yr}>{yr}</option>)}
                                            </select>
                                        </div>
                                    );
                                })()}
                            </div>
                            <div>
                                <label className="text-xs font-medium text-ink-muted uppercase tracking-widest mb-1 block">Start Time</label>
                                {/* Custom 12h AM/PM picker — trainingTime stays as HH:MM internally */}
                                {(() => {
                                    const [hh, mm] = trainingTime.split(":").map(Number);
                                    const period = hh >= 12 ? "PM" : "AM";
                                    const hour12 = hh % 12 || 12;
                                    const selClass = "bg-surface-active border border-strokes rounded px-2 py-2 text-sm text-ink focus:outline-none focus:border-brand-primary";
                                    const setTime = (h12, min, per) => {
                                        let h24 = h12 % 12;
                                        if (per === "PM") h24 += 12;
                                        setTrainingTime(`${String(h24).padStart(2,"0")}:${String(min).padStart(2,"0")}`);
                                    };
                                    return (
                                        <div className="flex gap-2 items-center">
                                            <select value={hour12} onChange={e => setTime(Number(e.target.value), mm, period)} className={selClass}>
                                                {Array.from({length:12},(_,i)=>i+1).map(h => <option key={h} value={h}>{h}</option>)}
                                            </select>
                                            <span className="text-ink-muted text-sm">:</span>
                                            <select value={mm} onChange={e => setTime(hour12, Number(e.target.value), period)} className={selClass}>
                                                {Array.from({length:12},(_,i)=>i*5).map(min => <option key={min} value={min}>{String(min).padStart(2,"0")}</option>)}
                                            </select>
                                            <select value={period} onChange={e => setTime(hour12, mm, e.target.value)} className={selClass}>
                                                <option value="AM">AM</option>
                                                <option value="PM">PM</option>
                                            </select>
                                        </div>
                                    );
                                })()}
                            </div>
                        </div>
                        {/* Training time overrides */}
                        <div>
                            <label className="text-xs font-medium text-ink-muted uppercase tracking-widest mb-1 block">
                                Training Schedule
                                {tplLoading && <span className="ml-2 text-[10px] opacity-60">loading…</span>}
                            </label>
                            <p className="text-[11px] text-ink-muted mb-2">Pre-filled from your pipeline template — edit here to override for this person only.</p>
                            {(() => {
                                const inp = "bg-surface-active border border-strokes rounded px-2 py-1.5 text-sm text-ink focus:outline-none focus:border-brand-primary w-full";
                                return (
                                    <div className="grid grid-cols-2 gap-2">
                                        <div>
                                            <div className="text-[10px] text-ink-muted mb-0.5">Day 1 Start</div>
                                            <input className={inp} value={tMonStart} onChange={e => setTMonStart(e.target.value)} placeholder="e.g. 1:00 PM" />
                                        </div>
                                        <div>
                                            <div className="text-[10px] text-ink-muted mb-0.5">Day 1 End</div>
                                            <input className={inp} value={tMonEnd} onChange={e => setTMonEnd(e.target.value)} placeholder="e.g. 3:00 PM" />
                                        </div>
                                        <div>
                                            <div className="text-[10px] text-ink-muted mb-0.5">Day 2 Start</div>
                                            <input className={inp} value={tTueStart} onChange={e => setTTueStart(e.target.value)} placeholder="e.g. 11:00 AM" />
                                        </div>
                                        <div>
                                            <div className="text-[10px] text-ink-muted mb-0.5">Day 2 End</div>
                                            <input className={inp} value={tTueEnd} onChange={e => setTTueEnd(e.target.value)} placeholder="e.g. 3:00 PM" />
                                        </div>
                                    </div>
                                );
                            })()}
                        </div>
                        {/* Template confirmation checkbox */}
                        <label className="flex items-start gap-2.5 cursor-pointer select-none mt-1">
                            <input
                                type="checkbox"
                                checked={templateConfirmed}
                                onChange={e => setTemplateConfirmed(e.target.checked)}
                                className="mt-0.5 accent-brand-primary"
                            />
                            <span className="text-xs text-ink-muted leading-snug">
                                I confirm the starter email template is correct and will be sent to {trainingModal.candidate.first_name}.
                            </span>
                        </label>
                        <div className="flex gap-2 justify-end">
                            <button
                                onClick={() => setTrainingModal(null)}
                                className="btn-secondary text-sm"
                            >
                                Cancel
                            </button>
                            <button
                                disabled={!templateConfirmed}
                                onClick={() => {
                                    const iso = trainingDate && trainingTime
                                        ? `${trainingDate}T${trainingTime}:00`
                                        : "";
                                    const payload = { ...trainingModal.basePayload, training_start_at: iso };
                                    if (tMonStart) payload.monday_start = tMonStart;
                                    if (tMonEnd)   payload.monday_end   = tMonEnd;
                                    if (tTueStart) payload.tuesday_start = tTueStart;
                                    if (tTueEnd)   payload.tuesday_end   = tTueEnd;
                                    onMove(trainingModal.candidate.id, payload);
                                    setTrainingModal(null);
                                }}
                                className="btn-primary text-sm disabled:opacity-40 disabled:cursor-not-allowed"
                            >
                                Book to Start
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* Reschedule training start date modal */}
            {rescheduleModal && (
                <div
                    role="dialog"
                    aria-modal="true"
                    onClick={() => { if (!rescheduleBusy) setRescheduleModal(null); }}
                    className="fixed inset-0 z-50 flex items-start sm:items-center justify-center bg-black/60 px-4 py-8 overflow-y-auto"
                >
                    <div onClick={(e) => e.stopPropagation()} className="surface p-6 rounded-xl max-w-sm w-full space-y-5 shadow-xl">
                        <div>
                            <div className="label-overline mb-0.5">Reschedule Start Date</div>
                            <h3 className="font-heading text-lg font-semibold">
                                {rescheduleModal.candidate.first_name} {rescheduleModal.candidate.last_name}
                            </h3>
                            <p className="text-xs text-ink-muted mt-1">
                                A new starter email will be sent automatically with the updated date.
                            </p>
                        </div>
                        <div className="space-y-3">
                            <div>
                                <label className="text-xs font-medium text-ink-muted uppercase tracking-widest mb-1 block">New Start Date</label>
                                {(() => {
                                    const [y, m, d] = rescheduleDate.split("-");
                                    const selClass = "bg-surface-active border border-strokes rounded px-2 py-2 text-sm text-ink focus:outline-none focus:border-brand-primary";
                                    const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
                                    const daysInMonth = new Date(Number(y), Number(m), 0).getDate();
                                    return (
                                        <div className="flex gap-2">
                                            <select value={m} onChange={e => setRescheduleDate(`${y}-${e.target.value}-${d}`)} className={selClass}>
                                                {months.map((mn, i) => <option key={i} value={String(i+1).padStart(2,"0")}>{mn}</option>)}
                                            </select>
                                            <select value={d} onChange={e => setRescheduleDate(`${y}-${m}-${e.target.value}`)} className={selClass}>
                                                {Array.from({length: daysInMonth}, (_, i) => String(i+1).padStart(2,"0")).map(dd => <option key={dd} value={dd}>{dd}</option>)}
                                            </select>
                                            <select value={y} onChange={e => setRescheduleDate(`${e.target.value}-${m}-${d}`)} className={selClass}>
                                                {[2025,2026,2027].map(yr => <option key={yr} value={yr}>{yr}</option>)}
                                            </select>
                                        </div>
                                    );
                                })()}
                            </div>
                            <div>
                                <label className="text-xs font-medium text-ink-muted uppercase tracking-widest mb-1 block">Start Time</label>
                                {(() => {
                                    const [hh, mm] = rescheduleTime.split(":").map(Number);
                                    const period = hh >= 12 ? "PM" : "AM";
                                    const hour12 = hh % 12 || 12;
                                    const selClass = "bg-surface-active border border-strokes rounded px-2 py-2 text-sm text-ink focus:outline-none focus:border-brand-primary";
                                    const setTime = (h12, min, per) => {
                                        let h24 = h12 % 12;
                                        if (per === "PM") h24 += 12;
                                        setRescheduleTime(`${String(h24).padStart(2,"0")}:${String(min).padStart(2,"0")}`);
                                    };
                                    return (
                                        <div className="flex gap-2 items-center">
                                            <select value={hour12} onChange={e => setTime(Number(e.target.value), mm, period)} className={selClass}>
                                                {Array.from({length:12},(_,i)=>i+1).map(h => <option key={h} value={h}>{h}</option>)}
                                            </select>
                                            <span className="text-ink-muted text-sm">:</span>
                                            <select value={mm} onChange={e => setTime(hour12, Number(e.target.value), period)} className={selClass}>
                                                {Array.from({length:12},(_,i)=>i*5).map(min => <option key={min} value={min}>{String(min).padStart(2,"0")}</option>)}
                                            </select>
                                            <select value={period} onChange={e => setTime(hour12, mm, e.target.value)} className={selClass}>
                                                <option value="AM">AM</option>
                                                <option value="PM">PM</option>
                                            </select>
                                        </div>
                                    );
                                })()}
                            </div>
                        </div>
                        <div className="flex gap-2 justify-end">
                            <button onClick={() => setRescheduleModal(null)} className="btn-secondary text-sm" disabled={rescheduleBusy}>
                                Cancel
                            </button>
                            <button
                                disabled={rescheduleBusy}
                                onClick={async () => {
                                    setRescheduleBusy(true);
                                    try {
                                        const iso = `${rescheduleDate}T${rescheduleTime}:00`;
                                        const res = await api.post(`/candidates/${rescheduleModal.candidate.id}/reschedule-training`, { training_start_at: iso });
                                        setRescheduleModal(null);
                                        if (res?.data?.email_status === "sent") {
                                            toast.success("Start date updated — confirmation email sent");
                                        } else {
                                            toast.warning("Start date updated, but the email didn't send — check the candidate's email address and try again");
                                        }
                                        // Refresh the board by triggering a parent re-fetch via a page-level event
                                        window.dispatchEvent(new CustomEvent("cgrecruit:refresh"));
                                    } catch (err) {
                                        toast.error(err?.response?.data?.detail || "Reschedule failed — try again.");
                                    } finally {
                                        setRescheduleBusy(false);
                                    }
                                }}
                                className="btn-primary text-sm"
                            >
                                {rescheduleBusy ? "Saving…" : "Confirm & Resend Email"}
                            </button>
                        </div>
                    </div>
                </div>
            )}

            <DirectBookModal
                open={directBookOpen}
                onClose={() => setDirectBookOpen(false)}
                pipelineId={pipelineId}
                jobs={jobs}
                onBooked={onRefresh}
            />
            <QuickAddTrainingModal
                open={quickTrainingOpen}
                onClose={() => setQuickTrainingOpen(false)}
                pipelineId={pipelineId}
                jobs={jobs}
                onAdded={onRefresh}
            />
        </>
    );
}

const KanbanCard = memo(function KanbanCard({ candidate, onClick, onRequestMove, onResumeQueue, stageKey, onDragStart, selected, selectionMode, onRescheduleTraining, onReject }) {
    const { timezone } = usePipeline() || {};
    const status = STATUS_COLORS[candidate.screening_status] || STATUS_COLORS.pending;
    const initials = `${candidate.first_name?.[0] || ""}${candidate.last_name?.[0] || ""}`.toUpperCase();
    const age = candidateAge(candidate.created_at);
    // Age only escalates in SCREENING: that's the stage a candidate is created
    // into, so created_at doubles as the stage clock there. Later stages already
    // show the date that matters (appointment, training start).
    const ageColor = stageKey === "SCREENING" && age
        ? (age.days >= 14 ? "#F87171" : age.days >= 7 ? "#FBBF24" : undefined)
        : undefined;
    // Once a verdict has been assigned the call is done — don't show "calling".
    const callingNow = candidate.screening_status === "in_progress" && !candidate.verdict;
    const queued = candidate.screening_status === "queued";
    const paused = candidate.screening_status === "paused";
    const nextAt = candidate.next_call_at;
    // A call is "actionable" (recruiter can Approve/Deny) only if the AI agent
    // booked a specific slot mid-call. If no slot was agreed, the recruiter
    // needs to see WHY (call_summary) and decide manually.
    const callDone = !!candidate.verdict && candidate.stage === "SCREENING";
    const hasBooking = !!candidate.appointment_at;
    // "incomplete" verdict OR incomplete_info screening status = call didn't truly happen
    // (voicemail / no response / aborted). Show "Incomplete call - Email/Text sent" badge.
    const callIncompleteCall = candidate.verdict === "incomplete" || candidate.screening_status === "incomplete_info";
    // Only auto-rejected when a hard gate explicitly failed (disqualification_reason set)
    const autoRejected = callDone && !!candidate.disqualification_reason && !callIncompleteCall;
    // Weak but passed hard gates — booked or pending, just flagged with low score
    const weakFlagged = callDone && candidate.verdict === "weak" && !candidate.disqualification_reason && !callIncompleteCall;
    // "actionable" = call done, slot booked, passed gates — no manual approve needed
    const callActionable = callDone && hasBooking && !autoRejected && !callIncompleteCall;
    // "incomplete" = call ran, no slot agreed (and not disqualified/incomplete) → show summary instead.
    const callIncomplete = callDone && !hasBooking && !autoRejected && !callIncompleteCall;

    return (
        <div
            onClick={onClick}
            draggable={!selectionMode}
            onDragStart={(e) => !selectionMode && onDragStart && onDragStart(e, candidate.id, stageKey)}
            className={`kanban-card animate-fade-in relative cursor-grab active:cursor-grabbing ${selected ? "ring-2 ring-brand-primary ring-offset-2 ring-offset-[#0C0C0E]" : ""}`}
            data-testid={`candidate-card-${candidate.id}`}
        >
            {selectionMode && (
                <div
                    className={`absolute top-1.5 left-1.5 w-4 h-4 rounded border-2 flex items-center justify-center transition-colors ${
                        selected ? "bg-brand-primary border-brand-primary" : "bg-transparent border-strokes"
                    }`}
                    data-testid={`select-checkbox-${candidate.id}`}
                >
                    {selected && <span className="text-white text-[10px] font-bold leading-none">✓</span>}
                </div>
            )}
            {callingNow && (
                <div className="absolute top-1.5 right-1.5 flex items-center gap-1 text-[9px] font-semibold uppercase tracking-widest text-[#FBBF24]">
                    <span className="relative flex h-1.5 w-1.5">
                        <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-[#FBBF24] opacity-75"></span>
                        <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-[#FBBF24]"></span>
                    </span>
                    Calling
                </div>
            )}
            <div className="flex items-start justify-between gap-2 mb-1">
                <div className="flex items-center gap-2 min-w-0 flex-1">
                    <div className="relative flex-shrink-0">
                        <div className="w-7 h-7 rounded-full bg-surface-active border border-strokes flex items-center justify-center text-[10px] font-semibold text-ink-muted">
                            {initials || "?"}
                        </div>
                        {(candidate.call_attempts || 0) > 0 && (
                            <span
                                className={`absolute -top-1 -right-1 min-w-[14px] h-[14px] px-1 rounded-full text-[9px] font-bold leading-[14px] text-white text-center ring-2 ring-[#0E0E11] ${
                                    (candidate.call_attempts >= 3) ? "bg-brand-danger" :
                                    (candidate.call_attempts === 2) ? "bg-[#F59E0B]" : "bg-brand-primary"
                                }`}
                                data-testid={`call-attempts-badge-${candidate.id}`}
                                title={`${candidate.call_attempts} call attempt${candidate.call_attempts > 1 ? "s" : ""}`}
                            >
                                {candidate.call_attempts}
                            </span>
                        )}
                    </div>
                    <div className="min-w-0 flex-1">
                        <div className="text-sm font-medium truncate">{candidate.first_name} {candidate.last_name}</div>
                        <div className="flex items-center gap-1.5">
                            <div className="text-[11px] text-ink-muted truncate">{candidate.email || candidate.phone || "—"}</div>
                            {/* Position in the column is otherwise the only age cue, so
                                someone stuck three weeks looks like this morning's arrival. */}
                            {age && (
                                <span
                                    className="text-[10px] text-ink-muted flex-shrink-0 tabular-nums"
                                    style={ageColor ? { color: ageColor } : undefined}
                                    title={`Added ${formatDate(candidate.created_at, timezone)}`}
                                    data-testid={`candidate-age-${candidate.id}`}
                                >
                                    {age.label}
                                </span>
                            )}
                        </div>
                    </div>
                </div>
                {candidate.smart_score != null && (
                    <ScorePill score={candidate.smart_score} />
                )}
            </div>

            {candidate.referred_by && (
                <div className="mt-1 text-[10px] text-ink-muted truncate">
                    Ref: <span className="text-ink">{candidate.referred_by}</span>
                </div>
            )}

            {candidate.verdict && (
                <div className="mt-1.5">
                    <VerdictBadge verdict={candidate.verdict} />
                </div>
            )}

            {/* Form-submitted indicator. Shown on every card whose candidate has
                completed the public form. Tooltip surfaces the submission time so
                recruiters can spot fresh form entries at a glance. Click goes to
                the drawer's Form tab via the underlying card click handler. */}
            {candidate.form_responses && Object.keys(candidate.form_responses).length > 0 && (
                <div
                    className="mt-1.5 inline-flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wide text-brand-primary bg-[rgba(139,92,246,0.10)] border border-[rgba(139,92,246,0.25)] px-1.5 py-0.5 rounded"
                    title={candidate.form_submitted_at ? `Form submitted ${fmtET(candidate.form_submitted_at, { timeZone: timezone })}` : "Form submitted"}
                    data-testid={`form-submitted-badge-${candidate.id}`}
                >
                    <svg width="9" height="9" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className="flex-shrink-0">
                        <path d="M3 7l3 3 7-7" />
                    </svg>
                    Form
                </div>
            )}

            {stageKey === "SCREENING" && (
                <div className="mt-2 space-y-1.5">
                    {callIncompleteCall && (
                        <>
                            <div
                                className="status-pill"
                                style={{ background: "rgba(245,158,11,0.18)", color: "#FBBF24" }}
                                title={candidate.call_summary || "Call did not connect — retry message sent."}
                                data-testid={`incomplete-call-badge-${candidate.id}`}
                            >
                                <Phone size={9} weight="bold" /> {candidate.last_call_voicemail ? "Voicemail — message left" : "Incomplete call — Email/Text sent"}
                            </div>
                            {nextAt && !callingNow && (
                                <CallCountdown nextAt={nextAt} testid={`call-countdown-${candidate.id}`} />
                            )}
                            {candidate.call_summary && (
                                <div
                                    className="text-[10px] text-ink-muted leading-snug line-clamp-2"
                                    data-testid={`incomplete-call-reason-${candidate.id}`}
                                    title={candidate.call_summary}
                                >
                                    {candidate.call_summary}
                                </div>
                            )}
                        </>
                    )}
                    {callIncomplete && (
                        <>
                            <div className="status-pill" style={{ background: "rgba(245,158,11,0.18)", color: "#FBBF24" }} title={candidate.call_summary || ""}>
                                <Phone size={9} weight="bold" /> {candidate.last_call_voicemail ? "Voicemail" : "Incomplete — no time agreed"}
                            </div>
                            {candidate.call_summary && (
                                <div
                                    className="text-[10px] text-ink-muted leading-snug line-clamp-2"
                                    data-testid={`incomplete-reason-${candidate.id}`}
                                    title={candidate.call_summary}
                                >
                                    {candidate.call_summary}
                                </div>
                            )}
                        </>
                    )}
                    {autoRejected && (
                        <>
                            <div className="status-pill" style={{ background: "rgba(239,68,68,0.18)", color: "#F87171" }} title={candidate.disqualification_reason || candidate.call_summary || ""}>
                                <Phone size={9} weight="bold" /> Rejected — {candidate.disqualification_reason || "hard gate"}
                            </div>
                            {candidate.call_summary && (
                                <div
                                    className="text-[10px] text-ink-muted leading-snug line-clamp-2"
                                    data-testid={`rejected-reason-${candidate.id}`}
                                    title={candidate.call_summary}
                                >
                                    {candidate.call_summary}
                                </div>
                            )}
                        </>
                    )}
                    {weakFlagged && (
                        <div
                            className="status-pill"
                            style={{ background: "rgba(245,158,11,0.18)", color: "#FBBF24" }}
                            title={candidate.call_summary || ""}
                            data-testid={`weak-flagged-${candidate.id}`}
                        >
                            ⚠ Low score{candidate.suitability_score != null ? ` · ${candidate.suitability_score}` : ""}
                        </div>
                    )}
                    {!callDone && !callIncompleteCall && (
                        <div className="flex items-center gap-2 flex-wrap">
                            {/* Live countdown — replaces the static "pending" pill the recruiter
                                used to see for the entire warmup_delay_minutes window. Now they
                                see exactly when Olivia will dial. */}
                            {nextAt && !callingNow ? (
                                <CallCountdown
                                    nextAt={nextAt}
                                    testid={`call-countdown-${candidate.id}`}
                                />
                            ) : (
                                <span
                                    className="status-pill"
                                    style={{ background: status.bg, color: status.color }}
                                    data-testid={`screening-status-${candidate.id}`}
                                >
                                    <Phone size={9} weight="bold" /> {status.label}
                                </span>
                            )}
                            {/* "Call now" override — only useful when there's no scheduled time
                                or to bypass the countdown (e.g. recruiter wants to dial
                                immediately while they have the candidate on a different line).
                                A paused candidate lands in the same slot, but re-queues via
                                the dialer instead of moving status — a bare status flip would
                                leave them with no next_call_at and never dialed. */}
                            {!nextAt && (paused || candidate.screening_status === "pending") && (
                                <button
                                    onClick={(e) => {
                                        e.stopPropagation();
                                        if (paused) onResumeQueue?.(candidate);
                                        else onRequestMove(candidate, { screening_status: "queued" }, { destinationLabel: "Queued" });
                                    }}
                                    data-testid={`queue-call-${candidate.id}`}
                                    className="ml-auto text-[11px] text-brand-primary hover:underline"
                                >
                                    {paused ? "Resume calling" : "Queue call"}
                                </button>
                            )}
                        </div>
                    )}
                </div>
            )}

            {stageKey === "APPOINTMENT" && candidate.appointment_at && (() => {
                const noShow = candidate.attendance_status === "no_show";
                const attendedForm = candidate.attendance_status === "attended_form";
                const attendedNoForm = candidate.attendance_status === "attended_no_form";
                const rescheduled = candidate.rescheduled;
                let bg = "rgba(59,130,246,0.08)", border = "rgba(59,130,246,0.2)", textColor = "#60A5FA";
                if (noShow) { bg = "rgba(239,68,68,0.10)"; border = "rgba(239,68,68,0.30)"; textColor = "#F87171"; }
                else if (attendedForm) { bg = "rgba(16,185,129,0.10)"; border = "rgba(16,185,129,0.25)"; textColor = "#10B981"; }
                return (
                    <div
                        className="mt-2 px-2 py-1.5 rounded space-y-1"
                        style={{ background: bg, border: `1px solid ${border}` }}
                        data-testid={`appointment-card-${candidate.id}`}
                    >
                        <div className="text-[11px] font-medium" style={{ color: textColor }}>
                            {formatDate(candidate.appointment_at, timezone)}
                        </div>
                        <div className="text-[10px] text-ink-muted truncate">
                            {candidate.appointment_recruiter || "Zoom"}
                        </div>
                        {/* ONE status line — most decisive state wins, so cards stay
                            scannable instead of stacking 3+ pills. Priority:
                            outcome (no-show/attended) > confirmation > reschedule. */}
                        {(() => {
                            let badge = null;
                            if (noShow) {
                                badge = { icon: <X size={9} weight="bold" />, text: "No-show — reschedule sent", bg: "rgba(239,68,68,0.18)", color: "#F87171", tid: "no-show-badge" };
                            } else if (attendedForm) {
                                badge = { icon: <CheckCircle size={9} weight="bold" />, text: "Attended · form done", bg: "rgba(16,185,129,0.18)", color: "#10B981", tid: "attended-form-badge" };
                            } else if (attendedNoForm) {
                                badge = { icon: <CheckCircle size={9} weight="bold" />, text: "Attended · form pending", bg: "rgba(59,130,246,0.18)", color: "#60A5FA", tid: "attended-no-form-badge" };
                            } else if (candidate.appointment_cancelled_at) {
                                // Outranks confirmation: they told us they can't make this
                                // slot, so a stale "SMS confirmed" would read as attending.
                                badge = { icon: <X size={9} weight="bold" />, text: "Can't attend — needs new time", bg: "rgba(239,68,68,0.18)", color: "#F87171", tid: "cant-attend-badge" };
                            } else if (candidate.appointment_sms_confirmed) {
                                badge = { icon: <CheckCircle size={9} weight="bold" />, text: rescheduled ? "Confirmed · rescheduled" : "SMS confirmed", bg: "rgba(16,185,129,0.15)", color: "#10B981", tid: "sms-confirmed-badge" };
                            } else if (rescheduled) {
                                badge = { icon: <ArrowsClockwise size={9} weight="bold" />, text: "Rescheduled", bg: "rgba(245,158,11,0.18)", color: "#FBBF24", tid: "rescheduled-badge" };
                            }
                            if (!badge) return null;
                            return (
                                <div
                                    className="status-pill mt-1"
                                    style={{ background: badge.bg, color: badge.color }}
                                    data-testid={`${badge.tid}-${candidate.id}`}
                                >
                                    {badge.icon} {badge.text}
                                </div>
                            );
                        })()}
                    </div>
                );
            })()}

            {candidate.rebook_watchlist && (
                <div
                    className="status-pill mt-2"
                    style={{ background: "rgba(139,92,246,0.15)", color: "#A78BFA" }}
                    data-testid={`rebook-watchlist-badge-${candidate.id}`}
                >
                    <EnvelopeSimple size={9} weight="bold" /> Reschedule email scheduled
                </div>
            )}

            {stageKey === "FORM" && (
                <div className="mt-2 space-y-1">
                    {candidate.appointment_at && (
                        <div className="flex items-center gap-1.5 text-[11px] text-ink-muted">
                            <CalendarBlank size={11} />
                            <span>Attended {formatDate(candidate.appointment_at, timezone)}</span>
                        </div>
                    )}
                    <div className="flex items-center gap-2 text-[11px] text-ink-muted">
                        <ClipboardText size={11} />
                        {candidate.form_reminder_at
                            ? <FormReminderCountdown remindAt={candidate.form_reminder_at} />
                            : <span>Form pending</span>
                        }
                    </div>
                </div>
            )}

            {stageKey === "CLOSE" && candidate.form_submitted_at && (
                <div className="mt-2 flex items-center gap-1.5 text-[11px] text-ink-muted">
                    <ClipboardText size={11} />
                    <span>Form submitted {formatDate(candidate.form_submitted_at)}</span>
                </div>
            )}

            {stageKey === "CLOSE" && candidate.form_decline_at && (
                <div className="mt-2 status-pill" style={{ background: "rgba(239,68,68,0.15)", color: "#EF4444" }}>
                    <XCircle size={9} weight="bold" /> Declined
                </div>
            )}
            {stageKey === "CLOSE" && candidate.close_success_at && (
                <div className="mt-2 status-pill" style={{ background: STATUS_COLORS.approved.bg, color: STATUS_COLORS.approved.color }}>
                    <CheckCircle size={9} weight="bold" /> Offered
                </div>
            )}
            {stageKey === "CLOSE" && onReject && (
                <button
                    onClick={(e) => { e.stopPropagation(); onReject(candidate); }}
                    className="mt-2 text-[10px] font-semibold text-red-400 hover:text-red-300 hover:underline flex items-center gap-1"
                    title="Send rejection email and archive"
                    data-testid={`reject-btn-${candidate.id}`}
                >
                    <XCircle size={9} weight="bold" /> Reject
                </button>
            )}

            {stageKey === "TRAINING" && (
                <div className="mt-2 space-y-1">
                    {candidate.training_start_at && (
                        <div className="text-[10px] text-ink-muted">
                            Starts {fmtET(candidate.training_start_at, { timeZone: timezone, weekday: "short", month: "short", day: "numeric" })}
                        </div>
                    )}
                    {candidate.training_attended ? (
                        <div className="status-pill" style={{ background: "rgba(16,185,129,0.15)", color: "#10B981" }}>
                            <CheckCircle size={9} weight="bold" /> Attended
                        </div>
                    ) : (
                        <div className="status-pill" style={{ background: "rgba(139,92,246,0.12)", color: "#A78BFA" }}>
                            <GraduationCap size={9} weight="bold" /> In training
                        </div>
                    )}
                    <button
                        onClick={(e) => { e.stopPropagation(); onRescheduleTraining?.(candidate); }}
                        className="mt-1 text-[10px] font-semibold text-brand-primary hover:underline flex items-center gap-1"
                        title="Reschedule start date"
                    >
                        <ArrowsClockwise size={9} weight="bold" /> Reschedule Start
                    </button>
                </div>
            )}
        </div>
    );
}, (prev, next) => (
    // Skip re-render unless something the card actually shows changed. The
    // 30s poll + SSE refreshes replace the whole candidates array with fresh
    // objects — without this, all ~70 cards re-render at once and scrolling
    // stutters. Any data change bumps updated_at on the backend, so it's a
    // safe change signal.
    prev.candidate.id === next.candidate.id &&
    prev.candidate.updated_at === next.candidate.updated_at &&
    prev.selected === next.selected &&
    prev.selectionMode === next.selectionMode &&
    prev.stageKey === next.stageKey
));

function ScorePill({ score }) {
    const color = score >= 80 ? "#10B981" : score >= 60 ? "#3B82F6" : score >= 40 ? "#F59E0B" : "#EF4444";
    return (
        <span
            className="text-[10px] font-mono font-semibold px-1.5 py-0.5 rounded flex-shrink-0"
            style={{ background: `${color}1A`, color }}
            title={`Smart Score: ${score}`}
        >
            {score}
        </span>
    );
}

/**
 * Live "Olivia calls in M:SS" countdown rendered on a SCREENING-stage card
 * once a call has been auto-scheduled (`next_call_at` set on the candidate).
 *
 * Updates every second so the recruiter can SEE that the system has actually
 * queued the call — replaces the old static "pending" pill that left people
 * wondering whether anything was happening. Falls back to "Calling now…"
 * once the timestamp is in the past.
 */
function CallCountdown({ nextAt, testid }) {
    const { timezone } = usePipeline() || {};
    const [now, setNow] = useState(() => Date.now());
    // next_call_at is a timezone-naive string stored in the account's timezone.
    // Both of these build Intl formatters — far too expensive to redo every
    // second once the board shows dozens of countdowns at once.
    const { target, dialAt } = useMemo(() => {
        const ms = nextAt ? etNaiveToUtcMs(nextAt, timezone) : 0;
        return {
            target: ms,
            dialAt: fmtET(new Date(ms), { timeZone: timezone, hour: '2-digit', minute: '2-digit', timeZoneName: 'short' }),
        };
    }, [nextAt, timezone]);
    const diff = Math.max(0, Math.floor((target - now) / 1000));
    // Retries land hours out, where the seconds digit is noise — tick per
    // minute until the dial is inside the hour.
    const far = diff >= 3600;
    useEffect(() => {
        const t = setInterval(() => setNow(Date.now()), far ? 60000 : 1000);
        return () => clearInterval(t);
    }, [far]);
    // 0 means etNaiveToUtcMs couldn't parse the value — render nothing rather
    // than a confident "Calling now…" pill whose tooltip is dated 1 Jan 1970.
    // The Call Queue's ETA drops the same row for the same reason.
    if (!target) return null;
    const isPast = diff <= 0;
    const h = Math.floor(diff / 3600);
    const m = Math.floor((diff % 3600) / 60);
    const s = diff % 60;
    const label = isPast
        ? "Calling now…"
        : h > 0
            ? `${h}h ${m}m`
            : `${m}:${s.toString().padStart(2, "0")}`;
    return (
        <span
            className="status-pill"
            style={{
                background: isPast ? "rgba(245,158,11,0.18)" : "rgba(139,92,246,0.15)",
                color: isPast ? "#FBBF24" : "#A78BFA",
            }}
            data-testid={testid}
            title={`Olivia will dial at ${dialAt}`}
        >
            <Phone size={9} weight="bold" /> Olivia calls in {label}
        </span>
    );
}

function FormReminderCountdown({ remindAt }) {
    const { timezone } = usePipeline() || {};
    const [now, setNow] = useState(() => Date.now());
    // Same Intl cost as CallCountdown, and reminders are usually a day out.
    const remindAtLabel = useMemo(
        () => fmtET(remindAt, { timeZone: timezone, hour: '2-digit', minute: '2-digit', timeZoneName: 'short' }),
        [remindAt, timezone],
    );
    const target = new Date(remindAt).getTime();
    const diff = Math.max(0, Math.floor((target - now) / 1000));
    const far = diff >= 3600;
    useEffect(() => {
        const t = setInterval(() => setNow(Date.now()), far ? 60000 : 1000);
        return () => clearInterval(t);
    }, [far]);
    const isPast = diff <= 0;
    const h = Math.floor(diff / 3600);
    const m = Math.floor((diff % 3600) / 60);
    const s = diff % 60;
    const label = isPast
        ? "Reminder sent"
        : h > 0
            ? `${h}h ${m}m until reminder`
            : `${m}:${s.toString().padStart(2, "0")} until reminder`;
    return (
        <span
            className="status-pill"
            style={{
                background: isPast ? "rgba(16,185,129,0.15)" : "rgba(245,158,11,0.15)",
                color: isPast ? "#10B981" : "#FBBF24",
            }}
            title={`Form reminder scheduled for ${remindAtLabel}`}
        >
            <ClipboardText size={9} weight="bold" /> {label}
        </span>
    );
}

const VERDICT_STYLES = {
    strong: { bg: "rgba(16,185,129,0.2)", color: "#10B981", label: "STRONG FIT" },
    good: { bg: "rgba(59,130,246,0.2)", color: "#60A5FA", label: "GOOD" },
    borderline: { bg: "rgba(245,158,11,0.2)", color: "#FBBF24", label: "BORDERLINE" },
    weak: { bg: "rgba(239,68,68,0.18)", color: "#F87171", label: "WEAK" },
    incomplete: { bg: "rgba(245,158,11,0.18)", color: "#FBBF24", label: "INCOMPLETE" },
};

export function VerdictBadge({ verdict, size = "sm" }) {
    if (!verdict) return null;
    const v = VERDICT_STYLES[verdict] || VERDICT_STYLES.borderline;
    const cls = size === "lg" ? "text-[11px] px-2.5 py-1" : "text-[9px] px-1.5 py-0.5";
    return (
        <span
            className={`inline-flex items-center gap-1 font-bold uppercase tracking-widest rounded ${cls}`}
            style={{ background: v.bg, color: v.color }}
            data-testid={`verdict-badge-${verdict}`}
        >
            {v.label}
        </span>
    );
}

function formatDate(iso, tz) {
    return fmtET(iso, { timeZone: tz, weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) || iso;
}

/** Compact candidate age ("40m", "6h", "3d", "5w") from created_at. This is age
 * since the candidate arrived, NOT time in stage — no stage-entry timestamp is
 * stored — so callers must label it "Added", not "in stage". */
function candidateAge(iso) {
    const t = iso ? new Date(iso).getTime() : NaN;
    if (Number.isNaN(t)) return null;
    const mins = Math.max(0, Math.floor((Date.now() - t) / 60000));
    const days = Math.floor(mins / 1440);
    const label = mins < 60
        ? `${mins}m`
        : days < 1
            ? `${Math.floor(mins / 60)}h`
            : days < 14
                ? `${days}d`
                : `${Math.floor(days / 7)}w`;
    return { label, days };
}
