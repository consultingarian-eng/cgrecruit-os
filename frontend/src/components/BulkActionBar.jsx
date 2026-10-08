import { useState } from "react";
import api from "@/lib/api";
import { toast } from "sonner";
import {
    Dialog, DialogContent, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { CheckSquare, X, ArrowRight, Envelope, Trash, Download, Archive, ArrowCounterClockwise, PhoneOutgoing } from "@phosphor-icons/react";

// No Applicant option — the board renders no Applicant column, so anyone moved
// there drops off every view and can only be found again by search.
const STAGE_OPTIONS = [
    { value: "SCREENING", label: "Screening" },
    { value: "APPOINTMENT", label: "Appointment" },
    { value: "FORM", label: "Form" },
    { value: "CLOSE", label: "To Close" },
    { value: "TRAINING", label: "Training" },
];

const TEMPLATE_OPTIONS = [
    { value: "warmup", label: "Warmup" },
    { value: "screening_retry", label: "Screening retry" },
    { value: "approval", label: "Approval / appointment confirmed" },
    { value: "appointment_no_show", label: "No-show reschedule" },
    { value: "form", label: "Form questionnaire" },
    { value: "rejection", label: "Polite rejection" },
];

const csvCell = (v) => {
    if (v == null) return "";
    const s = String(v).replace(/"/g, '""');
    return /[",\n]/.test(s) ? `"${s}"` : s;
};

export default function BulkActionBar({ selectedIds, candidates, onClearSelection, onAfterAction, showArchived }) {
    const [moveOpen, setMoveOpen] = useState(false);
    const [templateOpen, setTemplateOpen] = useState(false);
    const [deleteOpen, setDeleteOpen] = useState(false);
    const [archiveOpen, setArchiveOpen] = useState(false);
    const [unarchiveOpen, setUnarchiveOpen] = useState(false);
    const [retryOpen, setRetryOpen] = useState(false);
    const [moveStage, setMoveStage] = useState("");
    const [moveTemplate, setMoveTemplate] = useState("");
    const [bulkTemplate, setBulkTemplate] = useState("");
    const [archiveRejected, setArchiveRejected] = useState(true);
    const [busy, setBusy] = useState(false);
    const [progress, setProgress] = useState(null);

    // Every action works off the candidates actually on the board, so the "N
    // selected" label, the dialog headings and the CSV can't disagree — a
    // selection made before a filter narrowed the board used to be counted here
    // and then silently skipped by the export.
    const targets = candidates.filter((c) => selectedIds.has(c.id));
    const count = targets.length;
    if (count === 0) return null;

    /** Serial fan-out, one request per candidate. Reports a running count because
     *  forty archives otherwise sit behind a disabled button for a minute with
     *  nothing on screen moving. */
    const runEach = async (fn) => {
        setBusy(true);
        setProgress({ done: 0, total: count });
        let ok = 0, fail = 0;
        for (const c of targets) {
            try { await fn(c.id); ok += 1; } catch { fail += 1; }
            setProgress({ done: ok + fail, total: count });
        }
        setBusy(false);
        setProgress(null);
        return { ok, fail };
    };

    const runLabel = (verb, idle) => (busy ? `${verb} ${progress?.done ?? 0}/${progress?.total ?? count}…` : idle);

    const runMove = async () => {
        if (!moveStage) return toast.error("Pick a stage");
        const { ok, fail } = await runEach((id) => {
            const payload = moveTemplate
                ? { stage: moveStage, send_email_template: moveTemplate }
                : { stage: moveStage };
            return api.post(`/candidates/${id}/move`, payload);
        });
        setMoveOpen(false);
        setMoveStage("");
        setMoveTemplate("");
        toast.success(`Moved ${ok}${fail ? ` · ${fail} failed` : ""}`);
        onAfterAction?.();
        onClearSelection();
    };

    const runSendTemplate = async () => {
        if (!bulkTemplate) return toast.error("Pick a template");
        const alsoArchive = bulkTemplate === "rejection" && archiveRejected;
        // The archive is counted apart from the send: an archive that fails must
        // not report the email as unsent, or the recruiter retries the row and
        // the candidate gets a second rejection email and SMS.
        let archived = 0, archiveFail = 0;
        const { ok, fail } = await runEach(async (id) => {
            await api.post(`/candidates/${id}/email`, { template_key: bulkTemplate });
            // A rejected candidate left on the board still reads as live. The
            // per-card Reject emails AND archives; without this the two paths
            // leave the board in different states for the same decision.
            if (alsoArchive) {
                try { await api.post(`/candidates/${id}/archive`, { reason: "rejected" }); archived += 1; }
                catch { archiveFail += 1; }
            }
        });
        setTemplateOpen(false);
        setBulkTemplate("");
        toast.success(
            `Sent ${ok}`
            + (alsoArchive ? ` · archived ${archived}` : "")
            + (fail ? ` · ${fail} failed to send` : "")
            + (archiveFail ? ` · ${archiveFail} sent but not archived` : "")
        );
        onAfterAction?.();
        // Archived rows leave the board, so holding the selection through a
        // partial failure leaves exactly the candidates still needing archiving.
        if (alsoArchive && !archiveFail) onClearSelection();
    };

    const runArchive = async () => {
        const { ok, fail } = await runEach((id) => api.post(`/candidates/${id}/archive`));
        setArchiveOpen(false);
        toast.success(`Archived ${ok}${fail ? ` · ${fail} failed` : ""}`);
        onAfterAction?.();
        onClearSelection();
    };

    const runUnarchive = async () => {
        const { ok, fail } = await runEach((id) => api.post(`/candidates/${id}/restore`));
        setUnarchiveOpen(false);
        toast.success(`Restored ${ok}${fail ? ` · ${fail} failed` : ""}`);
        onAfterAction?.();
        onClearSelection();
    };

    const runUnarchiveRetry = async () => {
        setBusy(true);
        try {
            const r = await api.post("/candidates/bulk-unarchive-retry", { candidate_ids: targets.map((c) => c.id) });
            const { queued = 0, total = 0 } = r.data || {};
            toast.success(`Restored ${total} · ${queued} call${queued === 1 ? "" : "s"} queued${total > queued ? ` · ${total - queued} skipped (no phone)` : ""}`);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to unarchive & retry");
        }
        setBusy(false);
        setRetryOpen(false);
        onAfterAction?.();
        onClearSelection();
    };

    const runDelete = async () => {
        const { ok, fail } = await runEach((id) => api.delete(`/candidates/${id}`));
        setDeleteOpen(false);
        toast.success(`Deleted ${ok}${fail ? ` · ${fail} failed` : ""}`);
        onAfterAction?.();
        onClearSelection();
    };

    const runExportCsv = () => {
        const rows = [
            ["First Name", "Last Name", "Email", "Phone", "Stage", "Verdict", "Status", "Smart Score", "Rating", "Appointment", "Created"],
            ...targets.map((c) => [
                c.first_name, c.last_name, c.email, c.phone, c.stage,
                c.verdict || "", c.screening_status || "",
                c.smart_score ?? "", c.rating ?? "",
                c.appointment_at || "", c.created_at || "",
            ]),
        ];
        const csv = rows.map((r) => r.map(csvCell).join(",")).join("\n");
        const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `candidates-${new Date().toISOString().split("T")[0]}.csv`;
        a.click();
        URL.revokeObjectURL(url);
        toast.success(`Exported ${targets.length} candidate${targets.length > 1 ? "s" : ""}`);
    };

    return (
        <>
            <div
                className="fixed bottom-4 left-1/2 -translate-x-1/2 max-w-[calc(100vw-1rem)] overflow-x-auto bg-[#0E0E11] border border-strokes rounded-2xl shadow-2xl px-3 py-2 flex items-center gap-2"
                style={{ zIndex: 50 }}
                data-testid="bulk-action-bar"
            >
                <div className="px-2 text-xs font-semibold flex items-center gap-1.5 shrink-0 whitespace-nowrap">
                    <CheckSquare size={12} weight="duotone" className="text-brand-primary" />
                    {count} selected
                </div>
                <div className="h-4 w-px bg-strokes shrink-0" />
                <button onClick={() => setMoveOpen(true)} data-testid="bulk-move-btn" className="text-xs px-3 py-1 rounded-full hover:bg-surface-hover flex items-center gap-1 shrink-0 whitespace-nowrap">
                    Move <ArrowRight size={10} weight="bold" />
                </button>
                <button onClick={() => setTemplateOpen(true)} data-testid="bulk-template-btn" className="text-xs px-3 py-1 rounded-full hover:bg-surface-hover flex items-center gap-1 shrink-0 whitespace-nowrap">
                    <Envelope size={10} /> Send template
                </button>
                <button onClick={runExportCsv} data-testid="bulk-csv-btn" className="text-xs px-3 py-1 rounded-full hover:bg-surface-hover flex items-center gap-1 shrink-0 whitespace-nowrap">
                    <Download size={10} weight="bold" /> CSV
                </button>
                {showArchived ? (
                    <>
                        <button onClick={() => setUnarchiveOpen(true)} data-testid="bulk-unarchive-btn" className="text-xs px-3 py-1 rounded-full text-emerald-400 hover:bg-emerald-400/10 flex items-center gap-1 shrink-0 whitespace-nowrap">
                            <ArrowCounterClockwise size={10} weight="bold" /> Unarchive
                        </button>
                        <button onClick={() => setRetryOpen(true)} data-testid="bulk-retry-btn" className="text-xs px-3 py-1 rounded-full text-brand-primary hover:bg-brand-primary/10 flex items-center gap-1 shrink-0 whitespace-nowrap">
                            <PhoneOutgoing size={10} weight="bold" /> Unarchive & retry calls
                        </button>
                    </>
                ) : (
                    <button onClick={() => setArchiveOpen(true)} data-testid="bulk-archive-btn" className="text-xs px-3 py-1 rounded-full text-amber-400 hover:bg-amber-400/10 flex items-center gap-1 shrink-0 whitespace-nowrap">
                        <Archive size={10} weight="bold" /> Archive
                    </button>
                )}
                <button onClick={() => setDeleteOpen(true)} data-testid="bulk-delete-btn" className="text-xs px-3 py-1 rounded-full text-brand-danger hover:bg-brand-danger/10 flex items-center gap-1 shrink-0 whitespace-nowrap">
                    <Trash size={10} weight="bold" /> Delete
                </button>
                <div className="h-4 w-px bg-strokes shrink-0" />
                <button onClick={onClearSelection} data-testid="bulk-clear-btn" className="text-xs px-2 py-1 rounded-full text-ink-muted hover:bg-surface-hover shrink-0">
                    <X size={11} weight="bold" />
                </button>
            </div>

            {/* Move dialog */}
            <Dialog open={moveOpen} onOpenChange={(o) => !o && setMoveOpen(false)}>
                <DialogContent className="bg-[#0E0E11] border-strokes max-w-md" data-testid="bulk-move-dialog">
                    <DialogHeader>
                        <DialogTitle className="font-heading text-xl">Move {count} candidate{count > 1 ? "s" : ""}</DialogTitle>
                    </DialogHeader>
                    <div className="space-y-3">
                        <div>
                            <label className="label-overline block mb-1.5">Move to stage</label>
                            <select className="input-dark" data-testid="bulk-move-stage" value={moveStage} onChange={(e) => setMoveStage(e.target.value)}>
                                <option value="">Select stage…</option>
                                {STAGE_OPTIONS.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
                            </select>
                        </div>
                        <div>
                            <label className="label-overline block mb-1.5">Email template <span className="text-ink-dim normal-case">(optional)</span></label>
                            <select className="input-dark" data-testid="bulk-move-template" value={moveTemplate} onChange={(e) => setMoveTemplate(e.target.value)}>
                                <option value="">No email</option>
                                {TEMPLATE_OPTIONS.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
                            </select>
                        </div>
                        <div className="flex gap-2 pt-2">
                            <button onClick={() => setMoveOpen(false)} className="btn-secondary flex-1">Cancel</button>
                            <button onClick={runMove} disabled={busy || !moveStage} data-testid="bulk-move-confirm" className="btn-primary flex-1 disabled:opacity-40">
                                {runLabel("Moving", "Move")}
                            </button>
                        </div>
                    </div>
                </DialogContent>
            </Dialog>

            {/* Send template dialog */}
            <Dialog open={templateOpen} onOpenChange={(o) => !o && setTemplateOpen(false)}>
                <DialogContent className="bg-[#0E0E11] border-strokes max-w-md" data-testid="bulk-template-dialog">
                    <DialogHeader>
                        <DialogTitle className="font-heading text-xl">Email {count} candidate{count > 1 ? "s" : ""}</DialogTitle>
                    </DialogHeader>
                    <div className="space-y-3">
                        <div>
                            <label className="label-overline block mb-1.5">Template</label>
                            <select className="input-dark" data-testid="bulk-template-select" value={bulkTemplate} onChange={(e) => setBulkTemplate(e.target.value)}>
                                <option value="">Select template…</option>
                                {TEMPLATE_OPTIONS.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
                            </select>
                        </div>
                        <p className="text-[11px] text-ink-muted">
                            Each candidate will receive the rendered email with their own merge variables.
                        </p>
                        {bulkTemplate === "rejection" && (
                            <label className="flex items-start gap-2 text-[11px] text-ink-muted cursor-pointer select-none">
                                <input
                                    type="checkbox"
                                    className="accent-brand-primary mt-0.5"
                                    data-testid="bulk-template-archive-rejected"
                                    checked={archiveRejected}
                                    onChange={(e) => setArchiveRejected(e.target.checked)}
                                />
                                Archive them too, the way the Reject button on a card does — otherwise they stay on the board looking live. You can restore them from the Archived view.
                            </label>
                        )}
                        <div className="flex gap-2 pt-2">
                            <button onClick={() => setTemplateOpen(false)} className="btn-secondary flex-1">Cancel</button>
                            <button onClick={runSendTemplate} disabled={busy || !bulkTemplate} data-testid="bulk-template-confirm" className="btn-primary flex-1 disabled:opacity-40">
                                {runLabel("Sending", `Send to ${count}`)}
                            </button>
                        </div>
                    </div>
                </DialogContent>
            </Dialog>

            {/* Archive dialog */}
            <Dialog open={archiveOpen} onOpenChange={(o) => !o && setArchiveOpen(false)}>
                <DialogContent className="bg-[#0E0E11] border-strokes max-w-md" data-testid="bulk-archive-dialog">
                    <DialogHeader>
                        <DialogTitle className="font-heading text-xl">Archive {count} candidate{count > 1 ? "s" : ""}?</DialogTitle>
                    </DialogHeader>
                    <p className="text-sm text-ink-muted">
                        Archived candidates are hidden from the dashboard and all pending calls/SMS are cancelled. Their data is kept and they can be restored individually from their card.
                    </p>
                    <div className="flex gap-2 pt-2">
                        <button onClick={() => setArchiveOpen(false)} className="btn-secondary flex-1">Cancel</button>
                        <button onClick={runArchive} disabled={busy} data-testid="bulk-archive-confirm" className="bg-amber-500 text-white px-4 py-2 rounded text-sm font-semibold flex-1 disabled:opacity-40">
                            {runLabel("Archiving", `Archive ${count}`)}
                        </button>
                    </div>
                </DialogContent>
            </Dialog>

            {/* Unarchive dialog */}
            <Dialog open={unarchiveOpen} onOpenChange={(o) => !o && setUnarchiveOpen(false)}>
                <DialogContent className="bg-[#0E0E11] border-strokes max-w-md" data-testid="bulk-unarchive-dialog">
                    <DialogHeader>
                        <DialogTitle className="font-heading text-xl">Unarchive {count} candidate{count > 1 ? "s" : ""}?</DialogTitle>
                    </DialogHeader>
                    <p className="text-sm text-ink-muted">
                        Restores them to the active pipeline with their prior stage and status intact.
                        Doesn't queue any new calls — use "Unarchive &amp; retry calls" if you want to re-engage them too.
                    </p>
                    <div className="flex gap-2 pt-2">
                        <button onClick={() => setUnarchiveOpen(false)} className="btn-secondary flex-1">Cancel</button>
                        <button onClick={runUnarchive} disabled={busy} data-testid="bulk-unarchive-confirm" className="bg-emerald-500 text-white px-4 py-2 rounded text-sm font-semibold flex-1 disabled:opacity-40">
                            {runLabel("Restoring", `Unarchive ${count}`)}
                        </button>
                    </div>
                </DialogContent>
            </Dialog>

            {/* Unarchive & retry calls dialog */}
            <Dialog open={retryOpen} onOpenChange={(o) => !o && setRetryOpen(false)}>
                <DialogContent className="bg-[#0E0E11] border-strokes max-w-md" data-testid="bulk-retry-dialog">
                    <DialogHeader>
                        <DialogTitle className="font-heading text-xl">Unarchive &amp; retry calls for {count} candidate{count > 1 ? "s" : ""}?</DialogTitle>
                    </DialogHeader>
                    <p className="text-sm text-ink-muted">
                        Restores them, resets call attempts, and re-queues a fresh screening call sequence for each —
                        almost like their first attempt. Calls are staggered and respect your call window, not fired all at once.
                        If someone previously replied STOP to texts, they'll still be called and emailed —
                        we just can never text that number again.
                    </p>
                    <div className="flex gap-2 pt-2">
                        <button onClick={() => setRetryOpen(false)} className="btn-secondary flex-1">Cancel</button>
                        <button onClick={runUnarchiveRetry} disabled={busy} data-testid="bulk-retry-confirm" className="btn-primary flex-1 disabled:opacity-40">
                            {busy ? "Queuing…" : `Unarchive & retry ${count}`}
                        </button>
                    </div>
                </DialogContent>
            </Dialog>

            {/* Delete dialog */}
            <Dialog open={deleteOpen} onOpenChange={(o) => !o && setDeleteOpen(false)}>
                <DialogContent className="bg-[#0E0E11] border-strokes max-w-md" data-testid="bulk-delete-dialog">
                    <DialogHeader>
                        <DialogTitle className="font-heading text-xl text-brand-danger">Delete {count} candidate{count > 1 ? "s" : ""}?</DialogTitle>
                    </DialogHeader>
                    <p className="text-sm text-ink-muted">
                        This permanently removes the candidate, all communications, and their conversations. Cannot be undone.
                    </p>
                    <div className="flex gap-2 pt-2">
                        <button onClick={() => setDeleteOpen(false)} className="btn-secondary flex-1">Cancel</button>
                        <button onClick={runDelete} disabled={busy} data-testid="bulk-delete-confirm" className="bg-brand-danger text-white px-4 py-2 rounded text-sm font-semibold flex-1 disabled:opacity-40">
                            {runLabel("Deleting", "Delete forever")}
                        </button>
                    </div>
                </DialogContent>
            </Dialog>
        </>
    );
}
