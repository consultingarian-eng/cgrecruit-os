import { useEffect, useState } from "react";
import api from "@/lib/api";
import { toast } from "sonner";
import { confirmDialog } from "@/components/ConfirmDialog";
import { usePipeline } from "@/lib/pipeline";
import { CardsThree, ArrowsMerge, X } from "@phosphor-icons/react";

/**
 * Duplicates page — surfaces cross-pipeline duplicate candidate groups so
 * recruiters can MERGE them before Olivia double-dials the same person.
 *
 * Detection happens server-side at apply-time AND on-demand via this page's
 * GET /api/candidates/duplicates query (groups by email + last-10-digit phone
 * fingerprint, only returns groups spanning 2+ pipelines).
 */
export default function DuplicatesPage() {
    const { pipelines = [] } = usePipeline() || {};
    const [groups, setGroups] = useState([]);
    const [loading, setLoading] = useState(true);
    const [busyKey, setBusyKey] = useState(null);

    // Every decision on this page is "which office keeps this person", so the
    // offices have to be named. Falls back to the id if one isn't in scope.
    const pipelineName = (id) => pipelines.find((p) => p.id === id)?.name || `${id?.slice(0, 8)}…`;

    const refresh = async () => {
        setLoading(true);
        try {
            const r = await api.get("/duplicates");
            setGroups(r.data.groups || []);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to load duplicates");
        } finally { setLoading(false); }
    };
    useEffect(() => { refresh(); }, []);

    const merge = async (winner, source, fullName) => {
        const winnerName = pipelineName(winner.pipeline_id);
        const sourceName = pipelineName(source.pipeline_id);
        // The message history is the half that doesn't come back: the merge
        // re-points every call, text and email onto the winner and stores no way
        // back. The archived source itself is restorable.
        const slotWarning = source.appointment_at && !winner.appointment_at
            ? ` Its booked interview on ${source.appointment_at.slice(0, 16).replace("T", " ")} will not carry over.`
            : "";
        const ok = await confirmDialog({
            title: `Merge into the ${winnerName} record?`,
            description: `All calls, texts and emails move onto the ${winnerName} record and will not move back. The ${sourceName} entry is archived and drops off that board — you can restore it, but its history stays here.${slotWarning}`,
            confirmLabel: "Merge",
            destructive: true,
        });
        if (!ok) return;
        setBusyKey(`${winner.id}:${source.id}`);
        try {
            await api.post(`/candidates/${winner.id}/merge`, { source_id: source.id });
            toast.success(`${fullName || "Candidate"} merged — source archived`);
            await refresh();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Merge failed");
        } finally { setBusyKey(null); }
    };

    const ignore = async (sourceId) => {
        // "Ignore" just archives the source — same backend operation as
        // a manual archive but framed differently in the UI.
        if (!(await confirmDialog({ title: "Archive this candidate?", description: "They move out of the duplicate list. You can restore them from the Archived view.", confirmLabel: "Archive" }))) return;
        setBusyKey(`ignore:${sourceId}`);
        try {
            await api.post(`/candidates/${sourceId}/archive`, { reason: "duplicate_ignored" });
            await refresh();
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Failed to archive");
        } finally { setBusyKey(null); }
    };

    return (
        <div className="min-h-screen bg-[#0C0C0E] text-ink" data-testid="duplicates-page">
            <div className="max-w-5xl mx-auto px-8 py-8">
                <div className="flex items-center gap-3 mb-1">
                    <CardsThree size={22} weight="duotone" className="text-brand-primary" />
                    <h1 className="font-heading text-3xl font-bold">Duplicates</h1>
                </div>
                <p className="text-sm text-ink-muted mb-8 max-w-2xl">
                    Candidates who applied to more than one pipeline with the same email or phone.
                    Merge them so Olivia doesn&apos;t call the same person twice and your team has the full
                    history on one record.
                </p>

                {loading ? (
                    <div className="text-sm text-ink-muted py-12 text-center">Scanning all pipelines…</div>
                ) : groups.length === 0 ? (
                    <div className="surface p-12 text-center">
                        <div className="text-3xl mb-3">🎉</div>
                        <div className="font-heading text-lg font-semibold mb-1">No duplicates</div>
                        <div className="text-sm text-ink-muted">Every active candidate belongs to one pipeline only.</div>
                    </div>
                ) : (
                    <div className="space-y-5">
                        {groups.map((g) => <DuplicateGroup key={g.group_key} group={g} busyKey={busyKey} pipelineName={pipelineName} onMerge={merge} onIgnore={ignore} />)}
                    </div>
                )}
            </div>
        </div>
    );
}

