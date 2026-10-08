import { useState, useEffect, useRef } from "react";
import api from "@/lib/api";
import { toast } from "sonner";
import { confirmDialog } from "@/components/ConfirmDialog";
import { ArrowCounterClockwise } from "@phosphor-icons/react";

const FIELD_META = [
    {
        key: "monday_start",
        label: "Day 1 Start Time",
        placeholder: "e.g. 1:00 PM",
        desc: "Start time for the first training day (usually Monday).",
        group: "schedule",
    },
    {
        key: "monday_end",
        label: "Day 1 End Time",
        placeholder: "e.g. 3:00 PM",
        desc: "End time for the first training day.",
        group: "schedule",
    },
    {
        key: "tuesday_start",
        label: "Day 2 Start Time",
        placeholder: "e.g. 11:00 AM",
        desc: "Start time for the second training day (usually Tuesday).",
        group: "schedule",
    },
    {
        key: "tuesday_end",
        label: "Day 2 End Time",
        placeholder: "e.g. 3:00 PM",
        desc: "End time for the second training day.",
        group: "schedule",
    },
    {
        key: "regular_schedule",
        label: "Regular Schedule",
        placeholder: "e.g. 10:30 AM – 8:00 PM",
        desc: "The ongoing daily schedule after training week.",
        group: "schedule",
    },
    {
        key: "office_address",
        label: "Office Address",
        placeholder: "e.g. 100 Example Street, Example City, EX 00000",
        desc: "Shown on the start date card in the email and used as [Address] in the confirmation SMS.",
        group: "details",
    },
    {
        key: "dress_code",
        label: "Dress Code",
        placeholder: "e.g. Business Smart",
        desc: "What to wear for training.",
        group: "details",
    },
    {
        key: "id_text",
        label: "ID / Documents",
        placeholder: "Please bring a form of ID…",
        desc: "What to bring for identity verification.",
        group: "details",
        multiline: true,
    },
    {
        key: "research_text",
        label: "Pre-Training Research",
        placeholder: "You'll learn about our clients during training…",
        desc: "Research candidates should do before Day 1.",
        group: "details",
        multiline: true,
    },
    {
        key: "food_text",
        label: "Food & Refreshments",
        placeholder: "Snacks, refreshments and lunch will be provided…",
        desc: "Food arrangements during training.",
        group: "details",
        multiline: true,
    },
    {
        key: "parking_text",
        label: "Parking",
        placeholder: "No free parking on site…",
        desc: "Parking / transport instructions.",
        group: "details",
        multiline: true,
    },
    {
        key: "custom_notes",
        label: "Custom Notes",
        placeholder: "Any additional information for this cohort…",
        desc: "Optional extra notes appended to the email.",
        group: "details",
        multiline: true,
    },
    {
        key: "confirmation_sms_body",
        label: "Confirmation SMS Body",
        placeholder: "Hi [First Name]! 👋 Looking forward to seeing you at the [Company] office today at [Start Time] for your first day orientation. Address: [Address]. Reply YES to confirm. Reply STOP to opt out.",
        desc: "The SMS sent ~3 hours before their start time. Variables: [First Name], [Company], [Start Date], [Start Time], [Address]. Leave blank for the default message.",
        group: "sms",
        multiline: true,
    },
    {
        key: "confirmation_sms_lead_hours",
        label: "SMS Lead Time (hours)",
        placeholder: "3",
        desc: "How many hours before the start time to send the confirmation SMS. Default is 3.",
        group: "sms",
    },
    {
        key: "confirmation_sms_instructions",
        label: "AI SMS Agent Instructions",
        placeholder: "e.g. Orientation is every Monday at 1pm at 100 Example Street. Parking is free on site. Contact Sam on 555-0100 if they have questions.",
        desc: "Custom instructions injected into the AI's system prompt. Use this to tell the AI about your office policies, address, contact, and anything specific to this pipeline. The AI uses these alongside the candidate's full context (stage, screening call, form responses) to reply at every point in the pipeline.",
        group: "sms",
        multiline: true,
        rows: 6,
    },
];

const EMPTY = Object.fromEntries(FIELD_META.map((f) => [f.key, ""]));

