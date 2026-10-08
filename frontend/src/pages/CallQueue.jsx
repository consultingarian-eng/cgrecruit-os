import { useEffect, useState, useCallback, useMemo } from "react";
import { useServerEvents } from "@/lib/useServerEvents";
import { useNavigate } from "react-router-dom";
import api from "@/lib/api";
import { toast } from "sonner";
import { Phone, ArrowLeft, Pause, Play, ArrowRight, Check } from "@phosphor-icons/react";
import { usePipeline } from "@/lib/pipeline";
import { etNaiveToUtcMs, fmtET } from "@/lib/formatET";

/** A queued row this close to its dial time is one the cap can actually be
 * holding back; anything further out is just the stagger doing its job. */
const DUE_SOON_MS = 120_000;

/**
 * Per-pipeline call queue console. Shows three lists: live calls, queued
 * (with countdowns + per-row pause), paused (with re-queue). SSE pushes plus a
 * 30s fallback poll keep it current, so there is no manual refresh control.
 */
export default function CallQueuePage() {
    const navigate = useNavigate();
    const { activePipelineId, pipelines, timezone } = usePipeline();
    const activePipeline = pipelines.find((p) => p.id === activePipelineId);
    const [data, setData] = useState({ live: [], queued: [], paused: [], counts: { live: 0, queued: 0, paused: 0 } });
    const [loading, setLoading] = useState(true);
    const [busyIds, setBusyIds] = useState({});

    const fetchQueue = useCallback(async () => {
        if (!activePipelineId) return;
        try {
            const r = await api.get(`/pipelines/${activePipelineId}/call-queue`);
            setData(r.data);
        } catch {
            toast.error("Failed to load call queue");
        } finally { setLoading(false); }
    }, [activePipelineId]);

    useEffect(() => {
        setLoading(true);
        fetchQueue();
        // 30s fallback poll — SSE handles instant updates below
        const t = setInterval(fetchQueue, 30000);
        return () => clearInterval(t);
    }, [fetchQueue]);

    // SSE: re-fetch the instant the server pushes a status change
    useServerEvents(fetchQueue, !!activePipelineId);

    const pause = async (cand) => {
        setBusyIds((p) => ({ ...p, [cand.id]: "pausing" }));
        try {
            await api.post(`/candidates/${cand.id}/queue/pause`);
            toast.success(`Paused ${cand.first_name || ""} ${cand.last_name || ""}`.trim());
            await fetchQueue();
        } catch { toast.error("Couldn't pause"); }
        finally { setBusyIds((p) => { const n = { ...p }; delete n[cand.id]; return n; }); }
    };

    const resume = async (cand) => {
        setBusyIds((p) => ({ ...p, [cand.id]: "resuming" }));
        try {
            await api.post(`/candidates/${cand.id}/queue/resume`, { delay_minutes: 0 });
            toast.success(`Re-queued ${cand.first_name || ""} ${cand.last_name || ""}`.trim());
            await fetchQueue();
        } catch (e) {
            // Resume now 400s with the precise blocker (stage moved on, auto_dial
            // off, archived, master switch off) — a generic message would leave
            // the recruiter pressing a button that can never work.
            toast.error(e?.response?.data?.detail || "Couldn't re-queue");
        }
        finally { setBusyIds((p) => { const n = { ...p }; delete n[cand.id]; return n; }); }
    };

    const dialAll = async () => {
        try {
            const r = await api.post(`/pipelines/${activePipelineId}/batch-dial`);
            const n = r.data?.queued || 0;
            // Nothing eligible is not a success — say so rather than "Queued 0".
            if (n === 0) toast.warning("No candidates eligible for batch dial");
            else toast.success(`Queued ${n} candidate${n === 1 ? "" : "s"}`);
            await fetchQueue();
        } catch (e) { toast.error(e?.response?.data?.detail || "Batch dial failed"); }
    };

    /** Sweep candidates whose `screening_status='in_progress'` is stale —
     * managed-mode calls go EL ↔ Twilio direct so the post-call hook sometimes
     * doesn't reach us, and the Live tile shows ghosts. The backend safely
     * resets them based on the latest conversation status. Auto-runs every
     * 5 min server-side; this button is for an immediate refresh. */
    const cleanupStale = async () => {
        try {
            const r = await api.post(`/pipelines/${activePipelineId}/call-queue/reconcile`);
            const n = r.data.reset || 0;
            if (n > 0) toast.success(`Reset ${n} stuck screening${n === 1 ? "" : "s"}`);
            else toast.info("Nothing stuck — every screening is current");
            await fetchQueue();
        } catch { toast.error("Cleanup failed"); }
    };

    /** The API has no "held back by the cap" flag, so infer it: a queued row
     * whose dial time has arrived (or lands within the postpone loop's next
     * turn) is one the cap is sitting on. Gating the badge on counts.queued
     * alone lit it up for a batch staggered across tomorrow and stayed dark
     * while calls were genuinely being postponed 30s at a time. */
    const dueSoonQueued = useMemo(() => {
        const cutoff = Date.now() + DUE_SOON_MS;
        return (data.queued || []).filter((c) => {
            const ms = etNaiveToUtcMs(c.next_call_at, timezone);
            return ms > 0 && ms <= cutoff;
        }).length;
    }, [data.queued, timezone]);

    if (!activePipelineId) {
        return (
            <div className="p-8 text-center text-ink-muted text-sm">
                Pick a pipeline from the top bar to view its call queue.
            </div>
        );
    }

    return (
        <div className="p-8 max-w-6xl mx-auto" data-testid="call-queue-page">
            <div className="flex items-center justify-between gap-4 mb-6">
                <div>
                    <button
                        onClick={() => navigate("/dashboard")}
                        className="flex items-center gap-1.5 text-xs text-ink-muted hover:text-ink mb-2"
                    >
                        <ArrowLeft size={11} weight="bold" /> Back to Dashboard
                    </button>
                    <h1 className="font-heading text-3xl font-bold tracking-tight">Call Queue</h1>
                    <p className="text-sm text-ink-muted mt-1">
                        Live screening calls + every queued / retry-scheduled dial for{" "}
                        <span className="text-ink font-semibold">{activePipeline?.name || "this pipeline"}</span>.
                    </p>
                </div>
                <div className="flex items-center gap-2">
                    {(data.stale_in_progress > 0) && (
                        <button
                            onClick={cleanupStale}
                            data-testid="queue-cleanup-stale-btn"
                            className="px-3 py-1.5 text-left rounded-md border border-amber-500/40 bg-amber-500/10 text-amber-300 hover:bg-amber-500/20 transition-colors"
                        >
                            <div className="text-[11px] font-semibold">
                                ⚠ {data.stale_in_progress} screening{data.stale_in_progress === 1 ? "" : "s"} stuck — reset
                            </div>
                            <div className="text-[10px] text-amber-300/70">
                                Marked as screening, but nothing is actually running.
                            </div>
                        </button>
                    )}
                    <button
                        onClick={dialAll}
                        data-testid="queue-dial-all-btn"
                        className="btn-primary flex items-center gap-1.5"
                    >
                        <Phone size={12} weight="bold" /> Dial all queued
                    </button>
                </div>
            </div>

            <div className="grid grid-cols-3 gap-3 mb-6">
                <Stat color="#34D399" label="On a call now" value={data.counts.live} testid="stat-live" />
                <Stat color="#A78BFA" label="Queued" value={data.counts.queued} testid="stat-queued" />
                <Stat color="#94A3B8" label="Paused" value={data.counts.paused} testid="stat-paused" />
            </div>

            {data.concurrency && (
                <div
                    className="surface mb-6 p-4"
                    data-testid="concurrency-meter"
                >
                    <div className="flex items-center justify-between text-xs">
                        <div>
                            <span className="font-semibold text-ink">
                                {data.counts.live} on the phone now
                            </span>
                            {/* Not a hard ceiling — the cap only holds back the auto-dialer;
                                a manual Call or an agent test call dials regardless. */}
                            <span className="text-ink-muted ml-2">
                                · auto-dialer waits past {data.concurrency.effective_cap} · pipeline cap {data.concurrency.pipeline_cap} · tenant {data.concurrency.tenant_live}/{data.concurrency.tenant_cap}
                            </span>
                        </div>
                        {data.counts.live >= data.concurrency.effective_cap && dueSoonQueued > 0 && (
                            <span className="text-[10px] uppercase tracking-widest font-bold text-[#FBBF24]">
                                AT CAP — QUEUED CALLS WAITING
                            </span>
                        )}
                    </div>
                    <div className="mt-2 h-1.5 rounded-full bg-[#0B0B0F] overflow-hidden">
                        <div
                            data-testid="concurrency-meter-fill"
                            className="h-full bg-gradient-to-r from-[#A78BFA] to-[#EC4899] transition-all"
                            style={{
                                width: `${Math.min(100, (data.counts.live / Math.max(1, data.concurrency.effective_cap)) * 100)}%`,
                            }}
                        />
                    </div>
                </div>
            )}

            {loading && <div className="text-center text-ink-muted text-sm py-12">Loading…</div>}

            {!loading && (
                <>
                    <Section title="Live calls" subtitle="Candidates currently mid-screening — the AI is on the phone right now.">
                        {data.live.length === 0 ? (
                            <Empty msg="No live calls. The next queued candidate will be dialed automatically." />
                        ) : (
                            data.live.map((c) => (
                                <Row
                                    key={c.id}
                                    candidate={c}
                                    onClick={() => navigate(`/dashboard?candidate=${c.id}`)}
                                    rightSlot={
                                        <span
                                            data-testid={`live-pulse-${c.id}`}
                                            className="flex items-center gap-1.5 text-[11px] text-[#34D399] font-semibold"
                                        >
                                            <span className="relative flex h-2 w-2">
                                                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-[#34D399] opacity-75"></span>
                                                <span className="relative inline-flex rounded-full h-2 w-2 bg-[#34D399]"></span>
                                            </span>
                                            On call
                                        </span>
                                    }
                                />
                            ))
                        )}
                        {data.truncated?.live && <MoreRows shown={data.live.length} total={data.counts.live} testid="truncated-live" />}
                    </Section>

                    <Section
                        title="Queued"
                        subtitle="Sorted by next-call time. The auto-dialer respects your call window + per-pipeline interval."
                    >
                        {data.queued.length === 0 ? (
                            <Empty msg="Queue is empty. Click 'Dial all queued' to fan out across the pipeline." />
                        ) : (
                            data.queued.map((c) => (
                                <Row
                                    key={c.id}
                                    candidate={c}
                                    onClick={() => navigate(`/dashboard?candidate=${c.id}`)}
                                    rightSlot={
                                        <div className="flex items-center gap-3">
                                            <ETA at={c.next_call_at} attempts={c.call_attempts} />
                                            <button
                                                onClick={(e) => { e.stopPropagation(); pause(c); }}
                                                disabled={busyIds[c.id] === "pausing"}
                                                data-testid={`queue-pause-${c.id}`}
                                                className="text-[11px] flex items-center gap-1 text-ink-muted hover:text-ink px-2 py-1 rounded border border-strokes hover:border-strokes-focus"
                                            >
                                                <Pause size={10} weight="bold" />
                                                {busyIds[c.id] === "pausing" ? "Pausing…" : "Pause"}
                                            </button>
                                        </div>
                                    }
                                />
                            ))
                        )}
                        {data.truncated?.queued && <MoreRows shown={data.queued.length} total={data.counts.queued} testid="truncated-queued" />}
                    </Section>

                    <Section
                        title="Paused"
                        subtitle="Pulled out of the dial queue manually. They stay in the pipeline until you Re-queue them."
                    >
                        {data.paused.length === 0 ? (
                            <Empty msg="No paused candidates." />
                        ) : (
                            data.paused.map((c) => (
                                <Row
                                    key={c.id}
                                    candidate={c}
                                    onClick={() => navigate(`/dashboard?candidate=${c.id}`)}
                                    rightSlot={
                                        <button
                                            onClick={(e) => { e.stopPropagation(); resume(c); }}
                                            disabled={busyIds[c.id] === "resuming"}
                                            data-testid={`queue-resume-${c.id}`}
                                            className="text-[11px] flex items-center gap-1 text-brand-primary hover:underline px-2 py-1"
                                        >
                                            <Play size={10} weight="bold" />
                                            {busyIds[c.id] === "resuming" ? "Re-queueing…" : "Re-queue"}
                                        </button>
                                    }
                                />
                            ))
                        )}
                        {data.truncated?.paused && <MoreRows shown={data.paused.length} total={data.counts.paused} testid="truncated-paused" />}
                    </Section>
                </>
            )}
        </div>
    );
}

function Stat({ color, label, value, testid }) {
    return (
        <div className="surface p-4 flex items-center justify-between" data-testid={testid}>
            <div>
                <div className="text-[10px] uppercase tracking-widest text-ink-muted font-semibold">{label}</div>
                <div className="font-heading text-3xl font-bold mt-1 tabular-nums" style={{ color }}>
                    {value}
                </div>
            </div>
        </div>
    );
}

function Section({ title, subtitle, children }) {
    return (
        <div className="mb-8">
            <div className="mb-2">
                <h2 className="font-heading text-lg font-semibold">{title}</h2>
                <p className="text-xs text-ink-muted">{subtitle}</p>
            </div>
            <div className="surface divide-y divide-strokes">{children}</div>
        </div>
    );
}

function Empty({ msg }) {
    return <div className="px-4 py-6 text-center text-sm text-ink-muted">{msg}</div>;
}

/** The API caps each list for rendering while `counts` stay true totals, so say
 * so rather than letting the page look like the backlog stops here. */
function MoreRows({ shown, total, testid }) {
    return (
        <div className="px-4 py-2 text-center text-[11px] text-ink-muted" data-testid={testid}>
            Showing the first {shown} of {total}.
        </div>
    );
}

function Row({ candidate, onClick, rightSlot }) {
    const fullName = `${candidate.first_name || ""} ${candidate.last_name || ""}`.trim() || "(no name)";
    return (
        <button
            onClick={onClick}
            data-testid={`queue-row-${candidate.id}`}
            className="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-surface-hover transition-colors"
        >
            <div className="flex items-center gap-3 min-w-0 flex-1">
                <div
                    className="w-8 h-8 rounded-full flex items-center justify-center text-[11px] font-bold flex-shrink-0"
                    style={{ background: "rgba(139,92,246,0.15)", color: "#A78BFA" }}
                >
                    {(candidate.first_name?.[0] || "?")}{(candidate.last_name?.[0] || "")}
                </div>
                <div className="min-w-0">
                    <div className="font-semibold text-sm truncate">{fullName}</div>
                    <div className="text-[11px] text-ink-muted truncate">
                        {candidate.phone || "—"} · {candidate.stage}
                        {candidate.appointment_at && (
                            <span className="ml-2 text-[#34D399]">
                                <Check size={9} weight="bold" className="inline mr-0.5" /> slot booked
                            </span>
                        )}
                    </div>
                </div>
            </div>
            <div className="flex items-center gap-2 flex-shrink-0">
                {rightSlot}
                <ArrowRight size={11} weight="bold" className="text-ink-muted/60" />
            </div>
        </button>
    );
}

function ETA({ at, attempts }) {
    const { timezone } = usePipeline() || {};
    const [now, setNow] = useState(Date.now());
    useEffect(() => {
        const t = setInterval(() => setNow(Date.now()), 30000);
        return () => clearInterval(t);
    }, []);
    if (!at) return null;
    // next_call_at is a naive wall-clock string in the account's timezone; rows
    // written before the writers were made canonical can still carry a UTC
    // offset, which etNaiveToUtcMs honours. 0 means unparseable — show nothing
    // rather than a confident (and wrong) "Calling soon".
    const targetMs = etNaiveToUtcMs(at, timezone);
    if (!targetMs) return null;
    const ms = targetMs - now;
    const soon = ms <= 0;
    const totalMin = Math.floor(Math.abs(ms) / 60000);
    const hrs = Math.floor(totalMin / 60);
    const mins = totalMin % 60;
    const label = hrs > 0 ? `${hrs}h ${mins}m` : `${mins}m`;
    const attemptLabel = attempts ? ` · attempt ${attempts + 1}` : "";
    return (
        <span
            className="text-[11px] font-semibold tabular-nums text-ink-muted"
            data-testid={`queue-eta-${at}`}
            title={`Scheduled for ${fmtET(new Date(targetMs), { timeZone: timezone, hour: '2-digit', minute: '2-digit', timeZoneName: 'short' })}`}
        >
            {soon ? "Calling soon" : `in ${label}`}{attemptLabel}
        </span>
    );
}
