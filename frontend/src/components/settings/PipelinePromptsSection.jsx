import { useEffect, useState } from "react";
import api from "@/lib/api";
import { toast } from "sonner";
import { usePipeline } from "@/lib/pipeline";
import { Stack, ArrowsClockwise, Microphone, Phone } from "@phosphor-icons/react";

/**
 * Recruiter-friendly per-pipeline editor — handles the THREE most common
 * customisations a non-admin recruiter actually needs day-to-day:
 *
 *   1. Agent name + opening line + location-specific context (prompts)
 *   2. Caller ID (the phone number candidates see when this pipeline dials)
 *
 * Anything deeper (calling_mode, ElevenLabs agent_id_override, voice_id,
 * LLM model) lives in the dedicated Screen Call Agent section, which is
 * also pipeline-scoped now but exposes the heavier dials.
 *
 * Saves trigger an automatic ElevenLabs agent sync server-side so changes
 * land on the next call without a separate "Sync" click.
 */
export default function PipelinePromptsSection() {
    const { pipelines, activePipelineId } = usePipeline();
    const [active, setActive] = useState(null);
    const [draft, setDraft] = useState({});
    const [twilioNumbers, setTwilioNumbers] = useState([]);
    const [verifiedIds, setVerifiedIds] = useState([]);
    const [busy, setBusy] = useState(false);
    const [loading, setLoading] = useState(true);

    // Caller-ID dropdowns need both Twilio-owned + verified BYO numbers.
    // These are tenant-wide read-only lists (no per-pipeline filtering).
    useEffect(() => {
        api.get("/twilio/phone-numbers")
            .then((r) => setTwilioNumbers(Array.isArray(r.data) ? r.data : []))
            .catch(() => setTwilioNumbers([]));
        api.get("/twilio/caller-ids")
            .then((r) => setVerifiedIds(Array.isArray(r.data) ? r.data : []))
            .catch(() => setVerifiedIds([]));
    }, []);

    // Hydrate the editor from the live pipeline doc whenever the top-nav
    // pipeline switcher changes. Re-fetching here (rather than reading from
    // the context cache) ensures we always see the latest overrides.
    useEffect(() => {
        let cancelled = false;
        const load = async () => {
            if (!activePipelineId) {
                setActive(null); setLoading(false); return;
            }
            setLoading(true);
            try {
                const r = await api.get("/pipelines");
                if (cancelled) return;
                const found = (r.data || []).find((p) => p.id === activePipelineId);
                setActive(found || null);
                setDraft({
                    agent_name_override: found?.agent_name_override || "",
                    first_message_override: found?.first_message_override || "",
                    additional_context_override: found?.additional_context_override || "",
                    twilio_phone_number: found?.twilio_phone_number || "",
                });
            } catch {
                if (!cancelled) toast.error("Failed to load pipeline");
            } finally {
                if (!cancelled) setLoading(false);
            }
        };
        load();
        return () => { cancelled = true; };
    }, [activePipelineId]);

    const save = async () => {
        if (!active) return;
        setBusy(true);
        try {
            // Send ONLY the prompt-override fields this form actually owns —
            // the prior `{...active, ...draft}` spread also pushed the
            // snapshot's availability_rules/blackouts back to the server,
            // silently overwriting any slot edits made elsewhere between
            // mount and save. Backend's $set leaves omitted fields alone.
            const body = {};
            const PROMPT_FIELDS = [
                "agent_name_override",
                "first_message_override",
                "additional_context_override",
                "voice_id_override",
                "twilio_phone_number",
            ];
            for (const k of PROMPT_FIELDS) {
                if (k in draft) body[k] = draft[k] ?? "";
            }
            await api.put(`/pipelines/${active.id}`, body);
            toast.success(`${active.name} prompt saved & pushed to ElevenLabs`);
            // Re-fetch so the local copy reflects backend-side normalisation
            const r = await api.get("/pipelines");
            const found = (r.data || []).find((p) => p.id === active.id);
            if (found) setActive(found);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Save failed");
        } finally { setBusy(false); }
    };

    if (loading) return <div className="text-sm text-ink-muted" data-testid="prompts-loading">Loading…</div>;
    if (!pipelines.length) {
        return (
            <div className="surface p-12 text-center max-w-md" data-testid="no-pipelines">
                <Stack size={28} weight="duotone" className="mx-auto mb-2 text-ink-muted" />
                <div className="font-heading text-base mb-1">No pipelines</div>
                <div className="text-sm text-ink-muted">Ask your administrator to assign you to a pipeline.</div>
            </div>
        );
    }
    if (!active) {
        return (
            <div className="surface p-12 text-center max-w-md" data-testid="no-active-pipeline">
                <Stack size={28} weight="duotone" className="mx-auto mb-2 text-ink-muted" />
                <div className="font-heading text-base mb-1">Pick an office</div>
                <div className="text-sm text-ink-muted">Switch office in the top nav to choose whose prompts to edit.</div>
            </div>
        );
    }

    return (
        <div className="max-w-3xl" data-testid="pipeline-prompts-section">
            <div className="mb-5">
                <h2 className="font-heading text-xl font-semibold flex items-center gap-2">
                    <Microphone size={18} weight="duotone" className="text-brand-primary" />
                    Office AI Voice &amp; Prompts
                </h2>
                <p className="text-xs text-ink-muted mt-0.5">
                    Editing prompts for <span className="text-ink font-medium">{active.name}</span>.
                    Switch office in the top nav to edit a different one. Changes save and push to ElevenLabs automatically.
                </p>
            </div>

            <div className="space-y-5">
                <div>
                    <label className="label-overline mb-1.5 block">Agent name</label>
                    <input
                        type="text"
                        className="input-dark"
                        data-testid="prompt-agent-name"
                        placeholder="Olivia"
                        value={draft.agent_name_override || ""}
                        onChange={(e) => setDraft((d) => ({ ...d, agent_name_override: e.target.value }))}
                    />
                    <div className="text-[11px] text-ink-muted mt-1">
                        What the AI calls itself on screening calls. Leave blank to use the default name.
                    </div>
                </div>

                <div>
                    <label className="label-overline mb-1.5 block">Opening message</label>
                    <textarea
                        rows={3}
                        className="input-dark resize-none"
                        data-testid="prompt-opening-message"
                        placeholder="Hi, this is {{agent_name}} from {{company_name}}, calling about your {{role}} application. Is this {{full_name}}?"
                        value={draft.first_message_override || ""}
                        onChange={(e) => setDraft((d) => ({ ...d, first_message_override: e.target.value }))}
                    />
                    <div className="text-[11px] text-ink-muted mt-1">
                        The very first thing the AI says when the candidate picks up. You can use placeholders like
                        {" "}<code className="text-ink">{"{{agent_name}}"}</code>,{" "}
                        <code className="text-ink">{"{{full_name}}"}</code>,{" "}
                        <code className="text-ink">{"{{company_name}}"}</code>,{" "}
                        <code className="text-ink">{"{{role}}"}</code>.
                    </div>
                </div>

                <div>
                    <label className="label-overline mb-1.5 block">Pipeline-specific context</label>
                    <textarea
                        rows={10}
                        className="input-dark resize-y font-mono text-xs leading-relaxed"
                        data-testid="prompt-context"
                        placeholder={`Append office-specific context the AI should know.\n\nExamples:\n• "Office is downtown, walking distance from the central station."\n• "Most suitable for candidates who live within 45 minutes of the city centre."\n• "Hours: 10:00am–6:00pm, Mon–Fri."`}
                        value={draft.additional_context_override || ""}
                        onChange={(e) => setDraft((d) => ({ ...d, additional_context_override: e.target.value }))}
                    />
                    <div className="text-[11px] text-ink-muted mt-1">
                        Appended to the master prompt for {active.name}. Use this to add commute info,
                        scheduling specifics, hiring-manager names, or anything else the AI should mention.
                    </div>
                </div>

                <div>
                    <label className="label-overline mb-1.5 block flex items-center gap-1.5">
                        <Phone size={11} weight="duotone" className="text-brand-primary" />
                        Caller ID for outbound calls
                    </label>
                    <select
                        className="input-dark"
                        data-testid="prompt-caller-id-select"
                        value={draft.twilio_phone_number || ""}
                        onChange={(e) => setDraft((d) => ({ ...d, twilio_phone_number: e.target.value }))}
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
                        {/* TwiML Bridge mode is disabled — Verified BYO numbers can't
                            serve as caller IDs for Managed-mode ElevenLabs calls. */}
                    </select>
                    {draft.twilio_phone_number && (
                        <div className="text-[11px] mt-1.5 px-2 py-1 rounded bg-[rgba(139,92,246,0.08)] border border-[rgba(139,92,246,0.2)] text-[#A78BFA]" data-testid="prompt-caller-id-readout">
                            Candidates dialed by <strong className="text-ink">{active.name}</strong> will see{" "}
                            <code className="font-mono text-ink">{draft.twilio_phone_number}</code> on their phone.
                        </div>
                    )}
                </div>

                <div className="flex items-center gap-2 pt-2 border-t border-strokes">
                    <button
                        onClick={save}
                        disabled={busy}
                        data-testid="save-prompt-btn"
                        className="btn-primary flex items-center gap-1.5"
                    >
                        <ArrowsClockwise size={12} weight="bold" className={busy ? "animate-spin" : ""} />
                        {busy ? "Saving & syncing…" : `Save ${active.name}`}
                    </button>
                    <span className="text-[11px] text-ink-muted">
                        Changes go live on the next outbound call.
                    </span>
                </div>

                <TestCallPanel pipeline={active} />
            </div>
        </div>
    );
}