function DuplicateGroup({ group, busyKey, pipelineName, onMerge, onIgnore }) {
    // Sort: most-progressed candidate (closer to APPOINTMENT/FORM) first — that
    // becomes the suggested merge winner. Within a stage, a booked slot wins:
    // the merge keeps only the winner's appointment_at, so with two records both
    // at APPOINTMENT the arbitrary order could archive the one holding the real
    // interview and the candidate would turn up for a slot nobody can see.
    const STAGE_ORDER = { APPLICANT: 0, SCREENING: 1, APPOINTMENT: 2, FORM: 3, CLOSE: 4, TRAINING: 5 };
    const sorted = [...group.candidates].sort(
        (a, b) => ((STAGE_ORDER[b.stage] ?? 0) - (STAGE_ORDER[a.stage] ?? 0))
            || ((b.appointment_at ? 1 : 0) - (a.appointment_at ? 1 : 0))
    );
    const winner = sorted[0];
    const sources = sorted.slice(1);
    const fullName = `${winner.first_name || ""} ${winner.last_name || ""}`.trim();

    return (
        <div className="surface p-5" data-testid={`duplicate-group-${group.group_key}`}>
            <div className="flex items-center gap-3 mb-3">
                <div className="font-heading text-lg font-semibold">{fullName || "Unknown candidate"}</div>
                <span className="text-[10px] uppercase tracking-widest text-ink-muted font-semibold">
                    {group.candidates.length} matches across {new Set(group.candidates.map((c) => c.pipeline_id)).size} pipelines
                </span>
            </div>
            {winner.email && <div className="text-xs text-ink-muted">{winner.email}</div>}
            {winner.phone && <div className="text-xs text-ink-muted">{winner.phone}</div>}

            <div className="mt-4 grid grid-cols-1 gap-2.5">
                <CandidateCard cand={winner} variant="winner" pipelineName={pipelineName} />
                {sources.map((src) => (
                    <div key={src.id} className="flex items-stretch gap-2">
                        <CandidateCard cand={src} variant="source" pipelineName={pipelineName} />
                        <div className="flex flex-col gap-1.5 justify-center">
                            <button
                                onClick={() => onMerge(winner, src, fullName)}
                                disabled={busyKey === `${winner.id}:${src.id}`}
                                data-testid={`merge-into-winner-btn-${src.id}`}
                                className="btn-primary !text-[11px] !px-3 !py-2 flex items-center gap-1.5 whitespace-nowrap"
                                title={`Merge the ${pipelineName(src.pipeline_id)} entry into the ${pipelineName(winner.pipeline_id)} record above`}
                            >
                                <ArrowsMerge size={12} weight="bold" /> Merge
                            </button>
                            <button
                                onClick={() => onIgnore(src.id)}
                                disabled={busyKey === `ignore:${src.id}`}
                                data-testid={`ignore-dup-btn-${src.id}`}
                                className="text-[11px] text-ink-muted hover:text-ink px-3 py-1.5 rounded hover:bg-surface-hover flex items-center gap-1 whitespace-nowrap"
                                title="Archive this entry — keeps the winner active"
                            >
                                <X size={12} /> Archive
                            </button>
                        </div>
                    </div>
                ))}
            </div>
        </div>
    );
}

function CandidateCard({ cand, variant, pipelineName }) {
    const isWinner = variant === "winner";
    return (
        <div
            className={`flex-1 px-3.5 py-2.5 rounded-md border ${isWinner
                ? "border-brand-primary/40 bg-[rgba(139,92,246,0.06)]"
                : "border-strokes bg-surface"
                }`}
            data-testid={`dup-cand-card-${cand.id}`}
        >
            <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2 min-w-0">
                    {isWinner && (
                        <span className="text-[9px] uppercase tracking-widest font-bold px-1.5 py-0.5 rounded bg-brand-primary text-white">
                            Suggested winner
                        </span>
                    )}
                    <span className="text-xs font-medium truncate">
                        {pipelineName(cand.pipeline_id)} · {cand.stage}
                        {cand.screening_status && cand.screening_status !== "pending" && (
                            <span className="text-ink-muted ml-1.5">· {cand.screening_status}</span>
                        )}
                    </span>
                </div>
                <span className="text-[10px] text-ink-muted whitespace-nowrap">
                    {cand.created_at?.slice(0, 10)}
                </span>
            </div>
            {cand.appointment_at && (
                <div className="text-[11px] text-emerald-400 mt-1">
                    📅 Booked: {cand.appointment_at.slice(0, 16).replace("T", " ")}
                </div>
            )}
        </div>
    );
}
