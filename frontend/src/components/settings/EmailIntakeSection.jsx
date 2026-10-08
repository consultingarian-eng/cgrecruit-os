import { useEffect, useState } from "react";
import api from "@/lib/api";
import { toast } from "sonner";
import { Switch } from "@/components/ui/switch";
import { EnvelopeOpen, Copy, ArrowsClockwise, Check, Warning, UserPlus, Trash } from "@phosphor-icons/react";

export default function EmailIntakeSection({ settings, onSaved }) {
    const [form, setForm] = useState(() => settings.email_intake || {
        enabled: true,
        inbound_domain: "inbox.example.com",
        default_pipeline_id: "",
        auto_dial: true,
    });
    const [setup, setSetup] = useState(null);
    const [log, setLog] = useState([]);
    const [failedResumes, setFailedResumes] = useState([]);
    const [pipelines, setPipelines] = useState([]);
    const [busy, setBusy] = useState(false);
    const [expandedLog, setExpandedLog] = useState(null);
    const [expandedBody, setExpandedBody] = useState(null);
    const [loadingBody, setLoadingBody] = useState(false);

    const openLogEntry = async (row) => {
        if (expandedLog === row.id) { setExpandedLog(null); setExpandedBody(null); return; }
        setExpandedLog(row.id);
        setExpandedBody(null);
        setLoadingBody(true);
        try {
            const r = await api.get(`/email-intake/log/${row.id}`);
            setExpandedBody(r.data);
        } catch { setExpandedBody(row); }
        finally { setLoadingBody(false); }
    };

    const upd = (k, v) => setForm({ ...form, [k]: v });

    const load = async () => {
        try {
            const [setupR, logR, pipelinesR, failedR] = await Promise.all([
                api.get("/email-intake/setup"),
                api.get("/email-intake/log"),
                api.get("/pipelines"),
                api.get("/email-intake/failed-resumes").catch(() => ({ data: [] })),
            ]);
            setSetup(setupR.data);
            setLog(logR.data);
            setPipelines(pipelinesR.data);
            setFailedResumes(failedR.data || []);
        } catch (e) {
            toast.error("Failed to load email intake config");
        }
    };

    const dismissFailedResume = async (logId) => {
        try {
            await api.post(`/email-intake/failed-resumes/${logId}/dismiss`);
            setFailedResumes((r) => r.filter((x) => x.id !== logId));
            toast.success("Dismissed");
        } catch { toast.error("Failed to dismiss"); }
    };

    useEffect(() => { load(); }, []);

    const save = async () => {
        setBusy(true);
        try {
            await api.put("/email-intake/settings", form);
            toast.success("Email intake settings saved");
            await load();
            onSaved && onSaved();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Save failed");
        } finally { setBusy(false); }
    };

    const copy = (text) => {
        navigator.clipboard.writeText(text);
        toast.success("Copied");
    };

    return (
        <div data-testid="email-intake-section" className="space-y-6 max-w-3xl">
            <header>
                <h1 className="font-heading text-2xl font-semibold flex items-center gap-2">
                    <EnvelopeOpen size={22} weight="duotone" className="text-brand-primary" /> Email-to-Resume Intake
                </h1>
                <p className="text-sm text-ink-muted mt-1">
                    Forward resumes to a dedicated inbox — we auto-parse the attachment, route to the right pipeline,
                    and queue an AI screening call. Powered by SendGrid Inbound Parse.
                </p>
            </header>

            <div className="surface p-5 space-y-4" data-testid="intake-toggles">
                <div className="flex items-center justify-between">
                    <div>
                        <div className="font-medium text-sm">Email intake enabled</div>
                        <div className="text-xs text-ink-muted">Master switch for inbound resume processing.</div>
                    </div>
                    <Switch
                        checked={!!form.enabled}
                        onCheckedChange={(v) => upd("enabled", v)}
                        data-testid="intake-enabled-switch"
                    />
                </div>
                <div className="flex items-center justify-between">
                    <div>
                        <div className="font-medium text-sm">Auto-dial after parsing</div>
                        <div className="text-xs text-ink-muted">As soon as a resume is parsed, schedule the AI screening call (within your call window).</div>
                    </div>
                    <Switch
                        checked={!!form.auto_dial}
                        onCheckedChange={(v) => upd("auto_dial", v)}
                        data-testid="intake-autodial-switch"
                    />
                </div>
                <div>
                    <div className="label-overline mb-1.5">Inbound subdomain</div>
                    <input
                        type="text"
                        className="input-dark"
                        value={form.inbound_domain || ""}
                        onChange={(e) => upd("inbound_domain", e.target.value)}
                        placeholder="inbox.example.com"
                        data-testid="intake-domain-input"
                    />
                    <div className="text-xs text-ink-muted mt-1">
                        Sub-domain you'll point an MX record at. Must NOT be your main domain — use something like <code>inbox.example.com</code>.
                    </div>
                </div>
                <div>
                    <div className="label-overline mb-1.5">Fallback pipeline</div>
                    <select
                        className="input-dark"
                        value={form.default_pipeline_id || ""}
                        onChange={(e) => upd("default_pipeline_id", e.target.value)}
                        data-testid="intake-default-pipeline-select"
                    >
                        <option value="">First pipeline (auto)</option>
                        {pipelines.map((p) => (
                            <option key={p.id} value={p.id}>{p.name}</option>
                        ))}
                    </select>
                    <div className="text-xs text-ink-muted mt-1">
                        Used when subject/body keywords don't clearly match a pipeline.
                    </div>
                </div>
                <button onClick={save} disabled={busy} className="btn-primary" data-testid="save-intake-btn">
                    {busy ? "Saving…" : "Save"}
                </button>
            </div>

            {setup && (
                <div className="surface p-5 space-y-4" data-testid="intake-setup">
                    {/* Collapsed by default — this is a DNS/SendGrid runbook
                        followed once, not something to re-read on every visit to
                        the intake log below. */}
                    <details>
                        <summary className="label-overline flex items-center gap-1.5 cursor-pointer hover:text-ink select-none">
                            <Warning size={11} weight="fill" /> One-time setup
                        </summary>
                        <ol className="text-sm text-ink-muted leading-relaxed list-decimal pl-5 space-y-1 mt-2">
                            <li>In your DNS, add an <strong>MX record</strong> on <code>{form.inbound_domain || "inbox.example.com"}</code> →
                                pointing to <code>mx.sendgrid.net</code> with priority <code>10</code>.</li>
                            <li>Wait 5–15 min for DNS propagation.</li>
                            <li>Go to SendGrid → <strong>Settings → Inbound Parse → Add Host & URL</strong>.</li>
                            <li>Receiving domain: <code>{form.inbound_domain || "inbox.example.com"}</code>.</li>
                            <li>Destination URL: paste the webhook URL below.</li>
                            <li>Leave <strong>"Post the raw, full MIME message"</strong> unchecked.</li>
                            <li>Save. Forward / send a resume to one of the addresses below — it auto-creates a candidate.</li>
                        </ol>

                        <div className="mt-4">
                            <div className="label-overline mb-1.5">Webhook URL (paste into SendGrid)</div>
                            <div className="flex items-center gap-2">
                                <code className="flex-1 px-3 py-2 bg-surface-active border border-strokes rounded text-xs break-all">
                                    {setup.webhook_url}
                                </code>
                                <button onClick={() => copy(setup.webhook_url)} className="px-3 py-2 rounded border border-strokes hover:border-strokes-focus" data-testid="copy-webhook-btn">
                                    <Copy size={14} />
                                </button>
                            </div>
                        </div>
                    </details>

                    <div>
                        <div className="label-overline mb-1.5">Inbound addresses</div>
                        <div className="space-y-2">
                            <AddressRow label="Auto-detect pipeline" address={`apply@${form.inbound_domain || setup.inbound_domain}`} onCopy={copy} />
                            {pipelines.map((p) => (
                                <AddressRow
                                    key={p.id}
                                    label={p.name}
                                    address={`${p.public_slug}@${form.inbound_domain || setup.inbound_domain}`}
                                    onCopy={copy}
                                />
                            ))}
                        </div>
                        <div className="text-xs text-ink-muted mt-2">
                            Send to <code>&lt;pipeline-slug&gt;@…</code> to land in that pipeline directly,
                            or <code>apply@…</code> to auto-detect from the subject/body.
                        </div>
                    </div>
                </div>
            )}

            <EmployeeAliasesSection pipelines={pipelines} domain={form.inbound_domain || setup?.inbound_domain || "inbox.example.com"} />

            <div className="surface p-5" data-testid="intake-log">
                <div className="flex items-center justify-between mb-3">
                    <div className="label-overline">Recent intake activity</div>
                    <button onClick={load} className="text-xs text-ink-muted hover:text-ink flex items-center gap-1" data-testid="refresh-intake-log-btn">
                        <ArrowsClockwise size={12} /> Refresh
                    </button>
                </div>
                {log.length === 0 ? (
                    <div className="text-sm text-ink-muted text-center py-6">
                        No emails received yet. Send a test resume to one of the addresses above to verify your setup.
                    </div>
                ) : (
                    <div className="overflow-x-auto">
                        <table className="w-full text-xs">
                            <thead className="text-ink-muted text-left">
                                <tr className="border-b border-strokes">
                                    <th className="pb-2">When</th>
                                    <th className="pb-2">From</th>
                                    <th className="pb-2">Subject</th>
                                    <th className="pb-2">Pipeline</th>
                                    <th className="pb-2">Result</th>
                                </tr>
                            </thead>
                            <tbody>
                                {log.map((row) => (
                                    <>
                                        <tr
                                            key={row.id}
                                            className="border-b border-strokes cursor-pointer hover:bg-surface-hover"
                                            onClick={() => openLogEntry(row)}
                                            data-testid={`intake-log-${row.id}`}
                                        >
                                            <td className="py-2 text-ink-muted">{new Date(row.received_at || row.created_at).toLocaleString()}</td>
                                            <td className="py-2">{row.from || "—"}</td>
                                            <td className="py-2 max-w-[200px] truncate">{row.subject || "—"}</td>
                                            <td className="py-2">{row.pipeline_name || "—"}</td>
                                            <td className="py-2">
                                                {row.status === "ingested" ? (
                                                    <span className="inline-flex items-center gap-1 text-brand-success">
                                                        <Check size={11} weight="bold" /> {row.candidate_name || "Candidate"}
                                                    </span>
                                                ) : (
                                                    <span className="inline-flex items-center gap-1 text-brand-warning">
                                                        {row.status} — {row.error || ""}
                                                    </span>
                                                )}
                                            </td>
                                        </tr>
                                        {expandedLog === row.id && (
                                            <tr key={`${row.id}-body`} className="border-b border-strokes bg-[#0B0B0F]">
                                                <td colSpan={5} className="py-3 px-2">
                                                    {loadingBody ? (
                                                        <div className="text-xs text-ink-muted">Loading…</div>
                                                    ) : expandedBody ? (
                                                        <div className="space-y-2">
                                                            <div className="text-[11px] text-ink-muted">
                                                                <span className="font-semibold text-ink">From:</span> {expandedBody.from}
                                                                {" · "}<span className="font-semibold text-ink">To:</span> {expandedBody.to}
                                                            </div>
                                                            {(expandedBody.body_text || expandedBody.body_html) ? (
                                                                <pre className="whitespace-pre-wrap text-[11px] text-ink bg-[#0E0E11] border border-strokes rounded p-3 max-h-60 overflow-auto font-mono">
                                                                    {expandedBody.body_text || expandedBody.body_html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim()}
                                                                </pre>
                                                            ) : (
                                                                <div className="text-xs text-ink-muted italic">No body text stored (email arrived before this feature was added).</div>
                                                            )}
                                                        </div>
                                                    ) : null}
                                                </td>
                                            </tr>
                                        )}
                                    </>
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}
            </div>

            {/* Failed resumes bucket */}
            <div className="surface p-5">
                <div className="flex items-center justify-between mb-4">
                    <div>
                        <h3 className="font-heading text-lg flex items-center gap-2">
                            <Warning size={16} weight="duotone" className="text-brand-warning" />
                            Failed resumes
                            {failedResumes.length > 0 && (
                                <span className="text-[10px] font-semibold uppercase tracking-widest px-1.5 py-0.5 rounded bg-brand-warning/15 text-brand-warning">
                                    {failedResumes.length}
                                </span>
                            )}
                        </h3>
                        <p className="text-xs text-ink-muted mt-1 max-w-md">
                            Inbound emails where the resume couldn't be parsed (no extractable name / phone / email or no attachment found).
                        </p>
                    </div>
                </div>

                {failedResumes.length === 0 ? (
                    <div className="text-xs text-ink-muted italic" data-testid="failed-resumes-empty">
                        No failed resumes — every parse has succeeded so far.
                    </div>
                ) : (
                    <div className="space-y-2" data-testid="failed-resumes-list">
                        {failedResumes.map((row) => (
                            <div key={row.id} className="border border-strokes rounded p-3 text-xs space-y-1.5" data-testid={`failed-resume-${row.id}`}>
                                <div className="flex items-center justify-between gap-2">
                                    <div>
                                        <span className="font-semibold">{row.from || "unknown sender"}</span>
                                        <span className="text-ink-muted"> · {new Date(row.created_at).toLocaleString()}</span>
                                    </div>
                                    <span className="text-[10px] uppercase tracking-widest px-1.5 py-0.5 rounded bg-brand-danger/15 text-brand-danger">
                                        {row.status}
                                    </span>
                                </div>
                                {row.subject && (
                                    <div className="text-ink-muted truncate">Subject: {row.subject}</div>
                                )}
                                {row.error && (
                                    <div className="text-ink-muted">Reason: {row.error}</div>
                                )}
                                {row.attachment_filename && (
                                    <div className="text-ink-muted">Attachment: {row.attachment_filename}</div>
                                )}
                                {row.resume_text_excerpt && (
                                    <details className="text-ink-muted">
                                        <summary className="cursor-pointer text-brand-primary hover:underline">View extracted text</summary>
                                        <pre className="whitespace-pre-wrap mt-2 p-2 bg-[#0B0B0F] border border-strokes rounded max-h-40 overflow-auto">{row.resume_text_excerpt}</pre>
                                    </details>
                                )}
                                <div className="flex justify-end gap-2 pt-1">
                                    <button
                                        onClick={() => dismissFailedResume(row.id)}
                                        data-testid={`dismiss-failed-${row.id}`}
                                        className="text-[11px] px-2 py-0.5 rounded border border-strokes hover:border-strokes-focus text-ink-muted"
                                    >
                                        Dismiss
                                    </button>
                                </div>
                            </div>
                        ))}
                    </div>
                )}
            </div>
        </div>
    );
}

function EmployeeAliasesSection({ pipelines, domain }) {
    const [selectedPipeline, setSelectedPipeline] = useState("");
    const [aliases, setAliases] = useState([]);
    const [newName, setNewName] = useState("");
    const [newSlug, setNewSlug] = useState("");
    const [busy, setBusy] = useState(false);

    const loadAliases = async (pid) => {
        if (!pid) return setAliases([]);
        try {
            const r = await api.get(`/email-intake/aliases?pipeline_id=${pid}`);
            setAliases(r.data);
        } catch { setAliases([]); }
    };

    useEffect(() => { loadAliases(selectedPipeline); }, [selectedPipeline]);

    const suggestSlug = (name) => {
        const slug = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
        setNewSlug(slug);
    };

    const add = async () => {
        if (!selectedPipeline) return toast.warning("Select a pipeline first");
        if (!newName.trim() || !newSlug.trim()) return toast.warning("Name and slug are required");
        setBusy(true);
        try {
            await api.post("/email-intake/aliases", {
                pipeline_id: selectedPipeline,
                employee_name: newName.trim(),
                slug: newSlug.trim(),
            });
            setNewName(""); setNewSlug("");
            await loadAliases(selectedPipeline);
            toast.success("Alias created");
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to create alias");
        } finally { setBusy(false); }
    };

    const remove = async (id) => {
        try {
            await api.delete(`/email-intake/aliases/${id}`);
            setAliases((a) => a.filter((x) => x.id !== id));
            toast.success("Alias removed");
        } catch { toast.error("Failed to remove"); }
    };

    const copy = (text) => { navigator.clipboard.writeText(text); toast.success("Copied"); };

    return (
        <div className="surface p-5 space-y-4">
            <div>
                <h3 className="font-heading text-lg flex items-center gap-2 mb-0.5">
                    <UserPlus size={16} weight="duotone" className="text-brand-primary" /> Per-Employee Email Addresses
                </h3>
                <p className="text-xs text-ink-muted">
                    Give each employee their own intake address. Resumes sent to it land in the right pipeline and show "Ref: [Employee]" on the candidate card.
                </p>
            </div>

            <div>
                <label className="label-overline block mb-1.5">Select pipeline</label>
                <select className="input-dark" value={selectedPipeline} onChange={(e) => setSelectedPipeline(e.target.value)}>
                    <option value="">— Choose a pipeline —</option>
                    {pipelines.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select>
            </div>

            {selectedPipeline && (
                <>
                    {aliases.length > 0 && (
                        <div className="space-y-2">
                            {aliases.map((a) => (
                                <div key={a.id} className="flex items-center gap-2">
                                    <div className="text-xs text-ink-muted w-32 flex-shrink-0 truncate">{a.employee_name}</div>
                                    <code className="flex-1 px-2.5 py-1.5 bg-surface-active border border-strokes rounded text-xs truncate">{a.email}</code>
                                    <button onClick={() => copy(a.email)} className="px-2 py-1.5 rounded border border-strokes hover:border-strokes-focus flex-shrink-0">
                                        <Copy size={12} />
                                    </button>
                                    <button onClick={() => remove(a.id)} className="px-2 py-1.5 rounded border border-strokes hover:border-brand-danger text-ink-muted hover:text-brand-danger flex-shrink-0">
                                        <Trash size={12} />
                                    </button>
                                </div>
                            ))}
                        </div>
                    )}

                    <div className="border-t border-strokes pt-4 space-y-3">
                        <div className="label-overline">Add new employee address</div>
                        <div className="grid grid-cols-2 gap-2.5">
                            <div>
                                <label className="label-overline block mb-1">Employee name *</label>
                                <input
                                    className="input-dark"
                                    placeholder="e.g. Sam Kestrel"
                                    value={newName}
                                    onChange={(e) => { setNewName(e.target.value); suggestSlug(e.target.value); }}
                                />
                            </div>
                            <div>
                                <label className="label-overline block mb-1">Slug (unique) *</label>
                                <input
                                    className="input-dark font-mono"
                                    placeholder="e.g. sam-downtown"
                                    value={newSlug}
                                    onChange={(e) => setNewSlug(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ""))}
                                />
                            </div>
                        </div>
                        {newSlug && (
                            <div className="text-xs text-ink-muted">
                                Address: <code className="text-ink">{newSlug}@{domain}</code>
                            </div>
                        )}
                        <button onClick={add} disabled={busy} className="btn-primary text-sm !py-1.5">
                            {busy ? "Adding…" : "Add address"}
                        </button>
                    </div>
                </>
            )}
        </div>
    );
}

function AddressRow({ label, address, onCopy }) {
    return (
        <div className="flex items-center gap-2" data-testid={`intake-address-${label.toLowerCase().replace(/\s+/g, "-")}`}>
            <div className="text-xs text-ink-muted w-44 flex-shrink-0">{label}</div>
            <code className="flex-1 px-2.5 py-1.5 bg-surface-active border border-strokes rounded text-xs">{address}</code>
            <button onClick={() => onCopy(address)} className="px-2 py-1.5 rounded border border-strokes hover:border-strokes-focus">
                <Copy size={12} />
            </button>
        </div>
    );
}
