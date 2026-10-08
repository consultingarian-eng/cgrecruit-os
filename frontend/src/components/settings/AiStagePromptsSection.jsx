import { useState, useEffect, useCallback } from "react";
import api from "@/lib/api";
import { toast } from "sonner";
import { Robot, ChatTeardrop, EnvelopeSimple } from "@phosphor-icons/react";

const STAGES = [
    {
        key: "SCREENING",
        label: "Screening",
        description: "Applied and in the screening pipeline — about to have or has had an AI screening call.",
        smsDefault:
            "Answer questions about the process, set expectations (screening call incoming), keep them warm. After the call: answer follow-up questions, reassure concerns, let them know next steps.",
        emailDefault:
            "Answer questions about the role and encourage them to complete their screening call. After the call: help with questions about the interview process, timing, or role.",
    },
    {
        key: "APPOINTMENT",
        label: "Appointment",
        description: "Booked for an interview.",
        smsDefault:
            "Confirm attendance, answer logistics (location, format), direct reschedule requests to the link. Acknowledge positive replies warmly — don't treat them as action items.",
        emailDefault:
            "Confirm they're coming, help with questions. If they want to reschedule, send the reschedule link — do not suggest specific dates.",
    },
    {
        key: "FORM",
        label: "Form",
        description: "Sent the post-interview questionnaire.",
        smsDefault:
            "Encourage them to complete the form, answer questions about it, explain what happens after submission.",
        emailDefault:
            "Encourage completion, answer questions about the form, explain what happens after they submit.",
    },
    {
        key: "CLOSE",
        label: "Close",
        description: "Final assessment stage — closing call happening or imminent. NOT yet booked to start.",
        smsDefault:
            "Answer questions about the role, process, or what happens next. Keep them engaged and positive. They are NOT yet booked to start.",
        emailDefault:
            "Answer questions about the role, process, or next steps. Keep them engaged and positive. They are NOT yet booked to start.",
    },
    {
        key: "TRAINING",
        label: "Training",
        description: "Assessed, booked to start, given a confirmed start date.",
        smsDefault:
            "Get them to confirm attendance (reply YES). Handle concerns, answer logistics. If they can't attend, the ONLY alternative is the following Monday at the same time — no other days.",
        emailDefault:
            "Confirm attendance, handle last-minute concerns, answer logistics. If they can't attend, the ONLY alternative is the following Monday at the same time — no other days.",
    },
];

export default function AiStagePromptsSection({ settings, pipelineId, onSave }) {
    const [form, setForm] = useState({});
    const [busy, setBusy] = useState(false);

    useEffect(() => {
        const raw = (settings?.ai_stage_prompts) || {};
        const hydrated = {};
        for (const s of STAGES) {
            hydrated[s.key] = {
                sms: (raw[s.key]?.sms) || "",
                email: (raw[s.key]?.email) || "",
            };
        }
        setForm(hydrated);
    }, [settings]);

    const upd = (stage, channel, val) =>
        setForm((f) => ({ ...f, [stage]: { ...f[stage], [channel]: val } }));

    const save = useCallback(async () => {
        setBusy(true);
        try {
            const params = pipelineId ? { pipeline_id: pipelineId } : {};
            await api.put("/settings/ai-stage-prompts", { ai_stage_prompts: form }, { params });
            toast.success("AI stage prompts saved");
            if (onSave) onSave();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Save failed");
        } finally {
            setBusy(false);
        }
    }, [form, pipelineId, onSave]);

    return (
        <div className="max-w-3xl space-y-2">
            <div className="mb-6">
                <h2 className="font-heading text-xl font-semibold flex items-center gap-2">
                    <Robot size={18} weight="duotone" className="text-brand-primary" />
                    AI Stage Prompts
                </h2>
                <p className="text-xs text-ink-muted mt-1">
                    Customise what the AI is told to do at each pipeline stage — for both SMS replies and
                    email replies. Leave a field blank to use the built-in default (shown as placeholder).
                    Defaults are always active after a fresh deploy — no re-save needed.
                </p>
            </div>

            {STAGES.map((stage) => (
                <div key={stage.key} className="surface rounded-xl overflow-hidden">
                    <div className="px-5 py-4 border-b border-strokes bg-surface-hover">
                        <div className="flex items-center gap-2">
                            <span className="text-xs font-bold uppercase tracking-widest text-brand-primary">
                                {stage.label}
                            </span>
                            <span className="text-[11px] text-ink-muted">— {stage.description}</span>
                        </div>
                    </div>
                    <div className="p-5 grid grid-cols-1 gap-4 sm:grid-cols-2">
                        <div>
                            <label className="flex items-center gap-1.5 text-xs font-semibold text-ink mb-2">
                                <ChatTeardrop size={12} weight="duotone" className="text-brand-primary" />
                                SMS Agent Instructions
                            </label>
                            <textarea
                                rows={5}
                                className="input-dark resize-y text-xs leading-relaxed w-full"
                                placeholder={stage.smsDefault}
                                value={form[stage.key]?.sms || ""}
                                onChange={(e) => upd(stage.key, "sms", e.target.value)}
                            />
                            <p className="text-[10px] text-ink-muted mt-1">
                                Placeholder = active default when blank.
                            </p>
                        </div>
                        <div>
                            <label className="flex items-center gap-1.5 text-xs font-semibold text-ink mb-2">
                                <EnvelopeSimple size={12} weight="duotone" className="text-brand-primary" />
                                Email Reply Instructions
                            </label>
                            <textarea
                                rows={5}
                                className="input-dark resize-y text-xs leading-relaxed w-full"
                                placeholder={stage.emailDefault}
                                value={form[stage.key]?.email || ""}
                                onChange={(e) => upd(stage.key, "email", e.target.value)}
                            />
                            <p className="text-[10px] text-ink-muted mt-1">
                                Placeholder = active default when blank.
                            </p>
                        </div>
                    </div>
                </div>
            ))}

            <div className="pt-2 flex items-center gap-3">
                <button
                    onClick={save}
                    disabled={busy}
                    className="btn-primary flex items-center gap-1.5"
                >
                    <Robot size={12} weight="bold" className={busy ? "animate-spin" : ""} />
                    {busy ? "Saving…" : "Save AI Stage Prompts"}
                </button>
                <span className="text-[11px] text-ink-muted">Takes effect on the next SMS or email reply.</span>
            </div>
        </div>
    );
}
