import { useEffect, useState } from "react";
import api from "@/lib/api";
import { toast } from "sonner";
import { confirmDialog, promptDialog } from "@/components/ConfirmDialog";
import { UsersThree, Plus, Trash, CheckCircle, X, PencilSimple } from "@phosphor-icons/react";

const ROLE_LABELS = {
    recruiter: { label: "Recruiter", color: "bg-[rgba(139,92,246,0.14)] text-[#A78BFA]" },
    viewer: { label: "View Only", color: "bg-amber-500/15 text-amber-400" },
};

/**
 * Super-admin-only panel for managing sub-accounts.
 * Supports two roles:
 *   recruiter — can move/hire/archive candidates in assigned pipelines
 *   viewer    — read-only; sees only candidates they personally added
 */
export default function TeamSection() {
    const [users, setUsers] = useState([]);
    const [pipelines, setPipelines] = useState([]);
    const [loading, setLoading] = useState(true);
    const [creating, setCreating] = useState(false);
    const [form, setForm] = useState({
        email: "", password: "", name: "", pipeline_ids: [], role: "recruiter",
    });

    const load = async () => {
        try {
            const [u, p] = await Promise.all([
                api.get("/admin/users"),
                api.get("/pipelines"),
            ]);
            setUsers(u.data || []);
            setPipelines(p.data || []);
        } catch {
            toast.error("Failed to load team");
        } finally { setLoading(false); }
    };

    useEffect(() => { load(); }, []);

    const togglePipeline = (id) => {
        setForm((f) => ({
            ...f,
            pipeline_ids: f.pipeline_ids.includes(id)
                ? f.pipeline_ids.filter((x) => x !== id)
                : [...f.pipeline_ids, id],
        }));
    };

    const submit = async (e) => {
        e.preventDefault();
        if (!form.email || !form.password || form.pipeline_ids.length === 0) {
            toast.error("Email, password, and at least one pipeline required");
            return;
        }
        if (form.password.length < 6) {
            toast.error("Password must be 6+ characters");
            return;
        }
        try {
            await api.post("/admin/users", form);
            const roleLabel = ROLE_LABELS[form.role]?.label || form.role;
            toast.success(`${roleLabel} account created for ${form.email}`);
            setForm({ email: "", password: "", name: "", pipeline_ids: [], role: "recruiter" });
            setCreating(false);
            load();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Create failed");
        }
    };

    const remove = async (u) => {
        if (!(await confirmDialog({ title: `Delete ${u.email}?`, description: "This cannot be undone.", confirmLabel: "Delete", destructive: true }))) return;
        try {
            await api.delete(`/admin/users/${u.id}`);
            toast.success("Deleted");
            load();
        } catch {
            toast.error("Delete failed");
        }
    };

    const resetPassword = async (u) => {
        const pw = await promptDialog({ title: `New password for ${u.email}`, description: "Minimum 6 characters.", inputType: "text", confirmLabel: "Reset password" });
        if (!pw || pw.length < 6) { if (pw) toast.error("Password must be at least 6 characters"); return; }
        try {
            await api.put(`/admin/users/${u.id}`, { password: pw });
            toast.success("Password reset");
        } catch {
            toast.error("Reset failed");
        }
    };

    const toggleRole = async (u) => {
        const newRole = u.role === "viewer" ? "recruiter" : "viewer";
        try {
            await api.put(`/admin/users/${u.id}`, { role: newRole });
            toast.success(`Role changed to ${ROLE_LABELS[newRole]?.label}`);
            load();
        } catch {
            toast.error("Role change failed");
        }
    };

    // Inline per-member pipeline editor: { userId, ids: [...] } while editing.
    const [pipeEdit, setPipeEdit] = useState(null);
    const [pipeSaving, setPipeSaving] = useState(false);

    const startPipeEdit = (u) => setPipeEdit({ userId: u.id, ids: [...(u.pipeline_ids || [])] });
    const togglePipeEditId = (id) =>
        setPipeEdit((s) => s && ({
            ...s,
            ids: s.ids.includes(id) ? s.ids.filter((x) => x !== id) : [...s.ids, id],
        }));
    const savePipeEdit = async () => {
        if (!pipeEdit) return;
        if (pipeEdit.ids.length === 0) {
            toast.error("Assign at least one pipeline — remove the account instead if they should lose all access");
            return;
        }
        setPipeSaving(true);
        try {
            await api.put(`/admin/users/${pipeEdit.userId}`, { pipeline_ids: pipeEdit.ids });
            toast.success("Pipeline access updated — takes effect on their next page refresh");
            setPipeEdit(null);
            load();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Update failed");
        } finally { setPipeSaving(false); }
    };

    if (loading) return <div className="text-sm text-ink-muted" data-testid="team-loading">Loading team…</div>;

    return (
        <div className="max-w-3xl" data-testid="team-section">
            <div className="flex items-start justify-between mb-5">
                <div>
                    <h2 className="font-heading text-xl font-semibold flex items-center gap-2">
                        <UsersThree size={18} weight="duotone" className="text-brand-primary" />
                        Team
                    </h2>
                    <p className="text-xs text-ink-muted mt-0.5">
                        Create logins for office managers and recruiters. Assign roles — <strong>Recruiter</strong> can move/hire candidates in their pipelines; <strong>View Only</strong> sees only candidates they personally added (read-only).
                    </p>
                </div>
                {!creating && (
                    <button
                        onClick={() => setCreating(true)}
                        data-testid="new-recruiter-btn"
                        className="btn-primary flex items-center gap-1.5 !py-1.5 !px-3 text-xs shrink-0"
                    >
                        <Plus size={12} weight="bold" /> Add member
                    </button>
                )}
            </div>

            {creating && (
                <form
                    onSubmit={submit}
                    data-testid="new-recruiter-form"
                    className="surface p-5 mb-6 space-y-3"
                >
                    <div className="flex items-center justify-between mb-2">
                        <h3 className="font-medium text-sm">New team member</h3>
                        <button type="button" onClick={() => setCreating(false)} className="text-ink-muted hover:text-ink">
                            <X size={14} />
                        </button>
                    </div>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                        <div>
                            <label className="label-overline mb-1 block">Email</label>
                            <input
                                type="email"
                                required
                                className="input-dark"
                                data-testid="new-recruiter-email"
                                placeholder="recruiter@example.com"
                                value={form.email}
                                onChange={(e) => setForm((f) => ({ ...f, email: e.target.value }))}
                            />
                        </div>
                        <div>
                            <label className="label-overline mb-1 block">Display name (optional)</label>
                            <input
                                type="text"
                                className="input-dark"
                                data-testid="new-recruiter-name"
                                placeholder="Riverside Recruiter"
                                value={form.name}
                                onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
                            />
                        </div>
                    </div>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                        <div>
                            <label className="label-overline mb-1 block">Temporary password</label>
                            <input
                                type="text"
                                required
                                minLength={6}
                                className="input-dark"
                                data-testid="new-recruiter-password"
                                placeholder="at least 6 characters"
                                value={form.password}
                                onChange={(e) => setForm((f) => ({ ...f, password: e.target.value }))}
                            />
                        </div>
                        <div>
                            <label className="label-overline mb-1 block">Role</label>
                            <select
                                className="input-dark"
                                data-testid="new-recruiter-role"
                                value={form.role}
                                onChange={(e) => setForm((f) => ({ ...f, role: e.target.value }))}
                            >
                                <option value="recruiter">Recruiter — can move & hire candidates</option>
                                <option value="viewer">View Only — sees only their own added candidates</option>
                            </select>
                        </div>
                    </div>
                    <div>
                        <label className="label-overline mb-1 block">Assigned pipelines</label>
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                            {pipelines.map((p) => {
                                const selected = form.pipeline_ids.includes(p.id);
                                return (
                                    <label
                                        key={p.id}
                                        className={`flex items-center gap-2 px-3 py-2 rounded-md border cursor-pointer transition-colors ${
                                            selected
                                                ? "border-brand-primary bg-[rgba(139,92,246,0.08)]"
                                                : "border-strokes hover:border-[#4A4D59]"
                                        }`}
                                        data-testid={`new-recruiter-pipeline-${p.id}`}
                                    >
                                        <input
                                            type="checkbox"
                                            checked={selected}
                                            onChange={() => togglePipeline(p.id)}
                                            className="accent-brand-primary"
                                        />
                                        <span className="text-sm">{p.name}</span>
                                        {selected && <CheckCircle size={12} weight="fill" className="ml-auto text-brand-primary" />}
                                    </label>
                                );
                            })}
                            {pipelines.length === 0 && (
                                <div className="text-xs text-ink-muted">No pipelines yet — create one first.</div>
                            )}
                        </div>
                    </div>
                    <div className="flex items-center gap-2 pt-2">
                        <button type="submit" className="btn-primary text-xs !py-1.5 !px-3" data-testid="submit-new-recruiter">
                            Create account
                        </button>
                        <button type="button" onClick={() => setCreating(false)} className="btn-secondary text-xs !py-1.5 !px-3">
                            Cancel
                        </button>
                    </div>
                </form>
            )}

            <div className="surface overflow-hidden" data-testid="team-list">
                <table className="w-full text-sm">
                    <thead>
                        <tr className="border-b border-strokes text-left text-[11px] uppercase tracking-widest text-ink-muted">
                            <th className="px-4 py-2.5 font-medium">Email</th>
                            <th className="px-4 py-2.5 font-medium">Name</th>
                            <th className="px-4 py-2.5 font-medium">Role</th>
                            <th className="px-4 py-2.5 font-medium">Pipelines</th>
                            <th className="px-4 py-2.5 font-medium text-right">Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        {users.map((u) => {
                            const roleInfo = ROLE_LABELS[u.role] || ROLE_LABELS.recruiter;
                            return (
                                <tr key={u.id} className="border-b border-strokes last:border-b-0 hover:bg-surface-hover" data-testid={`team-row-${u.id}`}>
                                    <td className="px-4 py-3 font-mono text-xs">{u.email}</td>
                                    <td className="px-4 py-3 text-ink-muted">{u.name || "—"}</td>
                                    <td className="px-4 py-3">
                                        <span className={`text-[10px] px-1.5 py-0.5 rounded font-semibold ${roleInfo.color}`}>
                                            {roleInfo.label}
                                        </span>
                                    </td>
                                    <td className="px-4 py-3">
                                        {pipeEdit?.userId === u.id ? (
                                            <div className="space-y-1.5" data-testid={`pipe-edit-${u.id}`}>
                                                <div className="flex flex-wrap gap-1.5">
                                                    {pipelines.map((p) => {
                                                        const selected = pipeEdit.ids.includes(p.id);
                                                        return (
                                                            <label
                                                                key={p.id}
                                                                className={`flex items-center gap-1.5 px-2 py-1 rounded border cursor-pointer text-[11px] transition-colors ${
                                                                    selected
                                                                        ? "border-brand-primary bg-[rgba(139,92,246,0.08)] text-ink"
                                                                        : "border-strokes text-ink-muted hover:border-[#4A4D59]"
                                                                }`}
                                                                data-testid={`pipe-edit-${u.id}-${p.id}`}
                                                            >
                                                                <input
                                                                    type="checkbox"
                                                                    checked={selected}
                                                                    onChange={() => togglePipeEditId(p.id)}
                                                                    className="accent-brand-primary"
                                                                />
                                                                {p.name}
                                                            </label>
                                                        );
                                                    })}
                                                </div>
                                                <div className="flex items-center gap-2">
                                                    <button
                                                        onClick={savePipeEdit}
                                                        disabled={pipeSaving}
                                                        data-testid={`pipe-edit-save-${u.id}`}
                                                        className="btn-primary !py-0.5 !px-2 text-[11px]"
                                                    >
                                                        {pipeSaving ? "Saving…" : "Save"}
                                                    </button>
                                                    <button
                                                        onClick={() => setPipeEdit(null)}
                                                        className="text-[11px] text-ink-muted hover:text-ink"
                                                    >
                                                        Cancel
                                                    </button>
                                                </div>
                                            </div>
                                        ) : (
                                            <div className="flex flex-wrap items-center gap-1 group/pipes">
                                                {(u.pipeline_names || []).map((n) => (
                                                    <span key={n} className="text-[10px] px-1.5 py-0.5 rounded bg-[rgba(139,92,246,0.14)] text-[#A78BFA]">
                                                        {n}
                                                    </span>
                                                ))}
                                                <button
                                                    onClick={() => startPipeEdit(u)}
                                                    data-testid={`pipe-edit-btn-${u.id}`}
                                                    title="Edit pipeline access"
                                                    className="text-ink-muted hover:text-brand-primary p-0.5"
                                                >
                                                    <PencilSimple size={11} />
                                                </button>
                                            </div>
                                        )}
                                    </td>
                                    <td className="px-4 py-3 text-right space-x-3 whitespace-nowrap">
                                        <button
                                            onClick={() => toggleRole(u)}
                                            data-testid={`toggle-role-${u.id}`}
                                            title={u.role === "viewer" ? "Promote to Recruiter" : "Demote to View Only"}
                                            className="text-[11px] text-ink-muted hover:text-brand-primary"
                                        >
                                            {u.role === "viewer" ? "→ Recruiter" : "→ View Only"}
                                        </button>
                                        <button onClick={() => resetPassword(u)} data-testid={`reset-password-${u.id}`} className="text-[11px] text-ink-muted hover:text-brand-primary">
                                            Reset pw
                                        </button>
                                        <button onClick={() => remove(u)} data-testid={`delete-recruiter-${u.id}`} className="text-[11px] text-brand-danger hover:underline inline-flex items-center gap-1">
                                            <Trash size={10} /> Remove
                                        </button>
                                    </td>
                                </tr>
                            );
                        })}
                        {users.length === 0 && (
                            <tr>
                                <td colSpan={5} className="px-4 py-12 text-center text-ink-muted text-sm">
                                    No team members yet. Click "Add member" to create the first one.
                                </td>
                            </tr>
                        )}
                    </tbody>
                </table>
            </div>
        </div>
    );
}
