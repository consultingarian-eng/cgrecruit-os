import { useState, useRef, useEffect } from "react";
import api from "@/lib/api";
import { toast } from "sonner";
import { Switch } from "@/components/ui/switch";
import { PhoneOutgoing, Robot, ArrowClockwise, WarningCircle, CheckCircle, Eye, X } from "@phosphor-icons/react";

const FIELD = ({ label, desc, children }) => (
    <div className="flex items-start justify-between gap-6 py-3 border-b border-strokes last:border-0">
        <div className="flex-1 min-w-0">
            <div className="text-sm font-medium text-ink">{label}</div>
            {desc && <div className="text-xs text-ink-muted mt-0.5 leading-relaxed">{desc}</div>}
        </div>
        <div className="shrink-0">{children}</div>
    </div>
);

const NumInput = ({ value, onChange, min = 1, max = 99 }) => (
    <input
        type="number"
        min={min}
        max={max}
        value={value}
        onChange={(e) => onChange(Math.max(min, Math.min(max, Number(e.target.value))))}
        className="w-20 bg-surface-active border border-strokes rounded px-2 py-1 text-sm text-center text-ink focus:outline-none focus:border-brand-primary"
    />
);

const DEFAULTS = {
    enabled: false,
    days_after_appointment: 7,
    max_attempts: 3,
    attempt_gap_hours: 24,
    lookback_days: 60,
    daily_cap: 15,
    opener_line: "",
    extra_instructions: "",
    followup_email_enabled: true,
};

const DEFAULT_OPENER =
    "Hi [First Name], this is [Agent Name] from [Company]. Just catching up regarding your " +
    "appointment with us on [Original Appointment Date] — it looks like you couldn't make it " +
    "and didn't reschedule. Are you still looking for work?";