/**
 * "Place a test call" panel — lets admins/recruiters dial their own number
 * with the pipeline's agent so they can audit Olivia's voice + opening line +
 * booking flow before a real candidate hears it.
 *
 * Stateless on the backend — `POST /api/elevenlabs/agent/test-call` doesn't
 * create a candidate, conversation row, or kanban entry. The transcript
 * still lives in the ElevenLabs dashboard if you want to review post-hangup.
 */
function TestCallPanel({ pipeline }) {
    const [open, setOpen] = useState(false);
    const [phone, setPhone] = useState(() => localStorage.getItem("cgrecruit_test_call_phone") || "");
    const [firstName, setFirstName] = useState("Test");
    const [busy, setBusy] = useState(false);
    const [lastResult, setLastResult] = useState(null);

    const dial = async () => {
        if (!phone || phone.replace(/\D/g, "").length < 10) {
            toast.warning("Enter a phone number with country code (e.g. +15550100199)");
            return;
        }
        localStorage.setItem("cgrecruit_test_call_phone", phone);
        setBusy(true);
        setLastResult(null);
        try {
            const r = await api.post("/elevenlabs/agent/test-call", {
                pipeline_id: pipeline.id,
                to_phone: phone,
                first_name: firstName || "Test",
            });
            const data = r.data || {};
            setLastResult(data);
            if (data.status === "initiated") {
                toast.success(`📞 Test call placed — your phone should ring in a moment`);
            } else {
                toast.error(`Call not initiated: ${data.error || data.status}`);
            }
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Test call failed");
        } finally { setBusy(false); }
    };

    return (
        <div className="border border-strokes rounded-md mt-1" data-testid="test-call-panel">
            <button
                onClick={() => setOpen((v) => !v)}
                className="w-full px-4 py-3 flex items-center justify-between text-left hover:bg-surface-hover transition-colors rounded-md"
                data-testid="test-call-toggle"
            >
                <div className="flex items-center gap-2.5">
                    <Phone size={14} weight="duotone" className="text-brand-primary" />
                    <div>
                        <div className="font-heading text-sm font-semibold">Place a test call</div>
                        <div className="text-[11px] text-ink-muted">
                            Dial your own phone with {pipeline.name}&apos;s agent to audit the prompt + voice + booking
                        </div>
                    </div>
                </div>
                <span className="text-ink-muted text-xs">{open ? "▾" : "▸"}</span>
            </button>
            {open && (
                <div className="border-t border-strokes p-4 space-y-3">
                    <div className="grid grid-cols-3 gap-3">
                        <div className="col-span-2">
                            <label className="label-overline block mb-1">Your phone (E.164)</label>
                            <input
                                data-testid="test-call-phone-input"
                                className="input-dark font-mono"
                                placeholder="+15550100199"
                                value={phone}
                                onChange={(e) => setPhone(e.target.value)}
                            />
                            <div className="text-[10px] text-ink-muted mt-1">
                                Include country code (+1 for US). Saved locally for next time.
                            </div>
                        </div>
                        <div>
                            <label className="label-overline block mb-1">Address you as</label>
                            <input
                                data-testid="test-call-firstname-input"
                                className="input-dark"
                                placeholder="Test"
                                value={firstName}
                                onChange={(e) => setFirstName(e.target.value)}
                            />
                        </div>
                    </div>
                    <button
                        onClick={dial}
                        disabled={busy}
                        data-testid="test-call-dial-btn"
                        className="btn-primary !text-xs flex items-center gap-1.5"
                    >
                        <Phone size={11} weight="bold" />
                        {busy ? "Placing call…" : `Dial ${pipeline.name} agent`}
                    </button>
                    {lastResult && lastResult.status === "initiated" && (
                        <div
                            data-testid="test-call-success"
                            className="text-[11px] px-3 py-2 rounded bg-[rgba(34,197,94,0.10)] border border-emerald-500/30 text-emerald-300"
                        >
                            ✅ Call placed via <code className="font-mono text-emerald-200">{lastResult.agent_name}</code> →{" "}
                            <code className="font-mono">{lastResult.to_phone}</code>
                            <div className="text-[10px] mt-0.5 text-ink-muted">
                                EL conversation ID: <code className="font-mono">{lastResult.conversation_id || "—"}</code>
                                {" · "}
                                Twilio call SID: <code className="font-mono">{lastResult.call_sid || "—"}</code>
                            </div>
                            <div className="text-[10px] mt-1 text-ink-muted">
                                After hanging up, the full transcript is in your ElevenLabs dashboard.
                            </div>
                        </div>
                    )}
                    {lastResult && lastResult.status !== "initiated" && (
                        <div
                            data-testid="test-call-error"
                            className="text-[11px] px-3 py-2 rounded bg-[rgba(244,63,94,0.10)] border border-rose-500/30 text-rose-300"
                        >
                            ❌ {lastResult.error || lastResult.status}
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}
