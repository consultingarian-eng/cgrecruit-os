import { useState, useEffect, useRef } from "react";
import api from "@/lib/api";
import { toast } from "sonner";
import { Switch } from "@/components/ui/switch";
import { Plus, Trash, CloudArrowUp, Eye, Microphone, CheckCircle, Play, Pause, Lightning, Brain, CurrencyDollar, ArrowUp, ArrowDown } from "@phosphor-icons/react";
import ElevenLabsConvaiEmbed from "@/components/ElevenLabsConvaiEmbed";

export default function ScreenCallAgentSection({ settings, onSaved, pipelineId, isAdmin, scopeName }) {
    const [form, setForm] = useState(settings.screen_call_agent || {});
    // Tracks the pipelineId we LAST loaded form state from. Used by the
    // useEffect below to decide whether a settings prop change is a real
    // scope switch (reset form) or a background refresh (keep form).
    const lastLoadedScopeRef = useRef(pipelineId);
    const [busy, setBusy] = useState(false);
    const [voices, setVoices] = useState([]);
    const [llmModels, setLlmModels] = useState([]);
    const [showPreview, setShowPreview] = useState(false);
    const [preview, setPreview] = useState(null);
    const [showTestWidget, setShowTestWidget] = useState(false);
    const [widgetFailed, setWidgetFailed] = useState(false);

    useEffect(() => {
        // Only reset the local form state when the user EXPLICITLY switches
        // scope (pipelineId changed) — NOT on every settings prop update.
        //
        // Why: the parent (Settings.jsx) re-fetches settings on a bunch of
        // triggers (top nav pipeline switch, post-save refresh, even a
        // sibling component triggering a re-render). The original
        // `[settings]` dependency made unsaved edits race-prone — a stray
        // refresh in between typing and clicking Save could wipe the form
        // back to the server's last-known value, making it look like Save
        // "didn't work" when really the form was reset BEFORE save fired.
        // Tracking pipelineId instead means the form resets only when you
        // genuinely change scope, which is the only time you actually
        // want fresh data.
        if (lastLoadedScopeRef.current !== pipelineId) {
            lastLoadedScopeRef.current = pipelineId;
            setForm(settings.screen_call_agent || {});
        }
    }, [pipelineId, settings]);

    useEffect(() => {
        api.get("/elevenlabs/voices").then((r) => {
            if (r.data?.voices) setVoices(r.data.voices);
        }).catch(() => {});
        api.get("/elevenlabs/llm-models").then((r) => {
            if (Array.isArray(r.data)) setLlmModels(r.data);
        }).catch(() => {});
    }, []);

    // Inject the ElevenLabs convai widget script when test widget toggled on
    useEffect(() => {
        if (!showTestWidget) return;
        const id = "elevenlabs-convai-widget-script";
        if (document.getElementById(id)) return;
        const s = document.createElement("script");
        s.id = id;
        s.src = "https://unpkg.com/@elevenlabs/convai-widget-embed";
        s.async = true;
        s.type = "text/javascript";
        // An office network that blocks unpkg.com would otherwise just render an
        // empty box — indistinguishable from a broken agent.
        s.onerror = () => setWidgetFailed(true);
        document.body.appendChild(s);
    }, [showTestWidget]);

    const save = async () => {
        setBusy(true);
        try {
            // Save settings AND push them live in one shot.
            const params = pipelineId ? { pipeline_id: pipelineId } : {};
            await api.put("/settings/screen-call-agent", form, { params });
            let syncSummary = null;
            let firstSyncError = "";
            try {
                // Sync only the pipeline being edited (or all when on global).
                const syncResp = await api.post(
                    "/elevenlabs/agent/sync",
                    pipelineId ? { pipeline_id: pipelineId } : {},
                );
                syncSummary = syncResp.data?.summary || null;
                if (syncSummary && (syncSummary.failed || 0) > 0) {
                    const failedRow = (syncResp.data?.results || []).find(
                        (r) => r.status === "failed",
                    );
                    // Surface the most actionable bit of the EL validation message.
                    const raw = failedRow?.error || "";
                    let parsed = raw;
                    try {
                        const j = JSON.parse(raw);
                        parsed = j?.detail?.message || j?.detail || raw;
                    } catch { /* not JSON */ }
                    firstSyncError = String(parsed).slice(0, 220);
                }
            } catch (syncErr) {
                firstSyncError = syncErr?.response?.data?.detail || syncErr?.message || "Unknown";
            }
            await onSaved?.();
            if (firstSyncError) {
                toast.error(`Saved — but ElevenLabs sync failed: ${firstSyncError}`, { duration: 8000 });
            } else {
                toast.success("Settings saved & live");
            }
        } catch { toast.error("Failed to save"); }
        finally { setBusy(false); }
    };

    const loadPreview = async () => {
        try {
            const r = await api.get("/elevenlabs/agent/preview-prompt");
            setPreview(r.data);
            setShowPreview(true);
        } catch { toast.error("Preview failed"); }
    };

    // IMPORTANT: use the functional updater so back-to-back upd() calls in the
    // same event handler don't clobber each other (e.g. picking a voice fires
    // upd("voice_id", id) AND upd("voice", name) — without functional updates,
    // the second call reads stale `form` from closure and drops the first).
    const upd = (k, v) => setForm((prev) => ({ ...prev, [k]: v }));

    const addQuestion = () => {
        const next = [...(form.screening_questions || []), { question: "", auto_screen: false }];
        upd("screening_questions", next);
    };
    const updateQuestion = (i, key, val) => {
        const next = [...(form.screening_questions || [])];
        next[i] = { ...next[i], [key]: val };
        upd("screening_questions", next);
    };
    const removeQuestion = (i) => {
        const next = [...(form.screening_questions || [])];
        next.splice(i, 1);
        upd("screening_questions", next);
    };
    const moveQuestion = (i, dir) => {
        const next = [...(form.screening_questions || [])];
        const target = i + dir;
        if (target < 0 || target >= next.length) return;
        [next[i], next[target]] = [next[target], next[i]];
        upd("screening_questions", next);
    };

    return (
        <div className="max-w-3xl space-y-8" data-testid="screen-call-agent-section">
            <div className="flex items-start justify-between gap-4">
                <div>
                    <h2 className="font-heading text-2xl font-bold tracking-tight">Screen Call Agent</h2>
                    <p className="text-sm text-ink-muted mt-1">
                        Configure your AI screening agent. Save once — changes go live automatically.
                    </p>
                </div>
                {/* /elevenlabs/agent/preview-prompt is super-admin-only — for a
                    recruiter this button can only 403 into "Preview failed". */}
                {isAdmin && (
                    <div className="flex gap-2 flex-shrink-0">
                        <button
                            onClick={loadPreview}
                            data-testid="preview-prompt-btn"
                            className="btn-secondary !py-1.5 !px-3 text-xs flex items-center gap-1.5"
                        >
                            <Eye size={12} weight="bold" /> Preview prompt
                        </button>
                    </div>
                )}
            </div>

            {/* Live Status Strip — admins only. The backend redacts
                `elevenlabs_agent_id` out of a recruiter's /settings payload, so
                for them this would permanently read "Not linked yet" on an agent
                that is live and dialing, and no save could ever clear it. */}
            {isAdmin && (
                <div className="surface p-4 flex flex-wrap items-center gap-4">
                    <div className="flex items-center gap-2">
                        <CheckCircle size={14} weight="fill" className="text-brand-success" />
                        <div>
                            <div className="label-overline">Status</div>
                            <div className="text-xs text-ink-muted mt-0.5">
                                {form.elevenlabs_agent_id ? "Linked & live" : "Not linked yet — save to publish"}
                            </div>
                        </div>
                    </div>
                    <div className="ml-auto flex gap-2">
                        <button
                            onClick={() => setShowTestWidget((s) => !s)}
                            data-testid="test-agent-btn"
                            disabled={!form.elevenlabs_agent_id}
                            title={form.elevenlabs_agent_id ? "" : "Save the agent first — there's nothing to test until it's linked"}
                            className="btn-secondary !py-1.5 !px-3 text-xs flex items-center gap-1.5 disabled:opacity-50"
                        >
                            <Microphone size={12} weight="bold" /> {showTestWidget ? "Hide test widget" : "Test the agent"}
                        </button>
                    </div>
                </div>
            )}

            {/* Coordination */}
            <Section title="Coordination">
                <Row label="Agent Name">
                    <input data-testid="agent-name-input" className="input-dark" value={form.agent_name || ""} onChange={(e) => upd("agent_name", e.target.value)} />
                </Row>
                <Row label="Language">
                    <select data-testid="agent-language-select" className="input-dark" value={form.language || "English"} onChange={(e) => upd("language", e.target.value)}>
                        {["English", "Spanish", "French", "German", "Portuguese", "Italian"].map((l) => <option key={l} value={l}>{l}</option>)}
                    </select>
                </Row>
                <Row label="Warmup Delay (minutes)" help="How long to wait between sending the warmup message and making the screening call.">
                    <input type="number" min="0" className="input-dark w-32" data-testid="warmup-delay-input" value={form.warmup_delay_minutes || 0} onChange={(e) => upd("warmup_delay_minutes", Number(e.target.value))} />
                </Row>
                <Row
                    label="How candidates are first contacted"
                    help="Chat first leads with a text and an email; the call still goes out if nobody replies, so nobody is contacted less. Many candidates find a text easier to answer than an unknown number."
                >
                    <select
                        className="input-dark"
                        data-testid="screening-mode-select"
                        // Legacy values ("voice_only" / "voice_and_chat") both mean
                        // call-first and are normalised on the server; show them as
                        // that rather than as a fourth and fifth option nobody can
                        // tell apart.
                        value={["chat_first", "chat_only"].includes(form.screening_mode) ? form.screening_mode : "voice_first"}
                        onChange={(e) => upd("screening_mode", e.target.value)}
                    >
                        <option value="chat_first">Chat first — text on arrival, call if no reply</option>
                        <option value="voice_first">Call first — AI calls after the warmup delay</option>
                        <option value="chat_only">Chat only — no screening calls at all</option>
                    </select>
                </Row>
                {form.screening_mode === "chat_first" && (
                    <Row
                        label="Wait before calling (minutes)"
                        help="How long to give them to reply to the text before the AI calls. Two hours is long enough to catch someone on a break and short enough that the call still lands the same working day."
                    >
                        <input
                            type="number"
                            min="15"
                            className="input-dark w-32"
                            data-testid="chat-first-delay-input"
                            value={form.chat_first_delay_minutes ?? 120}
                            onChange={(e) => upd("chat_first_delay_minutes", Number(e.target.value))}
                        />
                    </Row>
                )}
            </Section>

            {/* Voice & AI */}
            <Section title="Voice & AI">
                <div>
                    <div className="label-overline mb-1.5 flex items-center gap-2 flex-wrap">
                        Voice
                        <span className="text-ink-dim normal-case font-normal tracking-normal">
                            — click <span className="text-ink">card</span> to select · click <span className="text-ink">▶</span> to preview
                        </span>
                    </div>
                    <VoicePicker
                        voices={voices}
                        value={form.voice_id}
                        onChange={(voice) => {
                            const displayName = (voice.name || "").split(" - ")[0];
                            // Single setForm so both fields land in the same render —
                            // and so the next "Save" sends voice_id along with voice.
                            setForm((prev) => ({
                                ...prev,
                                voice_id: voice.voice_id,
                                voice: displayName,
                            }));
                            toast.success(`Voice set to ${displayName || "selected voice"}`);
                        }}
                    />
                </div>
                <Row label="Voice Model (TTS engine)" help="The text-to-speech engine that generates the agent's voice. v4 Turbo is the newest and starts speaking about twice as fast as Turbo v2. Use v2_5 / Multilingual only when the agent's Language is not English.">
                    <select className="input-dark" data-testid="voice-model-select" value={form.voice_model || "eleven_v4_turbo"} onChange={(e) => upd("voice_model", e.target.value)}>
                        <optgroup label="English agents (recommended)">
                            <option value="eleven_v4_turbo">v4 Turbo — newest, fastest + most natural (default)</option>
                            <option value="eleven_turbo_v2">Turbo v2 — previous default</option>
                            <option value="eleven_flash_v2">Flash v2 — fast, older voice engine</option>
                        </optgroup>
                        <optgroup label="Non-English / multilingual">
                            <option value="eleven_turbo_v2_5">Turbo v2.5 — multilingual, low latency</option>
                            <option value="eleven_flash_v2_5">Flash v2.5 — multilingual, fastest</option>
                            <option value="eleven_multilingual_v2">Multilingual v2 — 30+ languages, premium</option>
                        </optgroup>
                    </select>
                </Row>
                <div>
                    <div className="label-overline mb-1.5">LLM Model <span className="text-ink-dim normal-case font-normal tracking-normal">— what powers Olivia's brain</span></div>
                    <LlmModelPicker
                        models={llmModels}
                        value={form.llm_model}
                        onChange={(id) => upd("llm_model", id)}
                    />
                </div>
                <details className="border-t border-strokes pt-4 mt-2">
                    <summary className="text-[11px] uppercase tracking-widest text-ink-muted cursor-pointer hover:text-ink select-none">
                        Advanced — provider IDs (auto-managed)
                    </summary>
                    <div className="space-y-3 mt-3 pl-3 border-l border-strokes">
                        <Row label="Agent ID" help="The provider-side ID of your screening agent. Auto-set on first save — don't change unless you know what you're doing.">
                            <input data-testid="elevenlabs-agent-id-input" className="input-dark font-mono text-xs" value={form.elevenlabs_agent_id || ""} onChange={(e) => upd("elevenlabs_agent_id", e.target.value)} />
                        </Row>
                        <Row label="Phone Number ID" help="Provider-side phone resource ID. Auto-set when you assign a number.">
                            <input data-testid="elevenlabs-phone-id-input" className="input-dark font-mono text-xs" value={form.elevenlabs_phone_number_id || ""} onChange={(e) => upd("elevenlabs_phone_number_id", e.target.value)} />
                        </Row>
                    </div>
                </details>
            </Section>

            {/* Appointment Booking */}
            <Section title="Appointment Booking">
                <Row label="Enable booking" help="When enabled, the agent will pencil-in an appointment after the screening questions.">
                    <Switch checked={!!form.enable_appointment_booking} onCheckedChange={(v) => upd("enable_appointment_booking", v)} data-testid="enable-booking-switch" />
                </Row>
                {/* "Days offered" used to live here writing screen_call_agent.appointment_days_offered —
                    a key nothing in the backend reads. The real booking window is
                    appointments.booking_days_offered, edited in Booking & Slots. */}
                <Row
                    label="Auto-approve delay"
                    help="After a successful screening call captures a slot, the candidate stays at SCREENING with Approve/Deny pills active for this many minutes before being auto-promoted to APPOINTMENT. Recruiters can Approve early or Cancel during the window. Set to 0 to disable auto-approval."
                >
                    <div className="flex items-center gap-2">
                        <input
                            type="number"
                            min="0"
                            max="10080"
                            className="input-dark w-32"
                            data-testid="auto-promote-delay-input"
                            value={form.auto_promote_delay_minutes != null ? form.auto_promote_delay_minutes : 120}
                            onChange={(e) => upd("auto_promote_delay_minutes", Number(e.target.value))}
                        />
                        <span className="text-xs text-ink-muted">minutes (default 120 = 2 hours)</span>
                    </div>
                </Row>
            </Section>

            {/* Voicemail detection */}
            <Section title="Voicemail Handling" subtitle="Native ElevenLabs detection — drops a short message and hangs up when an answering machine or call screener picks up.">
                <Row label="Enable voicemail detection" help="Uses the platform's built-in voicemail_detection system tool. When the AI hears a beep / automated greeting / 'leave a message' prompt, it leaves your configured message and ends the call (no wasted minutes running the full screening to a voicemail).">
                    <Switch
                        checked={form.voicemail_detection_enabled !== false}
                        onCheckedChange={(v) => upd("voicemail_detection_enabled", !!v)}
                        data-testid="voicemail-detection-toggle"
                    />
                </Row>
                {form.voicemail_detection_enabled !== false && (
                    <Row label="Voicemail message" help="Short script the AI leaves when voicemail is detected. Use placeholders [Agent Name], [Company], [Role], [First Name].">
                        <textarea
                            data-testid="voicemail-message-textarea"
                            rows={4}
                            className="input-dark"
                            placeholder="Hi, this is [Agent Name] from [Company]. I'm calling about your application for the [Role] role…"
                            value={form.voicemail_message || ""}
                            onChange={(e) => upd("voicemail_message", e.target.value)}
                        />
                    </Row>
                )}
            </Section>

            {/* Prompting */}
            <Section title="Prompting">
                <Row label="Purpose" help="Describes the purpose of this call to the AI agent.">
                    <textarea data-testid="purpose-textarea" rows={3} className="input-dark" value={form.purpose || ""} onChange={(e) => upd("purpose", e.target.value)} />
                </Row>
                <Row label="Opening Message" help="The first thing the agent says when the candidate picks up.">
                    <textarea data-testid="opening-message-textarea" rows={2} className="input-dark" value={form.opening_message || ""} onChange={(e) => upd("opening_message", e.target.value)} />
                </Row>

                <div>
                    <div className="flex items-center justify-between mb-2">
                        <div>
                            <div className="label-overline">Screening Questions</div>
                            <div className="text-xs text-ink-muted mt-0.5">{(form.screening_questions || []).length} questions</div>
                        </div>
                        <button onClick={addQuestion} data-testid="add-question-btn" className="btn-secondary !py-1 !px-2 text-xs flex items-center gap-1">
                            <Plus size={11} weight="bold" /> Add
                        </button>
                    </div>
                    <div className="space-y-2">
                        {(form.screening_questions || []).map((q, i) => (
                            <div key={i} className="surface p-3 space-y-2" data-testid={`question-${i}`}>
                                <div className="flex items-center justify-between">
                                    <div className="flex items-center gap-1">
                                        <span className="text-xs font-mono text-ink-muted w-6">Q{i + 1}</span>
                                        <button
                                            onClick={() => moveQuestion(i, -1)}
                                            disabled={i === 0}
                                            data-testid={`move-up-${i}`}
                                            className="p-1 rounded hover:bg-surface-hover text-ink-muted disabled:opacity-20 disabled:cursor-default"
                                        >
                                            <ArrowUp size={11} weight="bold" />
                                        </button>
                                        <button
                                            onClick={() => moveQuestion(i, 1)}
                                            disabled={i === (form.screening_questions || []).length - 1}
                                            data-testid={`move-down-${i}`}
                                            className="p-1 rounded hover:bg-surface-hover text-ink-muted disabled:opacity-20 disabled:cursor-default"
                                        >
                                            <ArrowDown size={11} weight="bold" />
                                        </button>
                                    </div>
                                    <div className="flex items-center gap-3">
                                        <label className="flex items-center gap-1.5 text-xs">
                                            <Switch checked={!!q.auto_screen} onCheckedChange={(v) => updateQuestion(i, "auto_screen", v)} data-testid={`auto-screen-${i}`} />
                                            <span className="text-ink-muted">Auto-screen</span>
                                        </label>
                                        <button onClick={() => removeQuestion(i)} data-testid={`remove-question-${i}`} className="text-brand-danger p-1 hover:bg-surface-hover rounded">
                                            <Trash size={11} />
                                        </button>
                                    </div>
                                </div>
                                <input
                                    data-testid={`question-input-${i}`}
                                    className="input-dark"
                                    value={q.question || ""}
                                    placeholder="What's your earliest start date?"
                                    onChange={(e) => updateQuestion(i, "question", e.target.value)}
                                />
                            </div>
                        ))}
                    </div>
                </div>

                <Row label="Booking Instructions" help="Customize how the agent handles the booking conversation.">
                    <textarea data-testid="booking-instructions-textarea" rows={3} className="input-dark" value={form.booking_instructions || ""} onChange={(e) => upd("booking_instructions", e.target.value)} />
                </Row>
                <Row label="Closing Message">
                    <textarea data-testid="closing-message-textarea" rows={3} className="input-dark" value={form.closing_message || ""} onChange={(e) => upd("closing_message", e.target.value)} />
                </Row>
                <Row label="Additional Context (FAQs, etc.)" help="Pay info, location details, interview format, anything the agent should know.">
                    <textarea data-testid="additional-context-textarea" rows={4} className="input-dark" value={form.additional_context || ""} onChange={(e) => upd("additional_context", e.target.value)} />
                </Row>
            </Section>

            <div className="flex items-center gap-3 sticky bottom-0 bg-[#0C0C0E]/95 backdrop-blur border-t border-strokes -mx-8 px-8 py-3 mt-4 z-10">
                <button onClick={save} disabled={busy} data-testid="save-agent-btn" className="btn-primary flex items-center gap-1.5">
                    <CloudArrowUp size={13} weight="bold" />
                    {busy ? "Saving…" : `Save for ${scopeName || "this pipeline"}`}
                </button>
                <span className="text-[11px] text-ink-muted">
                    Saving applies these changes to <strong className="text-ink">{scopeName || "this pipeline"}</strong> only — switch the scope at the top to save elsewhere.
                </span>
            </div>

            {/* Preview prompt modal */}
            {showPreview && preview && (
                <div className="fixed inset-0 z-50 bg-black/70 backdrop-blur flex items-center justify-center p-4" onClick={() => setShowPreview(false)} data-testid="prompt-preview-modal">
                    <div className="bg-[#0E0E11] border border-strokes rounded-lg max-w-3xl w-full max-h-[85vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
                        <div className="sticky top-0 bg-[#0E0E11] p-5 border-b border-strokes flex items-center justify-between">
                            <div>
                                <div className="font-heading text-lg font-semibold">Prompt preview</div>
                                <div className="text-xs text-ink-muted">This is exactly what your agent will use.</div>
                            </div>
                            <button onClick={() => setShowPreview(false)} className="text-ink-muted hover:text-ink p-1">✕</button>
                        </div>
                        <div className="p-5 space-y-4">
                            <div>
                                <div className="label-overline mb-1.5">First message</div>
                                <div className="surface p-3 text-sm font-mono text-xs">{preview.first_message}</div>
                            </div>
                            <div>
                                <div className="label-overline mb-1.5">System prompt ({preview.system_prompt.length} chars)</div>
                                <pre className="surface p-4 text-xs font-mono whitespace-pre-wrap leading-relaxed">{preview.system_prompt}</pre>
                            </div>
                        </div>
                    </div>
                </div>
            )}

            {/* Embedded test widget */}
            {showTestWidget && form.elevenlabs_agent_id && (
                <div className="surface p-5" data-testid="test-widget-container">
                    <div className="label-overline mb-3">Test the agent (browser)</div>
                    <div className="text-xs text-ink-muted mb-4">
                        Click the widget below to start a test conversation in your browser. This uses the same agent that gets called when you initiate an outbound call to a candidate.
                        <br />
                        <span className="text-ink-muted/70">
                            Test runs use placeholder values for {`{role}`}, {`{full_name}`}, {`{company}`}, {`{agent_name}`} so the agent's first message renders.
                        </span>
                    </div>
                    {widgetFailed && (
                        <div className="text-xs text-[#F87171] mb-3">
                            Test widget could not load — check this network&apos;s access to unpkg.com. The agent
                            itself is unaffected; only in-browser testing needs that script.
                        </div>
                    )}
                    {/* Renders the official ElevenLabs Convai web component.
                        Constructed via createElement (not dangerouslySetInnerHTML)
                        so attribute values can never escape into raw HTML — the
                        DOM API serialises them safely. JSON.stringify on the
                        dynamic_variables object handles quote-escaping inside
                        the string payload itself. */}
                    <ElevenLabsConvaiEmbed
                        agentId={form.elevenlabs_agent_id}
                        dynamicVariables={{
                            role: "Customer Service Representative",
                            full_name: "Test Candidate",
                            company: "Example Co",
                            agent_name: form.agent_name || "Olivia",
                            city: "Downtown",
                            appointment_date: "tomorrow",
                            appointment_time: "2:00 PM",
                        }}
                    />
                </div>
            )}
        </div>
    );
}

