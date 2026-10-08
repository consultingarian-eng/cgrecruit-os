import { useState, useEffect, useCallback, useRef } from "react";
import { usePipeline } from "@/lib/pipeline";
import { useAuth } from "@/lib/auth";
import {
    Brain, ArrowClockwise, Warning, Info,
    TrendDown, Robot, ChatCircleText, Archive,
    Lightning, CaretDown, CaretUp,
} from "@phosphor-icons/react";
import api from "@/lib/api";

// ── Severity config ───────────────────────────────────────────────────────────
const SEVERITY = {
    high:   { color: "#EF4444", bg: "rgba(239,68,68,0.12)",   label: "High"   },
    medium: { color: "#F59E0B", bg: "rgba(245,158,11,0.12)",  label: "Medium" },
    low:    { color: "#10B981", bg: "rgba(16,185,129,0.12)",  label: "Low"    },
};

const SECTION_ICONS = {
    funnel_gaps:    <TrendDown    size={16} weight="duotone" />,
    ai_voice:       <Robot        size={16} weight="duotone" />,
    communications: <ChatCircleText size={16} weight="duotone" />,
    opt_outs:       <Archive      size={16} weight="duotone" />,
};

// ── Score ring ────────────────────────────────────────────────────────────────
function ScoreRing({ score }) {
    const color = score >= 75 ? "#10B981" : score >= 50 ? "#F59E0B" : "#EF4444";
    const r = 36;
    const circ = 2 * Math.PI * r;
    const dash = (score / 100) * circ;
    return (
        <div className="relative flex items-center justify-center w-24 h-24 shrink-0">
            <svg width="96" height="96" className="-rotate-90">
                <circle cx="48" cy="48" r={r} fill="none" stroke="rgba(255,255,255,0.08)" strokeWidth="8" />
                <circle
                    cx="48" cy="48" r={r} fill="none"
                    stroke={color} strokeWidth="8"
                    strokeDasharray={`${dash} ${circ}`}
                    strokeLinecap="round"
                    style={{ transition: "stroke-dasharray 0.8s ease" }}
                />
            </svg>
            <div className="absolute text-center">
                <div className="font-heading text-2xl font-bold tabular-nums" style={{ color }}>{score}</div>
                <div className="text-[9px] uppercase tracking-widest text-ink-muted">Health</div>
            </div>
        </div>
    );
}

