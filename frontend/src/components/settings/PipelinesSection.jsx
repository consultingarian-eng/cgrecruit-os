import { useEffect, useState } from "react";
import api from "@/lib/api";
import { toast } from "sonner";
import { usePipeline } from "@/lib/pipeline";
import { CloudArrowUp, Copy, Plus, Trash, MagicWand } from "@phosphor-icons/react";

/**
 * Pipeline editor — edits whichever pipeline is currently selected in the
 * top-nav switcher. Switching pipelines from the header automatically loads
 * that pipeline's overrides.
 *
 * Tenant-wide actions (Create new pipeline, Auto-detect from ElevenLabs)
 * remain in the header; they're CRUD on the pipeline collection itself, not
 * per-pipeline editing.
 */
export default function PipelinesSection() {
    const { pipelines, activePipelineId, setActive: setActivePipelineId, refresh } = usePipeline();
    const [active, setActive] = useState(null);
    const [voices, setVoices] = useState([]);
    const [twilioNumbers, setTwilioNumbers] = useState([]);
    const [verifiedIds, setVerifiedIds] = useState([]);
    const [busy, setBusy] = useState(false);
    // Offices come from backend/company_profile.json.
    const [offices, setOffices] = useState([]);
    useEffect(() => {
        api.get("/company-profile/offices").then((r) => setOffices(r.data?.offices || [])).catch(() => {});
    }, []);

    // Hydrate `active` whenever the top-nav pipeline switcher changes (or the
    // pipelines list refreshes after a create/save).
    useEffect(() => {
        if (!activePipelineId) { setActive(null); return; }
        const found = pipelines.find((p) => p.id === activePipelineId);
        setActive(found || null);
    }, [activePipelineId, pipelines]);

    useEffect(() => {
        api.get("/elevenlabs/voices").then((r) => setVoices(r.data?.voices || [])).catch(() => {});
        api.get("/twilio/phone-numbers").then((r) => {
            if (Array.isArray(r.data)) setTwilioNumbers(r.data);
        }).catch(() => {});
        api.get("/twilio/caller-ids").then((r) => {
            if (Array.isArray(r.data)) setVerifiedIds(r.data);
        }).catch(() => {});
    }, []);

    const upd = (k, v) => setActive((prev) => ({ ...(prev || {}), [k]: v }));

    const save = async () => {
        if (!active) return;
        setBusy(true);
        try {
            // IMPORTANT — only send fields THIS form owns. Spreading
            // `availability_rules` / `availability_blackouts` /
            // `appointment_duration_minutes` from the stale `active` snapshot
            // would silently overwrite slots the user (or another tab) just
            // edited in the AvailabilityManager. Slots are ONLY edited there.
            const payload = {
                name: active.name,
                description: active.description || "",
                twilio_phone_number: active.twilio_phone_number || "",
                public_slug: active.public_slug || "",
elevenlabs_agent_id_override: active.elevenlabs_agent_id_override || "",
                elevenlabs_phone_number_id_override: active.elevenlabs_phone_number_id_override || "",
                voice_id_override: active.voice_id_override || "",
                additional_context_override: active.additional_context_override || "",
                first_message_override: active.first_message_override || "",
                agent_name_override: active.agent_name_override || "",
                cg1_office_key: active.cg1_office_key || "",
            };
            await api.put(`/pipelines/${active.id}`, payload);
            await refresh();
            toast.success(`${active.name} saved — applies to next call from this pipeline`);
        } catch (e) {
            toast.error("Failed to save");
        } finally { setBusy(false); }
    };

    const create = async () => {
        const name = prompt("New pipeline name (e.g., 'Riverside')");
        if (!name) return;
        try {
            const r = await api.post("/pipelines", { name });
            await refresh();
            // Switch the top nav (and thus this editor) to the freshly-created pipeline.
            if (r.data?.id) setActivePipelineId(r.data.id);
            toast.success(`Pipeline "${name}" created`);
        } catch { toast.error("Failed"); }
    };

    const remove = async () => {
        if (!active) return;
        if (!confirm(`Delete "${active.name}"? Candidates and jobs in this pipeline will also be deleted.`)) return;
        try {
            await api.delete(`/pipelines/${active.id}`);
            await refresh();
            // The top nav will auto-fall-back to the first remaining pipeline.
            toast.success("Pipeline deleted");
        } catch { toast.error("Failed"); }
    };

    const copyApplyLink = () => {
        if (!active?.public_slug) return;
        const url = `${window.location.origin}/apply/${active.public_slug}`;
        navigator.clipboard.writeText(url);
        toast.success("Apply link copied");
    };

    const autoLink = async () => {
        setBusy(true);
        try {
            const r = await api.post("/elevenlabs/auto-link-pipelines");
            const linked = r.data?.linked || [];
            const skipped = r.data?.skipped || [];
            await refresh();
            if (linked.length > 0) {
                toast.success(`Auto-linked ${linked.length} pipeline${linked.length > 1 ? "s" : ""} to ElevenLabs (${skipped.length} skipped)`);
            } else {
                toast.warning("No pipelines could be auto-linked. Check Twilio number imports in ElevenLabs.");
            }
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Auto-link failed");
        } finally { setBusy(false); }
    };

    return (
        <div className="max-w-3xl space-y-6" data-testid="pipelines-section">
            <div className="flex items-start justify-between gap-4 flex-wrap">
                <div>
                    <h2 className="font-heading text-2xl font-bold tracking-tight">Offices &amp; Variants</h2>
                    <p className="text-sm text-ink-muted mt-1">
                        Editing infrastructure for{" "}
                        <span className="text-ink font-medium">
                            {active?.name || "(no office selected)"}
                        </span>
                        . Switch office in the top nav to edit a different one.
                    </p>
                </div>
                <div className="flex gap-2">
                    <button onClick={autoLink} disabled={busy} data-testid="auto-link-btn" className="btn-secondary !py-1.5 !px-3 text-xs flex items-center gap-1.5">
                        <MagicWand size={12} weight="bold" /> {busy ? "…" : "Auto-detect from ElevenLabs"}
                    </button>
                    <button onClick={create} data-testid="new-pipeline-btn" className="btn-primary !py-1.5 !px-3 text-xs flex items-center gap-1.5">
                        <Plus size={12} weight="bold" /> New pipeline
                    </button>
                </div>
            </div>

            {!active ? (
                <div className="surface p-8 text-center text-sm text-ink-muted" data-testid="no-active-pipeline">
                    {pipelines.length
                        ? "Switch office in the top nav to choose which one to configure."
                        : "No pipelines yet — click \"New pipeline\" to create your first office."}
                </div>
            ) : (
                <div className="space-y-5">
                    <div className="surface p-5 space-y-4">
                        <div className="flex items-center justify-between">
                            <div>
                                <div className="label-overline">Editing</div>
                                <div className="font-heading text-xl font-semibold">{active.name}</div>
                            </div>
                            <button onClick={remove} data-testid="delete-pipeline-btn" className="text-brand-danger p-1.5 hover:bg-surface-hover rounded">
                                <Trash size={14} />
                            </button>
                        </div>

                        <Field label="Pipeline Name">
                            <input data-testid="pipeline-name-input" className="input-dark" value={active.name || ""} onChange={(e) => upd("name", e.target.value)} />
                        </Field>
                        <Field label="Description">
                            <input data-testid="pipeline-description-input" className="input-dark" value={active.description || ""} onChange={(e) => upd("description", e.target.value)} />
                        </Field>
                        <Field label="Public slug (for /apply/{slug})">
                            <input data-testid="pipeline-slug-input" className="input-dark font-mono" value={active.public_slug || ""} onChange={(e) => upd("public_slug", e.target.value)} />
                        </Field>

                        <Field label="Caller ID for outbound calls">
                            <select
                                data-testid="pipeline-caller-id-select"
                                className="input-dark"
                                value={active.twilio_phone_number || ""}
                                onChange={(e) => upd("twilio_phone_number", e.target.value)}
                            >
                                <option value="">Inherit from global Caller ID setting</option>
                                {twilioNumbers.length > 0 && (
                                    <optgroup label="Twilio-owned (works in any calling mode)">
                                        {twilioNumbers.map((n) => (
                                            <option key={n.phone_number} value={n.phone_number}>
                                                {n.phone_number} {n.friendly_name && n.friendly_name !== n.phone_number ? `— ${n.friendly_name}` : ""}
                                            </option>
                                        ))}
                                    </optgroup>
                                )}
                            </select>
                            {active.twilio_phone_number && (
                                <div className="text-[11px] mt-1.5 px-2 py-1 rounded bg-[rgba(139,92,246,0.08)] border border-[rgba(139,92,246,0.2)] text-[#A78BFA] flex items-center gap-1.5" data-testid="active-caller-id-readout">
                                    Candidates dialed by <strong className="text-ink">{active.name}</strong> will see this number on their phone:
                                    <code className="font-mono text-ink">{active.twilio_phone_number}</code>
                                </div>
                            )}
                        </Field>

                        <button onClick={copyApplyLink} data-testid="pipeline-copy-apply-btn" className="btn-secondary !py-1.5 !px-3 text-xs flex items-center gap-1.5">
                            <Copy size={12} weight="bold" /> Copy public Apply link
                        </button>
                    </div>

                    <div className="surface p-5 space-y-4">
                        <div className="flex items-center justify-between">
                            <div>
                                <div className="font-heading text-base font-semibold">AI Agent Variant</div>
                                <div className="text-xs text-ink-muted mt-0.5">
                                    Overrides apply to outbound calls placed for candidates in this pipeline. Empty = inherit from Settings → Screen Call Agent.
                                </div>
                            </div>
                        </div>
                        <Field label="Agent Name Override" help="e.g., 'Olivia' for one office, 'Mia' for another. Leave blank to use the global agent name.">
                            <input data-testid="pipeline-agent-name-input" className="input-dark" placeholder="Inherit (Olivia)" value={active.agent_name_override || ""} onChange={(e) => upd("agent_name_override", e.target.value)} />
                        </Field>
                        <Field label="First Message Override" help="The opening line. Use [First Name], [Role], [Company] as placeholders. Leave blank to inherit.">
                            <textarea data-testid="pipeline-first-message-input" rows={2} className="input-dark" placeholder="Inherit from Settings" value={active.first_message_override || ""} onChange={(e) => upd("first_message_override", e.target.value)} />
                        </Field>
                        <Field label="Location-specific Context (FAQs)" help="Appended to the base prompt. Tell the agent about this pipeline's office location, commute, hiring manager, anything pipeline-specific.">
                            <textarea data-testid="pipeline-context-input" rows={6} className="input-dark font-mono text-xs" placeholder="e.g., Office: Downtown, two minutes from the central station..." value={active.additional_context_override || ""} onChange={(e) => upd("additional_context_override", e.target.value)} />
                        </Field>
                        <Field label="Voice Override" help="Use a different ElevenLabs voice for this pipeline. Leave blank to inherit.">
                            <select data-testid="pipeline-voice-select" className="input-dark" value={active.voice_id_override || ""} onChange={(e) => upd("voice_id_override", e.target.value)}>
                                <option value="">Inherit from Settings</option>
                                {voices.map((v) => <option key={v.voice_id} value={v.voice_id}>{v.name}</option>)}
                            </select>
                        </Field>
                        <div className="grid grid-cols-2 gap-3">
                            <Field label="ElevenLabs Agent ID Override (advanced)" help="Use a different agent entirely. Otherwise the global agent is used with overrides applied.">
                                <input data-testid="pipeline-agent-override-input" className="input-dark font-mono text-xs" placeholder="Inherit" value={active.elevenlabs_agent_id_override || ""} onChange={(e) => upd("elevenlabs_agent_id_override", e.target.value)} />
                            </Field>
                            <Field label="ElevenLabs Phone Number ID Override">
                                <input data-testid="pipeline-phone-override-input" className="input-dark font-mono text-xs" placeholder="Inherit" value={active.elevenlabs_phone_number_id_override || ""} onChange={(e) => upd("elevenlabs_phone_number_id_override", e.target.value)} />
                            </Field>
                        </div>
                    </div>

                    <div className="border-t border-strokes pt-4">
                        <div className="text-xs font-semibold text-ink uppercase tracking-widest mb-3">Office</div>
                        <Field label="Office" help="Which office this pipeline recruits for (offices are defined in backend/company_profile.json). Drives the office address, map links, SMS sender and starter-email defaults — and, if the CG1 field app is connected, the CG1 office new starters are added to.">
                            <select
                                className="input-dark"
                                value={active.cg1_office_key || ""}
                                onChange={(e) => upd("cg1_office_key", e.target.value)}
                            >
                                <option value="">Match by pipeline name</option>
                                {offices.map((o) => <option key={o.key} value={o.key}>{o.label}</option>)}
                            </select>
                        </Field>
                    </div>

                    <div className="rounded-md p-4 bg-[rgba(139,92,246,0.06)] border border-[rgba(139,92,246,0.2)] text-xs text-[#A78BFA] flex items-start gap-2">
                        <CloudArrowUp size={14} weight="bold" className="flex-shrink-0 mt-0.5" />
                        <div>
                            Saving re-syncs this office's agent to ElevenLabs; there is no per-call override.
                        </div>
                    </div>

                    <button onClick={save} disabled={busy} data-testid="save-pipeline-btn" className="btn-primary">
                        {busy ? "Saving…" : "Save pipeline"}
                    </button>
                </div>
            )}
        </div>
    );
}

function Field({ label, help, children }) {
    return (
        <div>
            <label className="label-overline block mb-1.5">{label}</label>
            {children}
            {help && <div className="text-xs text-ink-muted mt-1">{help}</div>}
        </div>
    );
}
