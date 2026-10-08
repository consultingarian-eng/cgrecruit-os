import { useState, useRef, useEffect } from "react";
import api from "@/lib/api";
import { toast } from "sonner";
import { Switch } from "@/components/ui/switch";

const DAYS = [
    { v: 0, label: "Mon" }, { v: 1, label: "Tue" }, { v: 2, label: "Wed" },
    { v: 3, label: "Thu" }, { v: 4, label: "Fri" }, { v: 5, label: "Sat" }, { v: 6, label: "Sun" },
];

export default function AutoDialerSection({ settings, onSaved, pipelineId }) {
    const initial = () => settings.auto_dialer || {
        enabled: true,
        auto_dial_on_apply: true,
        max_retry_attempts: 3,
        retry_delay_hours: 4,
        batch_interval_seconds: 30,
        call_window_start: "09:00",
        call_window_end: "19:00",
        call_window_days: [0, 1, 2, 3, 4],
        sync_after_call_minutes: 8,
        pre_call_sms_enabled: true,
        pre_call_sms_minutes: 3,
    };
    const [form, setForm] = useState(initial);
    // Only reset form when the SCOPE changes (user explicitly switched
    // pipelines in the top nav). Prop-driven `useEffect([settings])` was
    // wiping unsaved edits on every background refresh — see
    // `/app/frontend/src/lib/useScopedFormState.js` for the rationale.
    const lastScopeRef = useRef(pipelineId);
    useEffect(() => {
        if (lastScopeRef.current !== pipelineId) {
            lastScopeRef.current = pipelineId;
            if (settings.auto_dialer) setForm(settings.auto_dialer);
        }
    }, [pipelineId, settings]);

    const [busy, setBusy] = useState(false);

    const upd = (k, v) => setForm({ ...form, [k]: v });

    const save = async () => {
        setBusy(true);
        try {
            const params = pipelineId ? { pipeline_id: pipelineId } : {};
            await api.put("/settings/auto-dialer", form, { params });
            await onSaved?.();
            toast.success("Auto-dialer saved");
        } catch (e) {
            toast.error("Failed to save");
        } finally { setBusy(false); }
    };

    const toggleDay = (v) => {
        const days = form.call_window_days || [];
        if (days.includes(v)) upd("call_window_days", days.filter((x) => x !== v));
        else upd("call_window_days", [...days, v].sort());
    };

    return (
        <div className="max-w-2xl space-y-6" data-testid="auto-dialer-section">
            <div>
                <h2 className="font-heading text-2xl font-bold tracking-tight">Auto-Dialer</h2>
                <p className="text-sm text-ink-muted mt-1">
                    Place AI screening calls automatically — on apply, on retry, in batch — only inside your call window.
                </p>
            </div>

            <div className="surface p-5 space-y-4">
                <ToggleRow
                    label="Auto-dialer enabled"
                    help="Master switch. Turning this off stops all auto-dialing immediately, including calls that are already scheduled. Callbacks a candidate asked for themselves still go out."
                    checked={form.enabled}
                    onChange={(v) => upd("enabled", v)}
                    testid="enabled-switch"
                />
                <ToggleRow
                    label="Auto-dial on apply"
                    help="When a candidate submits the public Apply form, automatically schedule a screening call after the warmup delay."
                    checked={form.auto_dial_on_apply}
                    onChange={(v) => upd("auto_dial_on_apply", v)}
                    testid="auto-dial-on-apply-switch"
                />
            </div>

            <div className="surface p-5 space-y-4">
                <div>
                    <h3 className="font-heading text-base font-semibold">Call window</h3>
                    <p className="text-xs text-ink-muted mt-0.5">Calls placed outside this window are pushed to the next valid time. Uses your timezone (Settings → Region & Language).</p>
                </div>
                <div className="grid grid-cols-2 gap-3">
                    <Field label="Window start (24h)">
                        <input type="time" data-testid="window-start-input" className="input-dark" value={form.call_window_start || "09:00"} onChange={(e) => upd("call_window_start", e.target.value)} />
                    </Field>
                    <Field label="Window end (24h)">
                        <input type="time" data-testid="window-end-input" className="input-dark" value={form.call_window_end || "19:00"} onChange={(e) => upd("call_window_end", e.target.value)} />
                    </Field>
                </div>
                <Field label="Days">
                    <div className="flex gap-1">
                        {DAYS.map((d) => {
                            const on = (form.call_window_days || []).includes(d.v);
                            return (
                                <button
                                    key={d.v}
                                    onClick={() => toggleDay(d.v)}
                                    data-testid={`day-${d.v}`}
                                    className={`flex-1 px-2 py-1.5 rounded text-xs font-medium border transition-colors ${
                                        on ? "bg-brand-primary border-brand-primary text-white" : "bg-transparent border-strokes text-ink-muted hover:border-strokes-focus"
                                    }`}
                                >
                                    {d.label}
                                </button>
                            );
                        })}
                    </div>
                </Field>
            </div>

            <div className="surface p-5 space-y-4">
                <div>
                    <h3 className="font-heading text-base font-semibold">Pre-call warmup SMS</h3>
                    <p className="text-xs text-ink-muted mt-0.5">Send a heads-up text from the pipeline's Twilio number a few minutes before Olivia dials. Drives answer rates significantly.</p>
                </div>
                <ToggleRow
                    label="Send warmup SMS before each call"
                    help="Uses the warmup template's `sms_body` from Settings → Applicant Comms."
                    checked={form.pre_call_sms_enabled}
                    onChange={(v) => upd("pre_call_sms_enabled", v)}
                    testid="pre-call-sms-switch"
                />
                <Field label="Minutes before call">
                    <input
                        type="number"
                        min="1"
                        max="30"
                        className="input-dark w-32"
                        data-testid="pre-call-sms-minutes-input"
                        value={form.pre_call_sms_minutes ?? 2}
                        onChange={(e) => upd("pre_call_sms_minutes", Number(e.target.value))}
                    />
                </Field>
            </div>

            <div className="surface p-5 space-y-4">
                <div>
                    <h3 className="font-heading text-base font-semibold">Retry policy</h3>
                    <p className="text-xs text-ink-muted mt-0.5">If a call comes back as no-answer, schedule a retry. Set how long to wait before each attempt.</p>
                </div>
                <Field label="Max retry attempts">
                    <input
                        type="number"
                        min="0"
                        max="10"
                        className="input-dark w-24"
                        data-testid="max-retries-input"
                        value={form.max_retry_attempts ?? 3}
                        onChange={(e) => {
                            const n = Number(e.target.value);
                            const current = form.retry_delays?.length
                                ? form.retry_delays
                                : Array(form.max_retry_attempts || 0).fill(form.retry_delay_hours || 2);
                            const resized = Array.from({ length: Math.max(0, n - 1) }, (_, i) => current[i] ?? form.retry_delay_hours ?? 2);
                            setForm({ ...form, max_retry_attempts: n, retry_delays: resized });
                        }}
                    />
                </Field>
                {(form.max_retry_attempts > 1) && (
                    <div className="space-y-2 pt-1">
                        <p className="text-xs text-ink-muted font-medium">Hours to wait before each retry:</p>
                        {Array.from({ length: form.max_retry_attempts - 1 }).map((_, i) => {
                            const delays = form.retry_delays?.length
                                ? form.retry_delays
                                : Array(form.max_retry_attempts).fill(form.retry_delay_hours ?? 2);
                            return (
                                <div key={i} className="flex items-center gap-3">
                                    <span className="text-xs text-ink-muted w-32 shrink-0">After attempt {i + 1}</span>
                                    <input
                                        type="number"
                                        min="1"
                                        max="72"
                                        className="input-dark w-24"
                                        value={delays[i] ?? form.retry_delay_hours ?? 2}
                                        onChange={(e) => {
                                            const next = Array.from({ length: form.max_retry_attempts }, (_, j) =>
                                                delays[j] ?? form.retry_delay_hours ?? 2
                                            );
                                            next[i] = Number(e.target.value);
                                            upd("retry_delays", next);
                                        }}
                                    />
                                    <span className="text-xs text-ink-muted">hours</span>
                                </div>
                            );
                        })}
                    </div>
                )}
            </div>

            <div className="surface p-5 space-y-4">
                <div>
                    <h3 className="font-heading text-base font-semibold">Batch dial pacing</h3>
                    <p className="text-xs text-ink-muted mt-0.5">When you click "Dial all queued", calls are placed with this gap between them (avoids ElevenLabs/Twilio rate limits).</p>
                </div>
                <Field label="Seconds between calls">
                    <input type="number" min="5" max="600" className="input-dark w-32" data-testid="batch-interval-input" value={form.batch_interval_seconds || 30} onChange={(e) => upd("batch_interval_seconds", Number(e.target.value))} />
                </Field>
                <Field
                    label="Max concurrent calls (this pipeline)"
                    help="Per-pipeline cap for fairness — one office shouldn't hog the whole ElevenLabs concurrency budget. Effective cap at runtime is the smaller of this and the tenant-wide ceiling (set on the global settings page)."
                >
                    <div className="flex items-center gap-2">
                        <input
                            type="number"
                            min="1"
                            max="100"
                            className="input-dark w-32"
                            data-testid="max-concurrent-input"
                            value={form.max_concurrent_calls ?? 5}
                            onChange={(e) => upd("max_concurrent_calls", Number(e.target.value))}
                        />
                        <span className="text-xs text-ink-muted">simultaneous calls in this pipeline</span>
                    </div>
                </Field>
                {/* Tenant-wide ceiling is a SINGLE GLOBAL value — show it
                    only when editing the tenant-wide / global settings (no
                    pipeline_id). Inside a per-pipeline view (one office's
                    pipeline) the field would imply you can change the
                    tenant ceiling from inside one pipeline, which you can't,
                    and it'd give two different per-pipeline pages diverging
                    "current values" of the same global setting. */}
                {!pipelineId && (
                    <Field
                        label="Tenant-wide ceiling (your ElevenLabs plan limit)"
                        help="Hard cap across ALL pipelines for this account — set this to your ElevenLabs subscription's concurrent-call limit (Creator: 5, Business: 20, Enterprise: custom). The dialer never lets the sum of live calls across pipelines exceed this. Tenant-wide setting; only editable from the global view."
                    >
                        <TenantCapField />
                    </Field>
                )}
                <Field label="Sync transcript N minutes after call ends">
                    <input type="number" min="3" max="60" className="input-dark w-32" data-testid="sync-after-input" value={form.sync_after_call_minutes || 8} onChange={(e) => upd("sync_after_call_minutes", Number(e.target.value))} />
                </Field>
            </div>

            <button onClick={save} disabled={busy} data-testid="save-auto-dialer-btn" className="btn-primary">
                {busy ? "Saving…" : "Save"}
            </button>
        </div>
    );
}