function Section({ title, children }) {
    return (
        <div>
            <h3 className="font-heading text-base font-semibold mb-4 text-ink">{title}</h3>
            <div className="space-y-4">{children}</div>
        </div>
    );
}

function Row({ label, help, children }) {
    return (
        <div>
            <div className="flex items-baseline justify-between gap-3 mb-1.5">
                <label className="label-overline">{label}</label>
            </div>
            {children}
            {help && <div className="text-xs text-ink-muted mt-1">{help}</div>}
        </div>
    );
}



function VoicePicker({ voices, value, onChange }) {
    const [playingId, setPlayingId] = useState(null);
    const audioRef = useRef(null);

    const togglePreview = (e, voice) => {
        e.stopPropagation();
        if (!voice.preview_url) {
            toast.message("No preview available for this voice");
            return;
        }
        if (playingId === voice.voice_id) {
            audioRef.current?.pause();
            setPlayingId(null);
            return;
        }
        if (audioRef.current) {
            audioRef.current.pause();
            audioRef.current = null;
        }
        const a = new Audio(voice.preview_url);
        a.onended = () => setPlayingId(null);
        a.onerror = () => { setPlayingId(null); toast.error("Couldn't play preview"); };
        a.play().catch(() => { setPlayingId(null); toast.error("Click again to allow audio playback"); });
        audioRef.current = a;
        setPlayingId(voice.voice_id);
    };

    if (voices.length === 0) {
        return <div className="text-sm text-ink-muted">Loading voices…</div>;
    }

    return (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 max-h-[420px] overflow-y-auto pr-1" data-testid="voice-picker-grid">
            {voices.map((v) => {
                const labels = v.labels || {};
                const isSelected = value === v.voice_id;
                const isPlaying = playingId === v.voice_id;
                return (
                    <button
                        key={v.voice_id}
                        type="button"
                        onClick={() => onChange(v)}
                        data-testid={`voice-card-${v.voice_id}`}
                        data-selected={isSelected}
                        className={`flex items-center gap-3 p-3 rounded-md border transition-all text-left cursor-pointer relative ${
                            isSelected
                                ? "border-brand-primary bg-[rgba(139,92,246,0.12)] ring-2 ring-brand-primary/30 shadow-[0_0_0_1px_rgba(139,92,246,0.4)]"
                                : "border-strokes hover:border-strokes-focus hover:bg-surface-hover"
                        }`}
                    >
                        {isSelected && (
                            <span className="absolute top-1.5 right-1.5 text-[8px] font-bold uppercase tracking-widest px-1.5 py-0.5 rounded bg-brand-primary text-white">
                                Selected
                            </span>
                        )}
                        <button
                            type="button"
                            onClick={(e) => togglePreview(e, v)}
                            data-testid={`voice-preview-${v.voice_id}`}
                            className={`flex-shrink-0 w-9 h-9 rounded-full border flex items-center justify-center transition-colors ${
                                isPlaying
                                    ? "border-brand-primary bg-brand-primary text-white"
                                    : "border-strokes hover:border-brand-primary text-ink"
                            }`}
                            aria-label={isPlaying ? "Pause preview" : "Play preview"}
                        >
                            {isPlaying ? <Pause size={14} weight="fill" /> : <Play size={14} weight="fill" />}
                        </button>
                        <div className="flex-1 min-w-0">
                            <div className="font-medium text-sm truncate">{v.name}</div>
                            <div className="flex items-center gap-1.5 mt-0.5 flex-wrap">
                                {labels.gender && <Tag>{labels.gender}</Tag>}
                                {labels.age && <Tag>{labels.age}</Tag>}
                                {labels.accent && <Tag>{labels.accent}</Tag>}
                                {labels.use_case && <Tag tone="primary">{labels.use_case}</Tag>}
                            </div>
                        </div>
                        {isSelected && <CheckCircle size={16} weight="fill" className="text-brand-primary flex-shrink-0" />}
                    </button>
                );
            })}
        </div>
    );
}