export default function StarterEmailSection({ pipelineId }) {
    const [form, setForm] = useState(EMPTY);
    const [loading, setLoading] = useState(true);
    const [busy, setBusy] = useState(false);
    const [resetting, setResetting] = useState(false);
    const lastPipelineRef = useRef(pipelineId);

    const load = async (pid) => {
        setLoading(true);
        try {
            const params = pid ? { pipeline_id: pid } : {};
            const r = await api.get("/settings/starter-template", { params });
            setForm({ ...EMPTY, ...r.data });
        } catch {
            toast.error("Could not load starter email template");
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        load(pipelineId);
    }, []);

    useEffect(() => {
        if (lastPipelineRef.current !== pipelineId) {
            lastPipelineRef.current = pipelineId;
            load(pipelineId);
        }
    }, [pipelineId]);

    const upd = (k, v) => setForm((f) => ({ ...f, [k]: v }));

    const save = async () => {
        setBusy(true);
        try {
            const params = pipelineId ? { pipeline_id: pipelineId } : {};
            await api.put("/settings/starter-template", form, { params });
            toast.success("Starter email template saved");
        } catch {
            toast.error("Could not save template");
        } finally {
            setBusy(false);
        }
    };

    const resetToDefaults = async () => {
        if (!(await confirmDialog({ title: "Reset starter email template?", description: "This pipeline's starter email returns to office defaults. This cannot be undone.", confirmLabel: "Reset", destructive: true }))) return;
        setResetting(true);
        try {
            const params = pipelineId ? { pipeline_id: pipelineId } : {};
            const r = await api.post("/settings/starter-template/reset", {}, { params });
            setForm({ ...EMPTY, ...r.data });
            toast.success("Reset to defaults");
        } catch {
            toast.error("Could not reset template");
        } finally {
            setResetting(false);
        }
    };

    const inputClass = "w-full bg-surface-active border border-strokes rounded px-3 py-2 text-sm text-ink placeholder-ink-muted focus:outline-none focus:border-brand-primary resize-none";

    if (loading) return <div className="text-sm text-ink-muted p-2">Loading template…</div>;

    const scheduleFields = FIELD_META.filter((f) => f.group === "schedule");
    const detailFields   = FIELD_META.filter((f) => f.group === "details");
    const smsFields      = FIELD_META.filter((f) => f.group === "sms");

    return (
        <div className="space-y-8 max-w-2xl">
            <div>
                <h2 className="font-heading text-xl font-semibold">Starter Email Template</h2>
                <p className="text-sm text-ink-muted mt-1 leading-relaxed">
                    These values populate the welcome email sent when a candidate is moved to Training.
                    {pipelineId
                        ? " Editing overrides for this pipeline only."
                        : " Editing the global defaults (all pipelines inherit these unless overridden)."}
                </p>
            </div>

            {/* Schedule section */}
            <section className="space-y-4">
                <h3 className="text-xs font-bold uppercase tracking-widest text-ink-muted">Training Schedule</h3>
                <div className="grid grid-cols-2 gap-4">
                    {scheduleFields.map((f) => (
                        <div key={f.key}>
                            <label className="block text-sm font-medium text-ink mb-1">{f.label}</label>
                            <input
                                className={inputClass}
                                value={form[f.key]}
                                onChange={(e) => upd(f.key, e.target.value)}
                                placeholder={f.placeholder}
                            />
                            <p className="text-[11px] text-ink-muted mt-0.5">{f.desc}</p>
                        </div>
                    ))}
                </div>
            </section>

            {/* Details section */}
            <section className="space-y-4">
                <h3 className="text-xs font-bold uppercase tracking-widest text-ink-muted">Email Details</h3>
                <div className="space-y-4">
                    {detailFields.map((f) => (
                        <div key={f.key}>
                            <label className="block text-sm font-medium text-ink mb-1">{f.label}</label>
                            {f.multiline ? (
                                <textarea
                                    className={inputClass}
                                    rows={3}
                                    value={form[f.key]}
                                    onChange={(e) => upd(f.key, e.target.value)}
                                    placeholder={f.placeholder}
                                />
                            ) : (
                                <input
                                    className={inputClass}
                                    value={form[f.key]}
                                    onChange={(e) => upd(f.key, e.target.value)}
                                    placeholder={f.placeholder}
                                />
                            )}
                            <p className="text-[11px] text-ink-muted mt-0.5">{f.desc}</p>
                        </div>
                    ))}
                </div>
            </section>

            {/* Confirmation SMS section */}
            <section className="space-y-4">
                <div>
                    <h3 className="text-xs font-bold uppercase tracking-widest text-ink-muted">Confirmation SMS</h3>
                    <p className="text-[11px] text-ink-muted mt-1">Sent automatically before their start time via Twilio. Replies are handled by the AI assistant.</p>
                </div>
                <div className="space-y-4">
                    {smsFields.map((f) => (
                        <div key={f.key}>
                            <label className="block text-sm font-medium text-ink mb-1">{f.label}</label>
                            {f.multiline ? (
                                <textarea
                                    className={inputClass}
                                    rows={f.rows || 4}
                                    value={form[f.key] || ""}
                                    onChange={(e) => upd(f.key, e.target.value)}
                                    placeholder={f.placeholder}
                                />
                            ) : (
                                <input
                                    className={inputClass}
                                    type="number"
                                    min="1"
                                    max="24"
                                    step="0.5"
                                    value={form[f.key] || ""}
                                    onChange={(e) => upd(f.key, e.target.value)}
                                    placeholder={f.placeholder}
                                />
                            )}
                            <p className="text-[11px] text-ink-muted mt-0.5">{f.desc}</p>
                        </div>
                    ))}
                </div>
            </section>

            {/* Actions */}
            <div className="flex items-center gap-3 pt-2 border-t border-strokes">
                <button onClick={save} disabled={busy} className="btn-primary text-sm px-5 py-2 disabled:opacity-50">
                    {busy ? "Saving…" : "Save Template"}
                </button>
                <button
                    onClick={resetToDefaults}
                    disabled={resetting}
                    className="flex items-center gap-1.5 text-sm text-ink-muted hover:text-ink disabled:opacity-50 transition-colors"
                >
                    <ArrowCounterClockwise size={13} weight="bold" />
                    {resetting ? "Resetting…" : "Reset to office defaults"}
                </button>
            </div>
        </div>
    );
}
