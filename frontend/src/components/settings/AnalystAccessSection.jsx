import { useEffect, useState } from "react";
import api from "@/lib/api";
import { toast } from "sonner";
import { confirmDialog } from "@/components/ConfirmDialog";
import { ChartLineUp, Plus, Trash, Eye, EyeSlash, ArrowCounterClockwise } from "@phosphor-icons/react";

export default function AnalystAccessSection() {
    const [analysts, setAnalysts] = useState([]);
    const [pipelines, setPipelines] = useState([]);
    const [loading, setLoading] = useState(true);
    const [creating, setCreating] = useState(false);
    const [form, setForm] = useState({ name: "", email: "", password: "", pipeline_ids: [] });
    const [showPassword, setShowPassword] = useState(false);
    const [revealed, setRevealed] = useState({});

    const load = async () => {
        try {
            const [a, p] = await Promise.all([
                api.get("/analyst-users"),
                api.get("/pipelines"),
            ]);
            setAnalysts(a.data);
            setPipelines(p.data);
        } catch {
            toast.error("Failed to load analyst accounts");
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => { load(); }, []);

    const togglePipeline = (pid) => {
        setForm((f) => ({
            ...f,
            pipeline_ids: f.pipeline_ids.includes(pid)
                ? f.pipeline_ids.filter((id) => id !== pid)
                : [...f.pipeline_ids, pid],
        }));
    };

    const create = async () => {
        if (!form.name.trim() || !form.email.trim()) {
            toast.error("Name and email are required");
            return;
        }
        try {
            const r = await api.post("/analyst-users", form);
            toast.success(`Analyst account created for ${r.data.name}`);
            if (r.data.generated_password) {
                setRevealed((prev) => ({ ...prev, [r.data.id]: r.data.generated_password }));
                toast.info(`Temporary password: ${r.data.generated_password}`, { duration: 12000 });
            }
            setForm({ name: "", email: "", password: "", pipeline_ids: [] });
            setCreating(false);
            load();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to create analyst");
        }
    };

    const resetPassword = async (id) => {
        try {
            const r = await api.patch(`/analyst-users/${id}`, { reset_password: true });
            const pw = r.data.generated_password;
            setRevealed((prev) => ({ ...prev, [id]: pw }));
            toast.success("Password reset", { duration: 8000 });
        } catch {
            toast.error("Failed to reset password");
        }
    };

    const updatePipelines = async (id, pipeline_ids) => {
        try {
            await api.patch(`/analyst-users/${id}`, { pipeline_ids });
            toast.success("Access updated");
            load();
        } catch {
            toast.error("Failed to update access");
        }
    };

    const remove = async (id, name) => {
        if (!(await confirmDialog({ title: `Remove analyst access for ${name}?`, description: "They will no longer be able to sign in.", confirmLabel: "Remove", destructive: true }))) return;
        try {
            await api.delete(`/analyst-users/${id}`);
            toast.success("Analyst account removed");
            load();
        } catch {
            toast.error("Failed to remove analyst");
        }
    };

    if (loading) return <div className="p-6 text-ink-muted text-sm">Loading…</div>;

    return (
        <div className="space-y-6">
            <div>
                <h2 className="font-heading text-lg font-bold">Analyst Access</h2>
                <p className="text-sm text-ink-muted mt-1">
                    Analyst accounts have read-only access to the Intelligence and Report views only.
                    They cannot access the kanban, settings, or any candidate data directly.
                    You can scope each analyst to specific offices (pipelines) or give them cross-office visibility.
                </p>
            </div>

            {/* Existing analysts */}
            {analysts.length > 0 && (
                <div className="space-y-3">
                    {analysts.map((a) => (
                        <AnalystRow
                            key={a.id}
                            analyst={a}
                            pipelines={pipelines}
                            revealedPassword={revealed[a.id]}
                            onResetPassword={() => resetPassword(a.id)}
                            onUpdatePipelines={(ids) => updatePipelines(a.id, ids)}
                            onDelete={() => remove(a.id, a.name)}
                        />
                    ))}
                </div>
            )}

            {analysts.length === 0 && !creating && (
                <div className="surface p-6 text-center text-ink-muted text-sm">
                    No analyst accounts yet. Create one to share a read-only reporting link.
                </div>
            )}

            {/* Create form */}
            {creating ? (
                <div className="surface p-5 space-y-4">
                    <div className="font-medium text-sm">New Analyst Account</div>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                        <div>
                            <label className="label-overline block mb-1">Name</label>
                            <input
                                className="input-dark"
                                placeholder="e.g. Partner reporting"
                                value={form.name}
                                onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
                            />
                        </div>
                        <div>
                            <label className="label-overline block mb-1">Email</label>
                            <input
                                type="email"
                                className="input-dark"
                                placeholder="analyst@example.com"
                                value={form.email}
                                onChange={(e) => setForm((f) => ({ ...f, email: e.target.value }))}
                            />
                        </div>
                    </div>
                    <div>
                        <label className="label-overline block mb-1">Password (leave blank to auto-generate)</label>
                        <div className="relative">
                            <input
                                type={showPassword ? "text" : "password"}
                                className="input-dark pr-10"
                                placeholder="Auto-generated if blank"
                                value={form.password}
                                onChange={(e) => setForm((f) => ({ ...f, password: e.target.value }))}
                            />
                            <button
                                type="button"
                                onClick={() => setShowPassword((v) => !v)}
                                className="absolute right-3 top-1/2 -translate-y-1/2 text-ink-muted hover:text-ink"
                            >
                                {showPassword ? <EyeSlash size={14} /> : <Eye size={14} />}
                            </button>
                        </div>
                    </div>
                    <div>
                        <label className="label-overline block mb-1.5">Office access (leave empty = all offices)</label>
                        <div className="flex flex-wrap gap-2">
                            {pipelines.map((p) => (
                                <button
                                    key={p.id}
                                    type="button"
                                    onClick={() => togglePipeline(p.id)}
                                    className={`px-3 py-1 rounded-full text-xs font-medium border transition-colors ${
                                        form.pipeline_ids.includes(p.id)
                                            ? "bg-brand-primary/20 border-brand-primary/40 text-brand-primary"
                                            : "bg-surface border-strokes text-ink-muted hover:text-ink"
                                    }`}
                                >
                                    {p.name}
                                </button>
                            ))}
                        </div>
                        {form.pipeline_ids.length === 0 && (
                            <p className="text-[11px] text-ink-muted mt-1.5">All offices visible</p>
                        )}
                    </div>
                    <div className="flex gap-2">
                        <button onClick={create} className="btn-primary !py-2 !px-4 text-sm">Create</button>
                        <button onClick={() => setCreating(false)} className="btn-secondary !py-2 !px-4 text-sm">Cancel</button>
                    </div>
                </div>
            ) : (
                <button
                    onClick={() => setCreating(true)}
                    className="btn-secondary flex items-center gap-2 !py-2 !px-4 text-sm"
                >
                    <Plus size={14} weight="bold" /> Add Analyst Account
                </button>
            )}
        </div>
    );
}

function AnalystRow({ analyst, pipelines, revealedPassword, onResetPassword, onUpdatePipelines, onDelete }) {
    const [expanded, setExpanded] = useState(false);
    const [localPipelineIds, setLocalPipelineIds] = useState(analyst.pipeline_ids || []);

    const toggle = (pid) => {
        setLocalPipelineIds((prev) =>
            prev.includes(pid) ? prev.filter((id) => id !== pid) : [...prev, pid]
        );
    };

    const hasPipelineChanges = JSON.stringify(localPipelineIds.sort()) !== JSON.stringify([...(analyst.pipeline_ids || [])].sort());

    const accessLabel = localPipelineIds.length === 0
        ? "All offices"
        : pipelines.filter((p) => localPipelineIds.includes(p.id)).map((p) => p.name).join(", ") || "—";

    return (
        <div className="surface border border-strokes rounded-lg overflow-hidden">
            <div
                className="flex items-center justify-between px-4 py-3 cursor-pointer hover:bg-surface-hover transition-colors"
                onClick={() => setExpanded((v) => !v)}
            >
                <div className="flex items-center gap-3 min-w-0">
                    <div className="p-1.5 rounded bg-brand-primary/10">
                        <ChartLineUp size={14} weight="duotone" className="text-brand-primary" />
                    </div>
                    <div className="min-w-0">
                        <div className="font-medium text-sm">{analyst.name}</div>
                        <div className="text-[11px] text-ink-muted truncate">{analyst.email}</div>
                    </div>
                </div>
                <div className="flex items-center gap-3 shrink-0 ml-4">
                    <span className="hidden sm:inline text-[11px] text-ink-muted">{accessLabel}</span>
                    <span className="text-[10px] uppercase tracking-widest px-2 py-0.5 rounded-full bg-brand-primary/15 border border-brand-primary/30 text-brand-primary font-semibold">
                        Analytics
                    </span>
                </div>
            </div>

            {expanded && (
                <div className="border-t border-strokes px-4 py-4 space-y-4">
                    {revealedPassword && (
                        <div className="flex items-center gap-2 p-3 rounded-lg bg-amber-500/10 border border-amber-500/20">
                            <span className="text-xs text-amber-400">Generated password:</span>
                            <code className="text-xs font-mono text-amber-300">{revealedPassword}</code>
                            <span className="text-[10px] text-amber-400/60 ml-1">— share this securely</span>
                        </div>
                    )}

                    <div>
                        <div className="label-overline mb-2">Office access</div>
                        <div className="flex flex-wrap gap-2 mb-2">
                            {pipelines.map((p) => (
                                <button
                                    key={p.id}
                                    type="button"
                                    onClick={() => toggle(p.id)}
                                    className={`px-3 py-1 rounded-full text-xs font-medium border transition-colors ${
                                        localPipelineIds.includes(p.id)
                                            ? "bg-brand-primary/20 border-brand-primary/40 text-brand-primary"
                                            : "bg-surface border-strokes text-ink-muted hover:text-ink"
                                    }`}
                                >
                                    {p.name}
                                </button>
                            ))}
                        </div>
                        {localPipelineIds.length === 0 && (
                            <p className="text-[11px] text-ink-muted">All offices visible</p>
                        )}
                        {hasPipelineChanges && (
                            <button
                                onClick={() => onUpdatePipelines(localPipelineIds)}
                                className="mt-2 btn-primary !py-1.5 !px-3 text-xs"
                            >
                                Save access
                            </button>
                        )}
                    </div>

                    <div className="flex items-center gap-3 pt-1">
                        <button
                            onClick={onResetPassword}
                            className="flex items-center gap-1.5 text-xs text-ink-muted hover:text-ink transition-colors"
                        >
                            <ArrowCounterClockwise size={13} /> Reset password
                        </button>
                        <button
                            onClick={onDelete}
                            className="flex items-center gap-1.5 text-xs text-brand-danger hover:opacity-80 transition-opacity"
                        >
                            <Trash size={13} /> Remove access
                        </button>
                    </div>
                </div>
            )}
        </div>
    );
}