export default function NoShowRevivalSection({ settings, onSaved, pipelineId }) {
    const initial = () => ({ ...DEFAULTS, ...(settings.no_show_revival || {}) });
    const [form, setForm] = useState(initial);
    const lastScopeRef = useRef(pipelineId);
    useEffect(() => {
        if (lastScopeRef.current !== pipelineId) {
            lastScopeRef.current = pipelineId;
            setForm({ ...DEFAULTS, ...(settings.no_show_revival || {}) });
            setPreview(null);
        }
    }, [pipelineId, settings]);

    const [busy, setBusy] = useState(false);
    const [syncing, setSyncing] = useState(false);
    const [agentInfo, setAgentInfo] = useState(null);
    const [preview, setPreview] = useState(null);
    const [previewing, setPreviewing] = useState(false);
    const [promptPreview, setPromptPreview] = useState(null);
    const [promptLoading, setPromptLoading] = useState(false);
    const upd = (k, v) => setForm((f) => ({ ...f, [k]: v }));

    const save = async () => {
        setBusy(true);
        try {
            const params = pipelineId ? { pipeline_id: pipelineId } : {};
            const r = await api.put("/settings/no-show-revival", form, { params });
            await onSaved?.();
            const resync = r.data?.agent_resync || [];
            const failed = resync.filter((x) => x.status !== "synced");
            if (resync.length === 0) {
                toast.success("Revival settings saved");
            } else if (failed.length) {
                toast.warning(`Saved, but ${failed.length} agent re-sync failed — use "Create & sync" below`);
            } else {
                toast.success(`Saved — ${resync.length} agent${resync.length > 1 ? "s" : ""} re-synced with the new script`);
            }
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to save");
        } finally { setBusy(false); }
    };

    const loadPromptPreview = async () => {
        setPromptLoading(true);
        try {
            const params = pipelineId ? { pipeline_id: pipelineId } : {};
            const r = await api.get("/revival/agent/preview-prompt", { params });
            setPromptPreview(r.data);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to load prompt");
        } finally { setPromptLoading(false); }
    };

    const syncAgent = async () => {
        setSyncing(true);
        setAgentInfo(null);
        try {
            const body = pipelineId ? { pipeline_id: pipelineId } : {};
            const r = await api.post("/revival/agent/sync", body);
            const rows = r.data?.results || [];
            setAgentInfo(rows);
            const failed = rows.filter((x) => x.status !== "synced");
            if (failed.length) {
                toast.error(`Agent sync: ${failed.length} failed — ${failed[0]?.error || "see details below"}`);
            } else {
                toast.success(`Revival agent${rows.length > 1 ? "s" : ""} ready${rows.some((x) => x.created) ? " (created new)" : ""}`);
            }
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Agent sync failed");
        } finally { setSyncing(false); }
    };

    const runPreview = async () => {
        if (!pipelineId) { toast.error("Select a pipeline first — preview is per office"); return; }
        setPreviewing(true);
        setPreview(null);
        try {
            const r = await api.get("/revival/preview", { params: { pipeline_id: pipelineId } });
            setPreview(r.data);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Preview failed");
        } finally { setPreviewing(false); }
    };

    return (
        <div className="space-y-6">
            <div>
                <h2 className="label-overline mb-0.5">No-Show Revival</h2>
                <p className="text-xs text-ink-muted leading-relaxed">
                    A dedicated AI agent calls archived no-show candidates a week or so after their missed
                    appointment — hears out what happened, and if they're still looking, offers new slots and
                    books them straight back onto the board. Candidates who reply STOP to texts are still
                    called and emailed; only SMS stays blocked for them.
                </p>
            </div>

            <div className="surface p-4 divide-y divide-strokes">
                <FIELD
                    label="Revival calling enabled"
                    desc="Master switch for this office. The hourly scheduler only queues calls while this is on — the manual 'Revival call now' button in the drawer works regardless."
                >
                    <Switch checked={!!form.enabled} onCheckedChange={(v) => upd("enabled", v)} data-testid="revival-enabled-switch" />
                </FIELD>
                <FIELD
                    label="Days after missed appointment"
                    desc="Wait this many days after the no-showed appointment before the first revival call."
                >
                    <NumInput value={form.days_after_appointment} onChange={(v) => upd("days_after_appointment", v)} min={1} max={60} />
                </FIELD>
                <FIELD
                    label="Max attempts per candidate"
                    desc="Total revival calls before giving up on a candidate. Someone who says 'not interested' is never called again regardless."
                >
                    <NumInput value={form.max_attempts} onChange={(v) => upd("max_attempts", v)} min={1} max={10} />
                </FIELD>
                <FIELD
                    label="Hours between attempts"
                    desc="Minimum gap before re-trying a candidate who didn't answer / hit voicemail."
                >
                    <NumInput value={form.attempt_gap_hours} onChange={(v) => upd("attempt_gap_hours", v)} min={1} max={168} />
                </FIELD>
                <FIELD
                    label="Lookback window (days)"
                    desc="Never call no-shows whose appointment is older than this — keeps the first-enable backlog sane and skips people who've long moved on."
                >
                    <NumInput value={form.lookback_days} onChange={(v) => upd("lookback_days", v)} min={7} max={365} />
                </FIELD>
                <FIELD
                    label="Daily call cap (this office)"
                    desc="Max revival calls per day, so the backlog trickles out without eating screening-call capacity. Calls also respect the auto-dialer call window and concurrency caps."
                >
                    <NumInput value={form.daily_cap} onChange={(v) => upd("daily_cap", v)} min={1} max={200} />
                </FIELD>
                <FIELD
                    label="Follow-up email after first missed call"
                    desc={`Once per candidate: when the first revival attempt ends in voicemail/no answer, email them the rebooking link. Edit the wording under Templates & Comms → "Revival Follow-Up".`}
                >
                    <Switch checked={form.followup_email_enabled !== false} onCheckedChange={(v) => upd("followup_email_enabled", v)} data-testid="revival-email-switch" />
                </FIELD>
            </div>

            <div>
                <h2 className="label-overline mb-0.5">Agent Script</h2>
                <p className="text-xs text-ink-muted leading-relaxed">
                    Customize what the agent says. Tokens work like email templates: [First Name], [Agent Name],
                    [Company], [Role], [Original Appointment Date], [Days Since Appointment]. Leave blank to use
                    the built-in script. Saving re-syncs the agent automatically.
                </p>
            </div>

            <div className="surface p-4 space-y-4">
                <div>
                    <label className="label-overline block mb-1.5">Opening line</label>
                    <textarea
                        value={form.opener_line || ""}
                        onChange={(e) => upd("opener_line", e.target.value)}
                        placeholder={DEFAULT_OPENER}
                        rows={3}
                        data-testid="revival-opener-input"
                        className="w-full bg-surface-active border border-strokes rounded px-3 py-2 text-xs text-ink leading-relaxed focus:outline-none focus:border-brand-primary placeholder:text-ink-dim"
                    />
                    <p className="text-[11px] text-ink-dim mt-1">The very first thing the agent says when the candidate picks up.</p>
                </div>
                <div>
                    <label className="label-overline block mb-1.5">Extra instructions <span className="text-ink-dim normal-case">(optional)</span></label>
                    <textarea
                        value={form.extra_instructions || ""}
                        onChange={(e) => upd("extra_instructions", e.target.value)}
                        placeholder={"e.g. If they mention transport issues, let them know the office is a 3-minute walk from the Green Line.\nNever discuss pay on this call — say the manager covers that at the interview."}
                        rows={4}
                        data-testid="revival-instructions-input"
                        className="w-full bg-surface-active border border-strokes rounded px-3 py-2 text-xs text-ink leading-relaxed focus:outline-none focus:border-brand-primary placeholder:text-ink-dim"
                    />
                    <p className="text-[11px] text-ink-dim mt-1">Appended to the agent's system prompt — use it for office-specific guidance, objection handling, or things to avoid.</p>
                </div>
                <button
                    onClick={loadPromptPreview}
                    disabled={promptLoading}
                    data-testid="revival-preview-prompt-btn"
                    className="btn-secondary inline-flex items-center gap-1.5 text-xs !py-1.5 !px-3"
                >
                    <Eye size={13} weight="bold" />
                    {promptLoading ? "Loading…" : "Preview full prompt"}
                </button>
                <p className="text-[11px] text-ink-dim">Shows the saved script — save first to preview unsent edits.</p>
            </div>

            <div>
                <h2 className="label-overline mb-0.5">Revival Agent</h2>
                <p className="text-xs text-ink-muted">
                    Each office gets its own dedicated ElevenLabs agent ("Olivia Revival — …") that shares the
                    office's existing phone number and booking tools. Create it once, re-sync after prompt/voice changes.
                </p>
            </div>

            <div className="surface p-4 space-y-3">
                <button
                    onClick={syncAgent}
                    disabled={syncing}
                    data-testid="revival-agent-sync-btn"
                    className="btn-secondary inline-flex items-center gap-1.5 text-xs !py-1.5 !px-3"
                >
                    <Robot size={13} weight="duotone" />
                    {syncing ? "Syncing…" : "Create & sync revival agent"}
                </button>
                {agentInfo && (
                    <ul className="space-y-1 text-xs">
                        {agentInfo.map((row) => (
                            <li key={row.pipeline_id} className="flex items-center gap-2">
                                {row.status === "synced"
                                    ? <CheckCircle size={13} weight="fill" className="text-emerald-400 shrink-0" />
                                    : <WarningCircle size={13} weight="fill" className="text-amber-400 shrink-0" />}
                                <span className="text-ink font-medium">{row.pipeline}</span>
                                {row.status === "synced" ? (
                                    <span className="text-ink-muted truncate">
                                        {row.created ? "created " : "synced "} · <code className="text-[10px]">{row.agent_id}</code>
                                    </span>
                                ) : (
                                    <span className="text-amber-400 truncate">{row.error}</span>
                                )}
                            </li>
                        ))}
                    </ul>
                )}
            </div>

            <div>
                <h2 className="label-overline mb-0.5">Preview</h2>
                <p className="text-xs text-ink-muted">
                    Dry-run of the next sweep — exactly who would be called with the saved settings. No calls are placed.
                </p>
            </div>

            <div className="surface p-4 space-y-3">
                <button
                    onClick={runPreview}
                    disabled={previewing || !pipelineId}
                    data-testid="revival-preview-btn"
                    className="btn-secondary inline-flex items-center gap-1.5 text-xs !py-1.5 !px-3"
                >
                    <PhoneOutgoing size={13} weight="duotone" />
                    {previewing ? "Loading…" : "Preview who'd be called"}
                </button>

                {preview && (
                    <div className="mt-2 space-y-2 text-xs">
                        <div className="flex items-center gap-2 text-ink-muted">
                            <span>
                                {preview.pipeline} · {preview.count} candidate{preview.count !== 1 ? "s" : ""} due
                                {!preview.revival_agent_id && " · ⚠ no revival agent yet"}
                                {!preview.enabled && " · scheduler currently off"}
                            </span>
                            <button onClick={runPreview} className="ml-auto text-ink-muted hover:text-ink">
                                <ArrowClockwise size={12} />
                            </button>
                        </div>
                        {preview.count === 0 ? (
                            <div className="text-ink-muted italic">
                                Nobody in the window right now — no-shows enter it {preview.settings?.days_after_appointment} days after their missed appointment.
                            </div>
                        ) : (
                            <ul className="divide-y divide-strokes border border-strokes rounded-md">
                                {preview.candidates.map((c) => (
                                    <li key={c.id} className="flex items-center gap-3 px-3 py-1.5">
                                        <span className="text-ink font-medium">{c.name || "—"}</span>
                                        <span className="text-ink-muted">{c.phone}</span>
                                        <span className="text-ink-muted ml-auto">
                                            missed {c.days_since_appointment != null ? `${c.days_since_appointment}d ago` : "—"}
                                            {c.revival_call_attempts > 0 && ` · ${c.revival_call_attempts} attempt${c.revival_call_attempts > 1 ? "s" : ""}`}
                                        </span>
                                    </li>
                                ))}
                            </ul>
                        )}
                    </div>
                )}
            </div>

            <div className="flex justify-end">
                <button onClick={save} disabled={busy} data-testid="revival-save-btn" className="btn-primary">
                    {busy ? "Saving…" : "Save revival settings"}
                </button>
            </div>

            {/* Full-prompt preview modal — mirrors the Screen Call Agent's */}
            {promptPreview && (
                <div
                    className="fixed inset-0 z-50 bg-black/70 backdrop-blur flex items-center justify-center p-4"
                    onClick={() => setPromptPreview(null)}
                    data-testid="revival-prompt-modal"
                >
                    <div
                        className="bg-[#0E0E11] border border-strokes rounded-lg max-w-3xl w-full max-h-[85vh] overflow-y-auto p-5 space-y-4"
                        onClick={(e) => e.stopPropagation()}
                    >
                        <div className="flex items-center justify-between">
                            <h3 className="font-heading text-lg">Revival agent — full script</h3>
                            <button onClick={() => setPromptPreview(null)} className="p-1 hover:bg-surface-hover rounded text-ink-muted">
                                <X size={16} />
                            </button>
                        </div>
                        <div>
                            <div className="label-overline mb-1.5">Opening line</div>
                            <pre className="surface p-3 text-xs font-mono whitespace-pre-wrap leading-relaxed">{promptPreview.first_message}</pre>
                        </div>
                        <div>
                            <div className="label-overline mb-1.5">Voicemail message</div>
                            <pre className="surface p-3 text-xs font-mono whitespace-pre-wrap leading-relaxed">{promptPreview.voicemail_message}</pre>
                        </div>
                        <div>
                            <div className="label-overline mb-1.5">System prompt ({promptPreview.system_prompt?.length || 0} chars)</div>
                            <pre className="surface p-3 text-xs font-mono whitespace-pre-wrap leading-relaxed">{promptPreview.system_prompt}</pre>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}
