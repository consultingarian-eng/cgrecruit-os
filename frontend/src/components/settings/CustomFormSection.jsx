import { useState, useEffect, useRef } from "react";
import api from "@/lib/api";
import { toast } from "sonner";
import { Plus, Trash, ArrowUp, ArrowDown, DotsSixVertical, Eye, ClipboardText, X } from "@phosphor-icons/react";
import { Switch } from "@/components/ui/switch";
import {
    Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription,
} from "@/components/ui/dialog";

const TYPES = [
    { value: "text", label: "Text Box" },
    { value: "multiple_choice", label: "Multiple Choice" },
    { value: "date", label: "Date Selector" },
];

export default function CustomFormSection({ settings, onSaved, pipelineId }) {
    const [questions, setQuestions] = useState(settings.custom_form?.questions || []);
    const [busy, setBusy] = useState(false);
    const [dragIndex, setDragIndex] = useState(null);
    const [dragOverIndex, setDragOverIndex] = useState(null);
    const [previewOpen, setPreviewOpen] = useState(false);
    const [previewResponses, setPreviewResponses] = useState({});

    // Only re-hydrate questions when scope (pipeline) changes — not on every
    // settings prop refresh. Background refreshes were wiping unsaved drag
    // reorders / new question text.
    const lastScopeRef = useRef(pipelineId);
    useEffect(() => {
        if (lastScopeRef.current !== pipelineId) {
            lastScopeRef.current = pipelineId;
            setQuestions(settings.custom_form?.questions || []);
        }
    }, [pipelineId, settings]);

    const save = async () => {
        setBusy(true);
        try {
            const params = pipelineId ? { pipeline_id: pipelineId } : {};
            await api.put("/settings/custom-form", { questions }, { params });
            await onSaved?.();
            toast.success("Form saved");
        } catch { toast.error("Failed to save"); }
        finally { setBusy(false); }
    };

    /**
     * Show the form EXACTLY as the candidate sees it after their interview
     * (FORM stage on the applicant status page). This is NOT the public-apply
     * form — that's the role-application form. The custom_form questions are
     * sent post-interview as a step before the closing 1-on-1 call, so the
     * preview should mirror the FORM-stage rendering, not /apply/{slug}.
     */
    const previewForm = () => {
        setPreviewResponses({});
        setPreviewOpen(true);
    };

    const add = () => setQuestions([
        ...questions,
        { question: "", answer_type: "text", options: [], required: true, id: crypto.randomUUID() },
    ]);

    const update = (i, patch) => {
        setQuestions((prev) => {
            const next = [...prev];
            next[i] = { ...next[i], ...patch };
            return next;
        });
    };

    const remove = (i) => {
        setQuestions((prev) => prev.filter((_, idx) => idx !== i));
    };

    /** Move question at `from` to position `to`, shifting the rest. */
    const reorder = (from, to) => {
        if (from === to || from < 0 || to < 0) return;
        setQuestions((prev) => {
            if (to >= prev.length) return prev;
            const next = [...prev];
            const [moved] = next.splice(from, 1);
            next.splice(to, 0, moved);
            return next;
        });
    };

    const moveUp = (i) => reorder(i, i - 1);
    const moveDown = (i) => reorder(i, i + 1);

    // ---- HTML5 drag-and-drop ----
    const onDragStart = (e, i) => {
        setDragIndex(i);
        e.dataTransfer.effectAllowed = "move";
        // Required for Firefox to fire dragover on the target.
        try { e.dataTransfer.setData("text/plain", String(i)); } catch { /* noop */ }
    };
    const onDragOver = (e, i) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        if (dragOverIndex !== i) setDragOverIndex(i);
    };
    const onDragLeave = (i) => {
        if (dragOverIndex === i) setDragOverIndex(null);
    };
    const onDrop = (e, i) => {
        e.preventDefault();
        const from = dragIndex;
        setDragIndex(null);
        setDragOverIndex(null);
        if (from === null || from === i) return;
        reorder(from, i);
    };
    const onDragEnd = () => {
        setDragIndex(null);
        setDragOverIndex(null);
    };

    return (
        <div className="max-w-3xl space-y-6" data-testid="custom-form-section">
            <div>
                <h2 className="font-heading text-2xl font-bold tracking-tight">Post-Interview Form</h2>
                <p className="text-sm text-ink-muted mt-1 leading-relaxed">
                    These questions are sent to the candidate <strong className="text-ink">after their interview</strong>, before the closing 1-on-1 call.
                    Use them to confirm understanding, available start dates, and anything else you need before
                    moving them to a hire decision. Drag the <DotsSixVertical size={11} weight="bold" className="inline align-text-bottom" /> handle (or use ↑/↓) to set the order.
                </p>
            </div>
            <div className="flex items-center gap-2 flex-wrap">
                <button onClick={add} data-testid="add-form-question-btn" className="btn-secondary flex items-center gap-1.5">
                    <Plus size={13} weight="bold" /> Add Question
                </button>
                <button
                    onClick={previewForm}
                    data-testid="preview-form-btn"
                    className="btn-secondary flex items-center gap-1.5"
                    title="Show the form exactly as it will appear to a candidate after their interview"
                >
                    <Eye size={13} weight="bold" /> Preview Form
                </button>
            </div>
            <div className="space-y-3">
                {questions.length === 0 && (
                    <div className="surface p-8 text-center text-sm text-ink-muted">No custom questions yet.</div>
                )}
                {questions.map((q, i) => {
                    const isDragging = dragIndex === i;
                    const isDropTarget = dragOverIndex === i && dragIndex !== null && dragIndex !== i;
                    return (
                        <div
                            key={q.id || i}
                            draggable
                            onDragStart={(e) => onDragStart(e, i)}
                            onDragOver={(e) => onDragOver(e, i)}
                            onDragLeave={() => onDragLeave(i)}
                            onDrop={(e) => onDrop(e, i)}
                            onDragEnd={onDragEnd}
                            className={`surface p-4 space-y-3 transition-all ${
                                isDragging ? "opacity-40" : ""
                            } ${
                                isDropTarget ? "ring-2 ring-brand-primary ring-offset-2 ring-offset-[#0C0C0E]" : ""
                            }`}
                            data-testid={`form-question-${i}`}
                        >
                            <div className="flex items-center justify-between gap-2">
                                <div className="flex items-center gap-2 min-w-0">
                                    <span
                                        className="text-ink-muted hover:text-ink cursor-grab active:cursor-grabbing select-none touch-none"
                                        title="Drag to reorder"
                                        data-testid={`form-q-drag-${i}`}
                                    >
                                        <DotsSixVertical size={16} weight="bold" />
                                    </span>
                                    <span className="text-xs text-ink-muted font-mono">Q{i + 1}</span>
                                </div>
                                <div className="flex items-center gap-1">
                                    <button
                                        type="button"
                                        onClick={() => moveUp(i)}
                                        disabled={i === 0}
                                        data-testid={`form-q-up-${i}`}
                                        className="p-1.5 rounded hover:bg-surface-hover disabled:opacity-25 disabled:cursor-not-allowed text-ink-muted hover:text-ink transition-colors"
                                        title="Move up"
                                    >
                                        <ArrowUp size={12} weight="bold" />
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => moveDown(i)}
                                        disabled={i === questions.length - 1}
                                        data-testid={`form-q-down-${i}`}
                                        className="p-1.5 rounded hover:bg-surface-hover disabled:opacity-25 disabled:cursor-not-allowed text-ink-muted hover:text-ink transition-colors"
                                        title="Move down"
                                    >
                                        <ArrowDown size={12} weight="bold" />
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => remove(i)}
                                        data-testid={`remove-form-q-${i}`}
                                        className="text-brand-danger p-1.5 hover:bg-surface-hover rounded transition-colors"
                                        title="Remove question"
                                    >
                                        <Trash size={12} />
                                    </button>
                                </div>
                            </div>
                            <input
                                data-testid={`form-q-text-${i}`}
                                className="input-dark"
                                placeholder="Question text"
                                value={q.question || ""}
                                onChange={(e) => update(i, { question: e.target.value })}
                            />
                            <div className="grid grid-cols-2 gap-3">
                                <select className="input-dark" data-testid={`form-q-type-${i}`} value={q.answer_type} onChange={(e) => update(i, { answer_type: e.target.value })}>
                                    {TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
                                </select>
                                <label className="flex items-center gap-2 text-sm">
                                    <Switch checked={!!q.required} onCheckedChange={(v) => update(i, { required: v })} data-testid={`form-q-required-${i}`} />
                                    Required
                                </label>
                            </div>
                            {q.answer_type === "multiple_choice" && (
                                <input
                                    className="input-dark"
                                    data-testid={`form-q-options-${i}`}
                                    placeholder="Comma-separated options (e.g., Downtown, Riverside)"
                                    value={(q.options || []).join("; ")}
                                    onChange={(e) => update(i, { options: e.target.value.split(";").map((s) => s.trim()).filter(Boolean) })}
                                />
                            )}
                        </div>
                    );
                })}
            </div>
            <button onClick={save} disabled={busy} data-testid="save-form-btn" className="btn-primary">
                {busy ? "Saving…" : "Save Form"}
            </button>

            {/* Preview modal — renders the form exactly as the candidate sees it
                at the FORM stage on /applicant/{token}, but in a non-submitting
                preview shell. Uses the LIVE state of `questions` (not the saved
                version) so reordering edits show up immediately. */}
            <Dialog open={previewOpen} onOpenChange={setPreviewOpen}>
                <DialogContent className="bg-[#0E0E11] border-strokes max-w-lg max-h-[85vh] overflow-y-auto" data-testid="form-preview-dialog">
                    <DialogHeader>
                        <DialogTitle className="font-heading text-xl tracking-tight flex items-center gap-2">
                            <ClipboardText size={18} weight="duotone" className="text-brand-primary" />
                            Preview — what candidates see
                        </DialogTitle>
                        <DialogDescription className="text-xs text-ink-muted leading-relaxed pt-1">
                            This is the post-interview questionnaire after their attendance is marked.
                            Sent before the closing 1-on-1 call. Read-only here — answers don't save.
                        </DialogDescription>
                    </DialogHeader>
                    <div className="surface p-5 mt-2 space-y-4">
                        <div className="flex items-center gap-2 mb-1">
                            <ClipboardText size={20} weight="fill" className="text-brand-primary" />
                            <h3 className="font-heading text-lg font-semibold">One last questionnaire</h3>
                        </div>
                        <p className="text-sm text-ink-muted">
                            A quick 3–5 minute questionnaire. Your answers help us match you to the best team.
                        </p>
                        <div className="space-y-4">
                            {questions.length === 0 && (
                                <div className="text-sm text-ink-muted italic">
                                    No questions yet — add some above to see the candidate-facing form.
                                </div>
                            )}
                            {questions.map((q, i) => (
                                <div key={q.id || `preview-${i}`} data-testid={`preview-q-${i}`}>
                                    <label className="label-overline block mb-1.5">
                                        {q.question || <span className="italic text-ink-dim">(question text empty — Q{i + 1})</span>}
                                        {q.required && <span className="text-brand-danger ml-1">*</span>}
                                    </label>
                                    {q.answer_type === "text" && (
                                        <textarea
                                            rows={3}
                                            className="input-dark"
                                            value={previewResponses[q.id] || ""}
                                            onChange={(e) => setPreviewResponses({ ...previewResponses, [q.id]: e.target.value })}
                                        />
                                    )}
                                    {q.answer_type === "date" && (
                                        <input
                                            type="date"
                                            className="input-dark"
                                            value={previewResponses[q.id] || ""}
                                            onChange={(e) => setPreviewResponses({ ...previewResponses, [q.id]: e.target.value })}
                                        />
                                    )}
                                    {q.answer_type === "multiple_choice" && (
                                        <select
                                            className="input-dark"
                                            value={previewResponses[q.id] || ""}
                                            onChange={(e) => setPreviewResponses({ ...previewResponses, [q.id]: e.target.value })}
                                        >
                                            <option value="">Choose…</option>
                                            {(q.options || []).map((o) => <option key={o} value={o}>{o}</option>)}
                                        </select>
                                    )}
                                </div>
                            ))}
                        </div>
                        {questions.length > 0 && (
                            <button
                                disabled
                                className="btn-primary w-full mt-3 opacity-60 cursor-not-allowed"
                                title="Preview only — submit is disabled"
                                data-testid="preview-submit-disabled"
                            >
                                Submit (preview — disabled)
                            </button>
                        )}
                    </div>
                </DialogContent>
            </Dialog>
        </div>
    );
}
