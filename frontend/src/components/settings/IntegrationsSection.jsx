import { useState, useEffect } from "react";
import api from "@/lib/api";
import { toast } from "sonner";
import { Plug, ArrowRight, Copy, ArrowsClockwise, Link } from "@phosphor-icons/react";

export default function IntegrationsSection({ settings, onSaved, readOnly = false }) {
    const [form, setForm] = useState(() => ({
        cg1_enabled: false,
        cg1_webhook_url: "",
        cg1_webhook_secret: "",
        ...((settings && settings.integrations) || {}),
    }));
    const [saving, setSaving] = useState(false);
    const [testing, setTesting] = useState(false);

    const [zapierUrl, setZapierUrl] = useState("");
    const [zapierLoading, setZapierLoading] = useState(true);
    const [zapierRegen, setZapierRegen] = useState(false);

    useEffect(() => {
        api.get("/integrations/zapier-token")
            .then((r) => setZapierUrl(r.data.webhook_url))
            .catch(() => {})
            .finally(() => setZapierLoading(false));
    }, []);

    const regenZapierToken = async () => {
        if (!confirm("This will invalidate your current Zapier webhook URL. Any existing Zaps must be updated. Continue?")) return;
        setZapierRegen(true);
        try {
            const r = await api.post("/integrations/zapier-token/regenerate");
            setZapierUrl(r.data.webhook_url);
            toast.success("Webhook URL regenerated");
        } catch { toast.error("Failed to regenerate"); }
        finally { setZapierRegen(false); }
    };

    const save = async () => {
        if (readOnly) return;
        setSaving(true);
        try {
            await api.put("/settings/integrations", form);
            toast.success("Integration settings saved");
            onSaved?.();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to save");
        } finally {
            setSaving(false);
        }
    };

    const testWebhook = async () => {
        if (!form.cg1_webhook_url) { toast.error("Set a webhook URL first"); return; }
        setTesting(true);
        try {
            const r = await api.post("/settings/integrations/cg1/test", form);
            const status = r.data?.status;
            const code = r.data?.status_code;
            if (status === "sent") toast.success(`Webhook fired OK (HTTP ${code})`);
            else toast.error(`Webhook ${status}: ${r.data?.error || `HTTP ${code}`}`);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Test failed");
        } finally {
            setTesting(false);
        }
    };

    return (
        <div className="max-w-2xl space-y-6" data-testid="integrations-section">
            <div>
                <div className="flex items-center gap-2">
                    <Plug size={18} weight="duotone" className="text-brand-primary" />
                    <h2 className="font-heading text-2xl tracking-tight">Outbound Integrations</h2>
                </div>
                <p className="text-sm text-ink-muted mt-1">Push hired candidates to other apps automatically.</p>
            </div>

            <div className="surface p-5 space-y-4">
                <div className="flex items-start justify-between gap-3">
                    <div>
                        <div className="font-heading text-lg flex items-center gap-2">
                            CG1 — Hire Webhook
                            <span className="text-[10px] uppercase tracking-widest text-ink-muted px-1.5 py-0.5 rounded bg-surface-active">POST</span>
                        </div>
                        <p className="text-xs text-ink-muted mt-1 max-w-md">
                            Fires <code className="text-ink">POST {"{url}"}</code> with{" "}
                            <code className="text-ink">{"{first_name, last_name, email, phone, appointment_at, hired_at}"}</code>{" "}
                            whenever you click <span className="text-ink font-semibold">Hire</span> on a candidate's form responses.
                        </p>
                    </div>
                    <label className="flex items-center gap-2 cursor-pointer select-none">
                        <input
                            type="checkbox"
                            data-testid="cg1-enabled"
                            disabled={readOnly}
                            checked={!!form.cg1_enabled}
                            onChange={(e) => setForm({ ...form, cg1_enabled: e.target.checked })}
                            className="accent-brand-primary"
                        />
                        <span className="text-xs">Enabled</span>
                    </label>
                </div>
                <div>
                    <label className="label-overline block mb-1.5">Webhook URL</label>
                    <input data-testid="cg1-webhook-url" type="url" readOnly={readOnly}
                        placeholder="https://your-field-app.example.com/hooks/hire"
                        className="input-dark" value={form.cg1_webhook_url || ""}
                        onChange={(e) => setForm({ ...form, cg1_webhook_url: e.target.value })} />
                </div>
                <div>
                    <label className="label-overline block mb-1.5">
                        Shared secret <span className="text-ink-dim normal-case font-normal">(sent as <code className="text-ink-muted">X-Webhook-Secret</code>)</span>
                    </label>
                    <input data-testid="cg1-webhook-secret" type="password" readOnly={readOnly}
                        placeholder={form.cg1_webhook_secret_set
                            ? "Saved. Leave blank to keep it, or type a new one"
                            : "any string — your CG1 app verifies this"}
                        className="input-dark" value={form.cg1_webhook_secret || ""}
                        onChange={(e) => setForm({ ...form, cg1_webhook_secret: e.target.value })} />
                </div>
                <div className="flex gap-2 pt-2">
                    <button onClick={testWebhook} disabled={testing || readOnly || !form.cg1_webhook_url}
                        data-testid="cg1-test-btn"
                        className="btn-secondary flex items-center gap-1.5 text-xs disabled:opacity-40">
                        {testing ? "Sending…" : "Send test payload"} <ArrowRight size={11} weight="bold" />
                    </button>
                    <button onClick={save} disabled={saving || readOnly} data-testid="cg1-save-btn"
                        className="btn-primary flex items-center gap-1.5 text-xs ml-auto disabled:opacity-40">
                        {saving ? "Saving…" : "Save settings"}
                    </button>
                </div>
            </div>

            <div className="text-[11px] text-ink-muted leading-relaxed">
                <strong className="text-ink">Status field on the candidate:</strong>{" "}
                After hire, each candidate gets a <code className="text-ink">cg1_webhook_status</code> field
                (sent / failed / skipped) so you can audit pushes from the candidate drawer.
            </div>

            {/* ── Intake webhook ── */}
            <div className="mt-2">
                <div className="flex items-center gap-2 mb-1">
                    <Link size={18} weight="duotone" className="text-brand-primary" />
                    <h2 className="font-heading text-2xl tracking-tight">Intake Webhook</h2>
                </div>
                <p className="text-sm text-ink-muted mb-4">
                    Send new applicants in from Zapier, Make or any system that can POST JSON. Each one is created
                    in the matching pipeline and screened exactly like an applicant from the apply form.
                </p>
                {zapierLoading ? (
                    <div className="text-xs text-ink-muted">Loading…</div>
                ) : (
                    <div className="surface p-5 space-y-4">
                        <div>
                            <label className="label-overline block mb-1.5">Webhook URL (keep it private — the token is the password)</label>
                            <div className="flex gap-2">
                                <input readOnly className="input-dark font-mono text-xs" value={zapierUrl}
                                    data-testid="intake-webhook-url" />
                                <button
                                    onClick={() => { navigator.clipboard?.writeText(zapierUrl); toast.success("Copied"); }}
                                    className="btn-secondary !py-1 !px-2.5 text-xs flex items-center gap-1.5">
                                    <Copy size={11} weight="bold" /> Copy
                                </button>
                            </div>
                        </div>
                        <div className="text-xs text-ink-muted leading-relaxed">
                            <div className="font-semibold text-ink text-sm mb-1.5">Fields</div>
                            <code className="text-ink">name</code> (or <code className="text-ink">first_name</code> +{" "}
                            <code className="text-ink">last_name</code>), <code className="text-ink">email</code>,{" "}
                            <code className="text-ink">phone</code>, optional <code className="text-ink">job_title</code>{" "}
                            (matched to a job to pick the pipeline), <code className="text-ink">pipeline_id</code>{" "}
                            and <code className="text-ink">resume</code> (plain text, parsed by AI).
                        </div>
                        <pre className="text-[11px] bg-surface-active rounded p-3 overflow-x-auto text-ink-muted">{`curl -X POST "<webhook URL>" \\
  -H "Content-Type: application/json" \\
  -d '{"name":"Rowan Sample","email":"rowan@example.com","phone":"+15550100100"}'`}</pre>
                        <div className="border-t border-strokes pt-4 flex items-center justify-between">
                            <span className="text-[11px] text-ink-muted">Regenerate if the URL has leaked. Existing Zaps must be updated.</span>
                            <button onClick={regenZapierToken} disabled={zapierRegen || readOnly}
                                className="btn-secondary !py-1 !px-2.5 text-xs flex items-center gap-1.5 disabled:opacity-40">
                                <ArrowsClockwise size={11} weight="bold" className={zapierRegen ? "animate-spin" : ""} /> Regenerate token
                            </button>
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}