// ── Finding card ──────────────────────────────────────────────────────────────
function Finding({ finding }) {
    const [open, setOpen] = useState(false);
    const sev = SEVERITY[finding.severity] || SEVERITY.low;
    return (
        <div
            className="rounded-lg border border-strokes overflow-hidden"
            style={{ borderLeftColor: sev.color, borderLeftWidth: 3 }}
        >
            <button
                onClick={() => setOpen((v) => !v)}
                className="w-full flex items-start gap-3 p-3.5 text-left hover:bg-surface-hover transition-colors"
            >
                <span
                    className="mt-0.5 shrink-0 text-[10px] font-semibold uppercase tracking-widest px-1.5 py-0.5 rounded"
                    style={{ background: sev.bg, color: sev.color }}
                >
                    {sev.label}
                </span>
                <span className="flex-1 text-sm font-medium text-ink">{finding.title}</span>
                <span className="text-ink-muted shrink-0 mt-0.5">
                    {open ? <CaretUp size={13} weight="bold" /> : <CaretDown size={13} weight="bold" />}
                </span>
            </button>
            {open && (
                <div className="px-3.5 pb-3.5 space-y-2.5 border-t border-strokes pt-3">
                    <p className="text-sm text-ink-muted leading-relaxed">{finding.detail}</p>
                    {finding.recommendation && (
                        <div className="flex items-start gap-2 rounded-md p-2.5" style={{ background: "rgba(99,102,241,0.1)" }}>
                            <Lightning size={14} weight="fill" className="mt-0.5 shrink-0 text-brand-primary" />
                            <p className="text-sm text-ink">{finding.recommendation}</p>
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}

// ── Section block ─────────────────────────────────────────────────────────────
function Section({ section }) {
    const [open, setOpen] = useState(true);
    const icon = SECTION_ICONS[section.id] || <Info size={16} weight="duotone" />;
    const highCount = (section.findings || []).filter((f) => f.severity === "high").length;
    return (
        <div className="surface overflow-hidden">
            <button
                onClick={() => setOpen((v) => !v)}
                className="w-full flex items-center gap-2.5 p-4 text-left hover:bg-surface-hover transition-colors"
            >
                <span className="text-brand-primary">{icon}</span>
                <span className="font-semibold text-sm text-ink flex-1">{section.title}</span>
                {highCount > 0 && (
                    <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded-full bg-red-500/15 text-red-400 border border-red-500/20">
                        {highCount} critical
                    </span>
                )}
                <span className="text-ink-muted">
                    {open ? <CaretUp size={13} weight="bold" /> : <CaretDown size={13} weight="bold" />}
                </span>
            </button>
            {open && (
                <div className="px-4 pb-4 space-y-2.5 border-t border-strokes pt-3">
                    {(section.findings || []).length === 0 ? (
                        <p className="text-sm text-ink-muted">No findings for this section.</p>
                    ) : (
                        (section.findings || []).map((f, i) => <Finding key={i} finding={f} />)
                    )}
                </div>
            )}
        </div>
    );
}

// ── Metric pill ───────────────────────────────────────────────────────────────
function MetricPill({ metric }) {
    return (
        <div className="surface p-3.5 flex flex-col gap-1">
            <div className="label-overline text-ink-muted">{metric.metric}</div>
            <div className="font-heading text-2xl font-bold tabular-nums text-ink">{metric.value}</div>
            <div className="text-[11px] text-ink-muted leading-snug">{metric.interpretation}</div>
        </div>
    );
}

// ── Top action card ───────────────────────────────────────────────────────────
function ActionCard({ action }) {
    const [open, setOpen] = useState(false);
    return (
        <div className="rounded-lg border border-strokes overflow-hidden">
            <button
                onClick={() => setOpen((v) => !v)}
                className="w-full flex items-center gap-3 p-3.5 text-left hover:bg-surface-hover transition-colors"
            >
                <span className="shrink-0 w-6 h-6 rounded-full flex items-center justify-center text-[11px] font-bold bg-brand-primary/20 text-brand-primary">
                    {action.priority}
                </span>
                <span className="flex-1 text-sm font-medium text-ink">{action.action}</span>
                <span className="text-ink-muted shrink-0">
                    {open ? <CaretUp size={13} weight="bold" /> : <CaretDown size={13} weight="bold" />}
                </span>
            </button>
            {open && (
                <div className="px-3.5 pb-3.5 space-y-3 border-t border-strokes pt-3">
                    {action.detail && <p className="text-sm text-ink-muted">{action.detail}</p>}
                    {action.expected_impact && (
                        <div className="text-xs text-emerald-400 font-medium">
                            Expected impact: {action.expected_impact}
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}

// ── Loading skeleton ──────────────────────────────────────────────────────────
function LoadingSkeleton() {
    return (
        <div className="space-y-4">
            <div className="surface p-5 flex items-center gap-5">
                <div className="w-24 h-24 rounded-full bg-surface-active animate-pulse shrink-0" />
                <div className="flex-1 space-y-2">
                    <div className="h-4 rounded bg-surface-active animate-pulse w-3/4" />
                    <div className="h-3 rounded bg-surface-active animate-pulse w-1/2" />
                    <div className="h-3 rounded bg-surface-active animate-pulse w-2/3" />
                </div>
            </div>
            {[1, 2, 3].map((i) => (
                <div key={i} className="surface p-4 space-y-2.5">
                    <div className="h-4 rounded bg-surface-active animate-pulse w-40" />
                    {[1, 2].map((j) => (
                        <div key={j} className="h-12 rounded bg-surface-active animate-pulse" style={{ animationDelay: `${j * 100}ms` }} />
                    ))}
                </div>
            ))}
        </div>
    );
}

// ── Empty state ───────────────────────────────────────────────────────────────
function EmptyState({ pipelineName, onRun, running }) {
    return (
        <div className="surface p-10 flex flex-col items-center text-center gap-4">
            <div className="w-16 h-16 rounded-2xl bg-brand-primary/15 flex items-center justify-center">
                <Brain size={32} weight="duotone" className="text-brand-primary" />
            </div>
            <div>
                <div className="font-semibold text-ink text-base mb-1">
                    No insights yet for {pipelineName || "this pipeline"}
                </div>
                <div className="text-sm text-ink-muted max-w-sm">
                    Run a full analysis to surface funnel gaps, AI voice issues, communication problems, and ranked recommendations.
                </div>
            </div>
            <button
                onClick={onRun}
                disabled={running}
                className="btn-primary flex items-center gap-2 disabled:opacity-50"
            >
                <Brain size={15} weight="duotone" />
                {running ? "Analysing…" : "Run Analysis"}
            </button>
        </div>
    );
}

// ── Main page ─────────────────────────────────────────────────────────────────
export default function AiInsightsPage() {
    const { activePipelineId, pipelines } = usePipeline();
    const { isSuperAdmin } = useAuth();

    const [selectedPipelineId, setSelectedPipelineId] = useState(() => activePipelineId || null);
    const [jobId, setJobId] = useState(null);
    const [status, setStatus] = useState("idle"); // idle | running | complete | error
    const [result, setResult] = useState(null);
    const [error, setError] = useState(null);
    const [lastRun, setLastRun] = useState(null);
    const [slowRun, setSlowRun] = useState(false);
    const pollRef = useRef(null);

    // Follow global pipeline switcher
    useEffect(() => {
        if (activePipelineId) setSelectedPipelineId((prev) => prev ?? activePipelineId);
    }, [activePipelineId]);

    // Load most recent job on pipeline change — resume polling if still running
    useEffect(() => {
        if (!selectedPipelineId) return;
        setResult(null);
        setStatus("idle");
        setError(null);
        setJobId(null);
        setSlowRun(false);
        api.get("/intelligence/ai-insights/latest", { params: { pipeline_id: selectedPipelineId } })
            .then((r) => {
                const job = r.data;
                if (!job || !job.id) return;
                if (job.status === "complete" && job.result) {
                    setResult(job.result);
                    setLastRun(job.completed_at);
                    setStatus("complete");
                } else if (job.status === "running") {
                    // Resume polling — user may have refreshed mid-analysis
                    setJobId(job.id);
                    setStatus("running");
                } else if (job.status === "error") {
                    setError(job.error || "Previous analysis failed.");
                    setStatus("error");
                }
            })
            .catch(() => {});
    }, [selectedPipelineId]);

    // Polling for running job
    useEffect(() => {
        if (!jobId || status !== "running") return;
        let ticks = 0;
        pollRef.current = setInterval(async () => {
            // A job whose worker died mid-run stays "running" for ever, so say so
            // after 5 minutes — but keep polling, a slow job still lands.
            if (++ticks === 100) setSlowRun(true);
            try {
                const r = await api.get(`/intelligence/ai-insights/${jobId}`);
                const job = r.data;
                if (job.status === "complete") {
                    clearInterval(pollRef.current);
                    setResult(job.result);
                    setLastRun(job.completed_at);
                    setStatus("complete");
                } else if (job.status === "error") {
                    clearInterval(pollRef.current);
                    setError(job.error || "Analysis failed. Please try again.");
                    setStatus("error");
                }
            } catch {
                // ignore transient poll errors
            }
        }, 3000);
        return () => clearInterval(pollRef.current);
    }, [jobId, status]);

    const runAnalysis = useCallback(async () => {
        if (!selectedPipelineId || status === "running") return;
        setStatus("running");
        setResult(null);
        setError(null);
        setSlowRun(false);
        try {
            const r = await api.post("/intelligence/ai-insights", { pipeline_id: selectedPipelineId });
            setJobId(r.data.job_id);
        } catch (e) {
            setError(e?.response?.data?.detail || "Failed to start analysis.");
            setStatus("error");
        }
    }, [selectedPipelineId, status]);

    const activePipeline = pipelines.find((p) => p.id === selectedPipelineId);
    const running = status === "running";

    const fmtDate = (iso) => {
        if (!iso) return null;
        try {
            return new Date(iso).toLocaleString("en-US", {
                month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
            });
        } catch { return null; }
    };

    return (
        <div className="max-w-4xl mx-auto px-4 md:px-6 py-6 space-y-5">
            {/* Header */}
            <div className="flex items-center justify-between gap-4 flex-wrap">
                <div className="flex items-center gap-2.5">
                    <Brain size={22} weight="duotone" className="text-brand-primary" />
                    <h1 className="font-heading text-xl font-bold text-ink">AI Pipeline Insights</h1>
                </div>
                <div className="flex items-center gap-3 flex-wrap">
                    {/* Pipeline selector (super-admins only — others are scoped to their pipeline) */}
                    {isSuperAdmin && pipelines.length > 1 && (
                        <select
                            value={selectedPipelineId || ""}
                            onChange={(e) => setSelectedPipelineId(e.target.value || null)}
                            className="input-dark text-sm py-1.5 pr-8 min-w-[160px]"
                        >
                            {pipelines.map((p) => (
                                <option key={p.id} value={p.id}>{p.name}</option>
                            ))}
                        </select>
                    )}
                    {lastRun && (
                        <span className="text-[11px] text-ink-muted">
                            Last run {fmtDate(lastRun)}
                        </span>
                    )}
                    <button
                        onClick={runAnalysis}
                        disabled={running || !selectedPipelineId}
                        className="btn-primary flex items-center gap-1.5 disabled:opacity-50"
                    >
                        {running ? (
                            <ArrowClockwise size={14} weight="bold" className="animate-spin" />
                        ) : (
                            <Brain size={14} weight="duotone" />
                        )}
                        {running ? "Analysing…" : result ? "Re-run" : "Run Analysis"}
                    </button>
                </div>
            </div>

            {/* Running hint */}
            {running && (
                <div className="surface p-4 flex items-center gap-3 border border-brand-primary/20">
                    <ArrowClockwise size={16} weight="bold" className="animate-spin text-brand-primary shrink-0" />
                    <div>
                        <div className="text-sm font-medium text-ink">Analysing {activePipeline?.name || "pipeline"}…</div>
                        <div className="text-xs text-ink-muted mt-0.5">
                            {slowRun
                                ? "Still running after 5 minutes — the analysis may have been interrupted on the server. Reload in a few minutes to check."
                                : "Reading call transcripts, communications, and funnel data. This takes 30–60 seconds."}
                        </div>
                    </div>
                </div>
            )}

            {/* Error state */}
            {status === "error" && (
                <div className="surface p-4 flex items-center gap-3 border border-red-500/20">
                    <Warning size={18} weight="duotone" className="text-red-400 shrink-0" />
                    <div>
                        <div className="text-sm font-medium text-red-400">Analysis failed</div>
                        <div className="text-xs text-ink-muted mt-0.5">{error}</div>
                    </div>
                </div>
            )}

            {/* Loading skeleton while polling */}
            {running && !result && <LoadingSkeleton />}

            {/* Empty state */}
            {status === "idle" && !result && !running && (
                <EmptyState
                    pipelineName={activePipeline?.name}
                    onRun={runAnalysis}
                    running={running}
                />
            )}

            {/* Results */}
            {result && (
                <div className="space-y-5">
                    {/* Overview card */}
                    <div className="surface p-5 flex items-start gap-5">
                        <ScoreRing score={result.overall_score ?? 50} />
                        <div className="flex-1 min-w-0">
                            <div className="label-overline text-ink-muted mb-1">{activePipeline?.name || "Pipeline"} — 90-day analysis</div>
                            <p className="text-base font-semibold text-ink leading-snug">{result.headline}</p>
                            {lastRun && (
                                <p className="text-[11px] text-ink-muted mt-2">Last analysed {fmtDate(lastRun)}</p>
                            )}
                        </div>
                    </div>

                    {/* Metrics callout */}
                    {(result.metrics_callout || []).length > 0 && (
                        <div>
                            <div className="label-overline text-ink-muted mb-2">Key Metrics</div>
                            <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
                                {result.metrics_callout.map((m, i) => (
                                    <MetricPill key={i} metric={m} />
                                ))}
                            </div>
                        </div>
                    )}

                    {/* Top actions */}
                    {(result.top_actions || []).length > 0 && (
                        <div>
                            <div className="flex items-center gap-2 mb-2">
                                <Lightning size={15} weight="duotone" className="text-amber-400" />
                                <div className="label-overline text-ink-muted">Top Recommended Actions</div>
                            </div>
                            <div className="surface overflow-hidden divide-y divide-strokes">
                                {result.top_actions.map((a, i) => (
                                    <ActionCard key={i} action={a} />
                                ))}
                            </div>
                        </div>
                    )}

                    {/* Detail sections */}
                    {(result.sections || []).length > 0 && (
                        <div>
                            <div className="label-overline text-ink-muted mb-2">Detailed Findings</div>
                            <div className="space-y-3">
                                {result.sections.map((s) => (
                                    <Section key={s.id} section={s} />
                                ))}
                            </div>
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}