function ToggleRow({ label, help, checked, onChange, testid }) {
    return (
        <div className="flex items-start justify-between gap-4">
            <div className="flex-1">
                <div className="text-sm font-medium">{label}</div>
                {help && <div className="text-xs text-ink-muted mt-0.5">{help}</div>}
            </div>
            <Switch checked={!!checked} onCheckedChange={onChange} data-testid={testid} />
        </div>
    );
}

function Field({ label, help, children }) {
    return (
        <div>
            <label className="label-overline block mb-1.5">{label}</label>
            {help && <p className="text-[11px] text-ink-muted mb-1.5 leading-relaxed">{help}</p>}
            {children}
        </div>
    );
}

function TenantCapField() {
    const [val, setVal] = useState(null);
    const [busy, setBusy] = useState(false);

    useEffect(() => {
        // Always read from the GLOBAL settings doc — tenant cap is tenant-wide.
        api.get("/settings").then((r) => setVal(r.data?.tenant_max_concurrent_calls ?? 5)).catch(() => setVal(5));
    }, []);

    const save = async () => {
        setBusy(true);
        try {
            await api.put("/settings/tenant-concurrency", { tenant_max_concurrent_calls: Number(val) });
            toast.success(`Tenant ceiling set to ${val}`);
        } catch { toast.error("Couldn't save"); }
        finally { setBusy(false); }
    };

    if (val === null) return <div className="text-xs text-ink-muted">Loading…</div>;
    return (
        <div className="flex items-center gap-2">
            <input
                type="number"
                min="1"
                max="200"
                className="input-dark w-32"
                data-testid="tenant-max-concurrent-input"
                value={val}
                onChange={(e) => setVal(Number(e.target.value))}
            />
            <button
                onClick={save}
                disabled={busy}
                data-testid="tenant-max-concurrent-save"
                className="text-[11px] btn-secondary !py-1 !px-2"
            >
                {busy ? "Saving…" : "Save tenant ceiling"}
            </button>
            <span className="text-xs text-ink-muted">across all pipelines</span>
        </div>
    );
}
