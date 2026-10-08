import { useEffect, useRef, useState } from "react";
import api from "@/lib/api";
import { toast } from "sonner";
import { Switch } from "@/components/ui/switch";
import { usePipeline } from "@/lib/pipeline";

export default function SmartScoringSection() {
    const { activePipelineId, pipelines } = usePipeline();
    const [jobs, setJobs] = useState([]);
    const [active, setActive] = useState(null);
    const [busy, setBusy] = useState(false);

    // The office the state on screen belongs to. A /jobs response that resolves
    // after the recruiter has switched offices is the previous office's and must
    // be dropped: applied, it would repopulate the list under the new office's
    // badge and Save would PUT by id, which the backend matches on id + user_id
    // only — the write lands on the other office.
    const loadedFor = useRef(activePipelineId);

    const refresh = async () => {
        const officeId = activePipelineId;
        if (!officeId) return;
        const r = await api.get("/jobs", { params: { pipeline_id: officeId } });
        if (loadedFor.current !== officeId) return;
        setJobs(r.data);
        // Membership, not a null check: the ad on screen has to be one this
        // office actually returned, whatever order the responses landed in.
        setActive((prev) => (prev && r.data.some((j) => j.id === prev.id) ? prev : r.data[0] || null));
    };

    useEffect(() => { loadedFor.current = activePipelineId; setActive(null); setJobs([]); }, [activePipelineId]);

    useEffect(() => { refresh(); /* eslint-disable-next-line */ }, [activePipelineId]);

    const upd = (k, v) => setActive({ ...active, [k]: v });

    const save = async () => {
        if (!active) return;
        const officeId = activePipelineId;
        setBusy(true);
        try {
            const payload = { ...active };
            delete payload.id;
            delete payload.user_id;
            delete payload.created_at;
            const r = await api.put(`/jobs/${active.id}`, payload);
            await refresh();
            // The write landed, but if the office changed while it was in flight
            // this ad is no longer ours to put back on screen.
            if (loadedFor.current === officeId) setActive(r.data);
            toast.success("Job ad saved");
        } catch { toast.error("Failed to save"); }
        finally { setBusy(false); }
    };

    const create = async () => {
        const officeId = activePipelineId;
        if (!officeId) return;
        try {
            const r = await api.post("/jobs", {
                pipeline_id: officeId,
                title: "New Job Ad",
                category: "",
                description: "",
                is_active: true,
            });
            await refresh();
            if (loadedFor.current === officeId) setActive(r.data);
        } catch { toast.error("Failed"); }
    };

    const pipelineName = pipelines.find((p) => p.id === activePipelineId)?.name;

    return (
        <div className="max-w-5xl space-y-6" data-testid="smart-scoring-section">
            <div>
                <div className="flex items-center gap-2 flex-wrap">
                    <h2 className="font-heading text-2xl font-bold tracking-tight">Job Ads</h2>
                    {pipelineName && (
                        <span
                            data-testid="job-ads-office-badge"
                            className="text-[9px] uppercase tracking-widest font-bold px-1.5 py-0.5 rounded bg-[rgba(139,92,246,0.20)] text-brand-primary"
                        >
                            {pipelineName}
                        </span>
                    )}
                </div>
                <p className="text-sm text-ink-muted mt-1">
                    The ad each applicant applied to. Claude matches candidate resumes
                    against the job description below.
                </p>
            </div>

            <div className="grid grid-cols-12 gap-4">
                <div className="col-span-12 md:col-span-3 surface p-2 space-y-1">
                    {jobs.map((j) => (
                        <button
                            key={j.id}
                            onClick={() => setActive(j)}
                            data-testid={`job-tab-${j.id}`}
                            className={`w-full text-left px-3 py-2 rounded text-sm transition-colors ${
                                active?.id === j.id ? "bg-surface-active text-ink" : "text-ink-muted hover:bg-surface-hover"
                            }`}
                        >
                            <div className="truncate">{j.title || "Untitled"}</div>
                            <div className="text-[10px] uppercase tracking-widest text-ink-dim mt-0.5">
                                {j.is_active ? "Running" : "Paused"}
                            </div>
                        </button>
                    ))}
                    <button onClick={create} data-testid="add-job-btn" className="w-full text-left px-3 py-2 rounded text-sm text-brand-primary hover:bg-surface-hover">
                        + New Job Ad
                    </button>
                </div>

                <div className="col-span-12 md:col-span-9 surface p-5 space-y-4">
                    {!active ? (
                        <div className="text-sm text-ink-muted">Select or create a job ad.</div>
                    ) : (
                        <>
                            <div className="flex items-center justify-between">
                                <div>
                                    <div className="label-overline">Job Ad</div>
                                    <div className="font-heading text-lg font-semibold">{active.title || "Untitled"}</div>
                                </div>
                                <label className="flex items-center gap-2 text-xs">
                                    <span className="text-ink-muted">Ad is running</span>
                                    <Switch checked={!!active.is_active} onCheckedChange={(v) => upd("is_active", v)} data-testid="job-active-switch" />
                                </label>
                            </div>
                            <Field label="Job Title">
                                <input data-testid="job-title-input" className="input-dark" value={active.title || ""} onChange={(e) => upd("title", e.target.value)} />
                            </Field>
                            <Field label="Category">
                                <input data-testid="job-category-input" className="input-dark" value={active.category || ""} onChange={(e) => upd("category", e.target.value)} />
                            </Field>
                            <Field label="Job Description (used for AI matching)">
                                <textarea data-testid="job-description-textarea" rows={8} className="input-dark" value={active.description || ""} onChange={(e) => upd("description", e.target.value)} />
                            </Field>
                            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                                <Field label="Postcode">
                                    <input data-testid="job-postcode-input" className="input-dark" value={active.postcode || ""} onChange={(e) => upd("postcode", e.target.value)} />
                                </Field>
                                <Field label="City">
                                    <input data-testid="job-city-input" className="input-dark" value={active.city || ""} onChange={(e) => upd("city", e.target.value)} />
                                </Field>
                                <Field label="Region">
                                    <input data-testid="job-region-input" className="input-dark" value={active.region || ""} onChange={(e) => upd("region", e.target.value)} />
                                </Field>
                                <Field label="Country">
                                    <input data-testid="job-country-input" className="input-dark" value={active.country || ""} onChange={(e) => upd("country", e.target.value)} />
                                </Field>
                            </div>
                            <button onClick={save} disabled={busy} data-testid="save-job-btn" className="btn-primary">
                                {busy ? "Saving…" : "Save Job Ad"}
                            </button>
                        </>
                    )}
                </div>
            </div>
        </div>
    );
}

function Field({ label, children }) {
    return (
        <div>
            <label className="label-overline block mb-1.5">{label}</label>
            {children}
        </div>
    );
}