function Tag({ children, tone = "default" }) {
    const cls = tone === "primary"
        ? "bg-[rgba(139,92,246,0.12)] text-brand-primary"
        : "bg-surface-active text-ink-muted";
    return (
        <span className={`inline-block px-1.5 py-0.5 rounded text-[9px] uppercase tracking-wider font-semibold ${cls}`}>
            {children}
        </span>
    );
}


function LlmModelPicker({ models, value, onChange }) {
    if (models.length === 0) {
        return <div className="text-sm text-ink-muted">Loading models…</div>;
    }
    return (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2" data-testid="llm-model-picker">
            {models.map((m) => {
                const isSelected = (value || "").startsWith(m.id) || value === m.id;
                const tierBadge = m.tier === "premium"
                    ? { label: "PREMIUM", cls: "bg-[rgba(245,158,11,0.15)] text-[#FBBF24]" }
                    : { label: "BALANCED", cls: "bg-[rgba(16,185,129,0.15)] text-brand-success" };
                return (
                    <button
                        key={m.id}
                        type="button"
                        onClick={() => onChange(m.id)}
                        data-testid={`llm-card-${m.id}`}
                        data-selected={isSelected}
                        className={`text-left p-3 rounded-md border transition-all ${
                            isSelected
                                ? "border-brand-primary bg-[rgba(139,92,246,0.08)]"
                                : "border-strokes hover:border-strokes-focus"
                        }`}
                    >
                        <div className="flex items-start justify-between gap-2 mb-1">
                            <div>
                                <div className="font-semibold text-sm flex items-center gap-1.5">
                                    {m.label}
                                    {m.default && <span className="text-[8px] uppercase tracking-widest px-1 py-0.5 rounded bg-brand-primary/20 text-brand-primary font-bold">DEFAULT</span>}
                                </div>
                                <div className="text-[11px] text-ink-muted">{m.provider}</div>
                            </div>
                            <span className={`text-[8px] uppercase tracking-widest font-bold px-1.5 py-0.5 rounded ${tierBadge.cls}`}>{tierBadge.label}</span>
                        </div>
                        <div className="text-xs text-ink mb-2">{m.tagline}</div>
                        <div className="text-[11px] text-ink-muted leading-snug mb-2">{m.best_for}</div>
                        <div className="flex items-center gap-3 text-[10px] text-ink-dim">
                            <span className="flex items-center gap-1"><Lightning size={10} weight="fill" />{m.latency_ms}ms</span>
                            {m.cost_per_1m_in != null && <span className="flex items-center gap-1"><CurrencyDollar size={10} weight="fill" />${m.cost_per_1m_in}/${m.cost_per_1m_out} per 1M</span>}
                        </div>
                        {isSelected && <Brain size={14} weight="fill" className="text-brand-primary mt-2" />}
                    </button>
                );
            })}
        </div>
    );
}



