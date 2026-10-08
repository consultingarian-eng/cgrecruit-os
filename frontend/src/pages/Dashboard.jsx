import { useEffect, useState, useCallback, useMemo } from "react";
import { useServerEvents } from "@/lib/useServerEvents";
import { useNavigate, useSearchParams } from "react-router-dom";
import api from "@/lib/api";
import { usePipeline } from "@/lib/pipeline";
import KanbanBoard from "@/components/KanbanBoard";
import CandidateDrawer from "@/components/CandidateDrawer";
import FiltersModal, { countActiveFilters } from "@/components/FiltersModal";
import BulkActionBar from "@/components/BulkActionBar";
import NeedsAttentionPanel from "@/components/NeedsAttentionPanel";
import { shouldSkipConfirm, setSkipConfirmForSession } from "@/components/StageMoveConfirmDialog";
import { toast } from "sonner";
import { Sparkle, Users, CheckCircle, ChartBar, Briefcase, X, Funnel, CheckSquare, ListBullets } from "@phosphor-icons/react";

const filtersKey = (pipelineId) => `cgrecruit_dashboard_filters_${pipelineId}`;

const matchesSearch = (c, q) => {
    const name = `${c.first_name || ""} ${c.last_name || ""}`.toLowerCase();
    return (
        name.includes(q) ||
        (c.email || "").toLowerCase().includes(q) ||
        (c.phone || "").toLowerCase().includes(q)
    );
};

export default function DashboardPage() {
    const navigate = useNavigate();
    const { activePipelineId, pipelines } = usePipeline();
    const [candidates, setCandidates] = useState([]);
    const [jobs, setJobs] = useState([]);
    const [loading, setLoading] = useState(true);
    const [selectedId, setSelectedId] = useState(null);
    const [searchParams, setSearchParams] = useSearchParams();
    const [showFilters, setShowFilters] = useState(false);
    const [filters, setFilters] = useState({});
    const [selectionMode, setSelectionMode] = useState(false);
    const [selectedIds, setSelectedIds] = useState(() => new Set());
    // Archived view toggle — when on, the Kanban shows ONLY soft-rejected
    // candidates so recruiters can review/restore them. Persisted to
    // localStorage so flipping pipelines doesn't lose the choice.
    const [showArchived, setShowArchived] = useState(
        () => localStorage.getItem("cgrecruit_dashboard_archived") === "1"
    );
    // Freshness of what the board is showing. Both the SSE stream and the 30s
    // poll fail silently, so without this the "Live" dot keeps pulsing green
    // over a board that stopped updating minutes ago.
    const [lastSyncedAt, setLastSyncedAt] = useState(() => Date.now());
    const [syncFailed, setSyncFailed] = useState(false);
    // Ticks so the freshness label ages on screen even when nothing arrives.
    const [nowTs, setNowTs] = useState(() => Date.now());
    // How many archived candidates the current search would have matched —
    // 0 unless a search over the active board came back empty.
    const [archivedMatches, setArchivedMatches] = useState(0);

    // Open a candidate straight from a URL — /?candidate=ID. Used by the needs-
    // attention panel and the notification bell, both of which link to one person.
    // The drawer fetches by id, so this works even when that candidate is filtered
    // out of the current board or sits in another pipeline.
    //
    // Watches searchParams rather than running once on mount: the dashboard is
    // already mounted when those links are clicked, so a mount-only effect would
    // change the address bar and nothing else.
    const candidateParam = searchParams.get("candidate");
    useEffect(() => {
        if (candidateParam) setSelectedId(candidateParam);
    }, [candidateParam]);

    const closeDrawer = useCallback(() => {
        setSelectedId(null);
        // Drop the param too, or clicking the same person again does nothing —
        // the URL would already say what it says, so no effect would fire.
        setSearchParams(
            (prev) => {
                const next = new URLSearchParams(prev);
                next.delete("candidate");
                return next;
            },
            { replace: true },
        );
    }, [setSearchParams]);
    useEffect(() => {
        localStorage.setItem("cgrecruit_dashboard_archived", showArchived ? "1" : "0");
    }, [showArchived]);

    const toggleSelect = (id) => {
        setSelectedIds((prev) => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    };
    const clearSelection = () => {
        setSelectedIds(new Set());
        setSelectionMode(false);
    };

    // `silent` skips the skeleton. Swapping the KanbanBoard for the skeleton
    // unmounts it, so every column's scroll position is lost — acceptable when
    // the whole dataset changes (pipeline / archived switch), jarring when one
    // card was edited in the drawer.
    const refresh = useCallback(async ({ silent = false } = {}) => {
        if (!activePipelineId) return;
        if (!silent) setLoading(true);
        try {
            const [cands, jb] = await Promise.all([
                api.get("/candidates", { params: { pipeline_id: activePipelineId, archived_only: showArchived || undefined } }),
                api.get("/jobs", { params: { pipeline_id: activePipelineId } }),
            ]);
            setCandidates(cands.data);
            setJobs(jb.data);
            setLastSyncedAt(Date.now());
            setSyncFailed(false);
        } catch (e) {
            setSyncFailed(true);
            toast.error("Failed to load pipeline data");
        } finally { if (!silent) setLoading(false); }
    }, [activePipelineId, showArchived]);

    const refreshSilent = useCallback(() => refresh({ silent: true }), [refresh]);

    // Silent live refresh — updates state without spinner so the Kanban "moves"
    // automatically when AI books a slot, post-call webhook flips the verdict, etc.
    const silentRefresh = useCallback(async () => {
        if (!activePipelineId) return;
        try {
            const r = await api.get("/candidates", { params: { pipeline_id: activePipelineId, archived_only: showArchived || undefined } });
            setCandidates((prev) => {
                // Only replace state if data actually changed (compare a stable signature)
                const sig = (list) => list.map((c) => `${c.id}:${c.stage}:${c.screening_status}:${c.updated_at}:${c.appointment_at || ""}:${c.verdict || ""}`).join("|");
                if (sig(prev) === sig(r.data)) return prev;
                return r.data;
            });
            setLastSyncedAt(Date.now());
            setSyncFailed(false);
        } catch {
            // Still no toast — a dropped poll isn't worth interrupting anyone —
            // but the indicator has to stop claiming the board is current.
            setSyncFailed(true);
        }
    }, [activePipelineId, showArchived]);

    useEffect(() => { refresh(); }, [refresh]);

    // SSE: instant update the moment the server pushes a status change
    useServerEvents(silentRefresh, !!activePipelineId);

    // 30s fallback poll — catches any SSE gaps (reconnect window, proxy drops)
    useEffect(() => {
        if (!activePipelineId) return;
        const id = setInterval(silentRefresh, 30000);
        return () => clearInterval(id);
    }, [activePipelineId, silentRefresh]);

    useEffect(() => {
        const id = setInterval(() => setNowTs(Date.now()), 15000);
        return () => clearInterval(id);
    }, []);

    // Re-subscribes whenever the handler identity changes: with [] deps this
    // held the first render's refresh forever, so adding an applicant after
    // switching office repainted the board with the previous office's people.
    useEffect(() => {
        const onFilters = () => setShowFilters(true);
        const onSearch = (e) => setFilters((f) => ({ ...f, search: (e.detail || "").trim() }));
        const onRefresh = () => refreshSilent();
        window.addEventListener("cgrecruit:open-filters", onFilters);
        window.addEventListener("cgrecruit:search", onSearch);
        window.addEventListener("cgrecruit:refresh", onRefresh);
        return () => {
            window.removeEventListener("cgrecruit:open-filters", onFilters);
            window.removeEventListener("cgrecruit:search", onSearch);
            window.removeEventListener("cgrecruit:refresh", onRefresh);
        };
    }, [refreshSilent]);

    const move = async (candidateId, payload) => {
        try {
            const r = await api.post(`/candidates/${candidateId}/move`, payload);
            setCandidates((prev) => prev.map((c) => (c.id === candidateId ? r.data.candidate : c)));
            if (payload.send_email_template && r.data.email?.status === "sent") {
                toast.success(`Email sent: ${payload.send_email_template}`);
            } else if (payload.send_email_template && r.data.email?.status) {
                toast.warning(`Email ${r.data.email.status}: ${r.data.email.reason || r.data.email.error || ""}`);
            }
        } catch (err) {
            // 409 means the interview already happened and nobody has said
            // whether they turned up. Dragging the card on used to discard that
            // answer silently — the attendance buttons live only in the
            // Interview column, so the move both threw the answer away and
            // removed the means of giving it. Send them to the one place that
            // can record it instead of failing with a shrug.
            if (err?.response?.status === 409) {
                toast.error(err.response.data?.detail || "Record the interview outcome first", {
                    duration: 10000,
                    action: { label: "Record it", onClick: () => setSelectedId(candidateId) },
                });
                return;
            }
            toast.error("Failed to update candidate");
        }
    };

    /** Filters cost four to six clicks through the modal to rebuild, so they
     * survive a trip to Calendar and back — but keyed per pipeline, because a
     * filter set on one office must never land on another office's board. `search` is
     * deliberately not persisted: the box that shows it lives in the header and
     * starts empty, so a restored search would filter the board invisibly. */
    const applyFilters = useCallback((f) => {
        setFilters(f);
        if (!activePipelineId) return;
        const { search, ...persisted } = f;
        try { localStorage.setItem(filtersKey(activePipelineId), JSON.stringify(persisted)); } catch { /* private mode */ }
    }, [activePipelineId]);

    useEffect(() => {
        if (!activePipelineId) return;
        let saved = {};
        try { saved = JSON.parse(localStorage.getItem(filtersKey(activePipelineId)) || "{}"); } catch { /* ignore */ }
        setFilters((f) => ({ ...saved, search: f.search }));
    }, [activePipelineId]);

    const filtered = useMemo(() => {
        let list = candidates;
        // Free-text search across name / email / phone.
        if (filters.search) {
            const q = filters.search.toLowerCase();
            list = list.filter((c) => matchesSearch(c, q));
        }
        // Stage multi-select.
        if (Array.isArray(filters.stages) && filters.stages.length) {
            const set = new Set(filters.stages);
            list = list.filter((c) => set.has(c.stage));
        }
        // Verdict multi-select.
        if (Array.isArray(filters.verdicts) && filters.verdicts.length) {
            const set = new Set(filters.verdicts);
            list = list.filter((c) => set.has(c.verdict));
        }
        // Screening status multi-select.
        if (Array.isArray(filters.statuses) && filters.statuses.length) {
            const set = new Set(filters.statuses);
            list = list.filter((c) => set.has(c.screening_status));
        }
        // Appointment date filter.
        if (filters.apptPreset && filters.apptPreset !== "any") {
            const startOfDay = (d) => { const x = new Date(d); x.setHours(0, 0, 0, 0); return x; };
            const today = startOfDay(new Date());
            const tomorrow = new Date(today); tomorrow.setDate(today.getDate() + 1);
            const dayOfWeek = today.getDay() === 0 ? 6 : today.getDay() - 1; // Mon=0..Sun=6
            const weekStart = new Date(today); weekStart.setDate(today.getDate() - dayOfWeek);
            const weekEnd = new Date(weekStart); weekEnd.setDate(weekStart.getDate() + 7);
            const nextWeekEnd = new Date(weekEnd); nextWeekEnd.setDate(weekEnd.getDate() + 7);
            list = list.filter((c) => {
                if (filters.apptPreset === "none") return !c.appointment_at;
                if (!c.appointment_at) return false;
                const a = new Date(c.appointment_at);
                if (Number.isNaN(a.getTime())) return false;
                switch (filters.apptPreset) {
                    case "today":     return a >= today && a < tomorrow;
                    case "tomorrow":  return a >= tomorrow && a < new Date(tomorrow.getTime() + 86400000);
                    case "this_week": return a >= weekStart && a < weekEnd;
                    case "next_week": return a >= weekEnd && a < nextWeekEnd;
                    case "past":      return a < today;
                    case "custom": {
                        const from = filters.apptFrom ? new Date(`${filters.apptFrom}T00:00:00`) : null;
                        const to = filters.apptTo ? new Date(`${filters.apptTo}T23:59:59`) : null;
                        if (from && a < from) return false;
                        if (to && a > to) return false;
                        return true;
                    }
                    default: return true;
                }
            });
        }
        if (filters.jobId) {
            list = list.filter((c) => c.job_id === filters.jobId);
        }
        // No minRating clause: the input is gone, and filters restored from
        // localStorage can still carry an old value that would hide candidates
        // with no control on screen to explain it.
        if (filters.minScore != null) list = list.filter((c) => (c.smart_score || 0) >= filters.minScore);
        if (filters.hasPhone) list = list.filter((c) => !!c.phone);
        if (filters.hasEmail) list = list.filter((c) => !!c.email);
        if (filters.hasResume) list = list.filter((c) => !!c.resume_url || !!c.parsed_resume);
        if (filters.referredBy) {
            const q = filters.referredBy.toLowerCase();
            list = list.filter((c) => (c.referred_by || "").toLowerCase().includes(q));
        }
        return list;
    }, [candidates, filters]);

    const activeFilterCount = countActiveFilters(filters);

    // Counted over `filtered`, not `candidates`: the columns below this strip
    // obey the filter, so counting the whole set here gave one screen two
    // different denominators. The trailing "N of M" line carries the total.
    const stats = useMemo(() => {
        const closed = filtered.filter((c) => c.stage === "CLOSE").length;
        const screening = filtered.filter((c) => c.stage === "SCREENING").length;
        const appointments = filtered.filter((c) => c.appointment_at).length;
        return { total: filtered.length, jobs: jobs.length, closed, screening, appointments };
    }, [filtered, jobs]);

    // A name that matches nobody on the active board is usually somebody who
    // was rejected or swept up by the no-show sweep — one toggle away, but an
    // empty board says only "No candidates", which reads as "never heard of
    // them". Only offered when search is the sole filter, or flipping to
    // Archived would land on an empty board too and the hint would have lied.
    useEffect(() => {
        const q = (filters.search || "").trim().toLowerCase();
        if (!activePipelineId || showArchived || !q || filtered.length > 0 || activeFilterCount > 1) {
            setArchivedMatches(0);
            return;
        }
        let cancelled = false;
        const t = setTimeout(async () => {
            try {
                const r = await api.get("/candidates", { params: { pipeline_id: activePipelineId, archived_only: true } });
                if (!cancelled) setArchivedMatches(r.data.filter((c) => matchesSearch(c, q)).length);
            } catch { /* the hint is a nicety — never interrupt a search for it */ }
        }, 400);
        return () => { cancelled = true; clearTimeout(t); };
    }, [activePipelineId, showArchived, filters.search, filtered.length, activeFilterCount]);

    // Green only while the board is provably current. Three missed polls turn
    // it amber; a failed fetch or two minutes of silence turns it into a way out.
    const sync = useMemo(() => {
        const age = Math.max(0, nowTs - lastSyncedAt);
        if (syncFailed || age > 120000) return { state: "down", label: "Not updating" };
        if (age > 90000) return { state: "stale", label: `Updated ${Math.max(1, Math.round(age / 60000))}m ago` };
        return { state: "live", label: "Live" };
    }, [nowTs, lastSyncedAt, syncFailed]);

    // Read every render (and at least once per tick) so the pill can't linger
    // after the flag is cleared, or stay hidden after a drag sets it.
    const skipConfirmOn = shouldSkipConfirm();

    const activePipeline = pipelines.find((p) => p.id === activePipelineId);

    if (!activePipelineId) {
        return (
            <div className="flex items-center justify-center h-full text-ink-muted text-sm" data-testid="no-pipeline-message">
                Select or create a pipeline to begin.
            </div>
        );
    }

    return (
        <div className="flex flex-col h-[calc(100vh-3.5rem)]" data-testid="dashboard-page">
            {/* KPI Strip */}
            <div className="px-5 py-3 border-b border-strokes bg-[#0E0E11] flex items-center gap-3 overflow-x-auto" data-testid="kpi-strip">
                <KPI icon={<Users size={14} weight="bold" />} label="Applicants" value={stats.total} />
                <KPI icon={<Sparkle size={14} weight="bold" />} label="Screening" value={stats.screening} />
                <KPI icon={<Briefcase size={14} weight="bold" />} label="Appointments" value={stats.appointments} />
                <KPI icon={<CheckCircle size={14} weight="bold" />} label="Closed" value={stats.closed} />
                <KPI icon={<ChartBar size={14} weight="bold" />} label="Jobs" value={stats.jobs} />
                <div className="ml-auto text-xs text-ink-muted flex items-center gap-2">
                    {/* Batch dialling lives on the Call Queue page, which shows
                        who is about to be called before it spends anything. */}
                    <button
                        onClick={() => navigate("/queue")}
                        data-testid="open-queue-btn"
                        title="Live calls, queued dials and retries — dial from there"
                        className="btn-secondary flex items-center gap-1.5 !py-1.5 !px-3 text-xs"
                    >
                        <ListBullets size={12} weight="bold" /> Call queue
                    </button>
                    <div className="h-4 w-px bg-strokes mx-1.5" />
                    <span className="relative flex h-1.5 w-1.5" data-testid="live-indicator" data-sync={sync.state}>
                        {sync.state === "live" && (
                            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-brand-success opacity-75"></span>
                        )}
                        <span className={`relative inline-flex rounded-full h-1.5 w-1.5 ${sync.state === "live"
                            ? "bg-brand-success"
                            : sync.state === "stale" ? "bg-amber-400" : "bg-rose-400"
                            }`}></span>
                    </span>
                    {sync.state === "down" ? (
                        <button
                            onClick={() => refresh()}
                            data-testid="live-refresh-btn"
                            className="text-[10px] uppercase tracking-widest text-rose-400 hover:text-rose-300 transition-colors"
                            title="The board hasn't updated in a while — click to reload it"
                        >
                            Not updating — Refresh
                        </button>
                    ) : (
                        <span className={`text-[10px] uppercase tracking-widest ${sync.state === "live" ? "text-ink-muted" : "text-amber-400"}`}>
                            {sync.label}
                        </span>
                    )}
                    <span className="ml-2">{activePipeline?.name} · {filtered.length} of {candidates.length} candidates</span>
                    {skipConfirmOn && (
                        <button
                            onClick={() => { setSkipConfirmForSession(false); setNowTs(Date.now()); }}
                            data-testid="skip-confirm-pill"
                            className="ml-2 inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-amber-500/15 border border-amber-500/30 text-amber-400 text-[10px] font-semibold hover:bg-amber-500/25 transition-colors"
                            title="Stage-move confirmations are off — emails and texts send without asking. Click to turn them back on."
                        >
                            Confirmations off
                            <X size={9} weight="bold" className="ml-0.5" />
                        </button>
                    )}
                    {activeFilterCount > 0 && (
                        <button
                            onClick={() => {
                                applyFilters({});
                                window.dispatchEvent(new CustomEvent("cgrecruit:clear-search"));
                            }}
                            data-testid="clear-filters-btn"
                            className="ml-2 inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-brand-primary/15 border border-brand-primary/30 text-brand-primary text-[10px] font-semibold hover:bg-brand-primary/25 transition-colors"
                            title="Clear all active filters"
                        >
                            <Funnel size={9} weight="bold" /> {activeFilterCount} filter{activeFilterCount > 1 ? "s" : ""}
                            <X size={9} weight="bold" className="ml-0.5" />
                        </button>
                    )}
                    <button
                        onClick={() => setShowArchived((v) => !v)}
                        data-testid="toggle-archived-view"
                        className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full border text-[10px] font-semibold transition-colors ${showArchived
                            ? "bg-rose-500/15 border-rose-500/30 text-rose-400"
                            : "bg-transparent border-strokes text-ink-muted hover:border-strokes-focus hover:text-ink"
                            }`}
                        title={showArchived ? "Showing only archived candidates — click to return to active view" : "Show archived (rejected) candidates so you can review or restore"}
                    >
                        {showArchived ? "⚠ Archived" : "Archived"}
                    </button>
                    <button
                        onClick={() => {
                            setSelectionMode((s) => !s);
                            if (selectionMode) setSelectedIds(new Set());
                        }}
                        data-testid="toggle-select-mode"
                        className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full border text-[10px] font-semibold transition-colors ${
                            selectionMode
                                ? "bg-brand-primary/15 border-brand-primary/30 text-brand-primary"
                                : "bg-transparent border-strokes text-ink-muted hover:border-strokes-focus hover:text-ink"
                        }`}
                        title="Select multiple candidates for bulk actions"
                    >
                        <CheckSquare size={9} weight="bold" /> {selectionMode ? "Selecting…" : "Select"}
                    </button>
                </div>
            </div>

            {/* Partial or failed ingests — renders nothing when there are none */}
            <NeedsAttentionPanel />

            {archivedMatches > 0 && (
                <div className="px-5 py-2 border-b border-strokes text-xs text-ink-muted flex items-center gap-2" data-testid="archived-match-hint">
                    <span>
                        Nobody active matches “{filters.search}” — {archivedMatches} archived{" "}
                        {archivedMatches === 1 ? "candidate does" : "candidates do"}.
                    </span>
                    <button
                        onClick={() => setShowArchived(true)}
                        data-testid="show-archived-matches-btn"
                        className="text-brand-primary font-semibold hover:underline"
                    >
                        Show archived
                    </button>
                </div>
            )}

            {/* Kanban */}
            <div className="flex-1 overflow-hidden">
                {loading ? (
                    <div className="h-full flex gap-3 p-4 overflow-hidden" data-testid="kanban-skeleton">
                        {[0, 1, 2, 3, 4].map((col) => (
                            <div key={col} className="flex-1 min-w-[220px] space-y-2">
                                <div className="h-7 rounded-md bg-surface animate-pulse" />
                                {Array.from({ length: 3 + (col % 3) }).map((_, i) => (
                                    <div key={i} className="rounded-lg bg-surface animate-pulse" style={{ height: 88, animationDelay: `${(col * 3 + i) * 60}ms` }} />
                                ))}
                            </div>
                        ))}
                    </div>
                ) : (
                    <KanbanBoard
                        candidates={filtered}
                        onCardClick={(id) => setSelectedId(id)}
                        onMove={move}
                        selectionMode={selectionMode}
                        selectedIds={selectedIds}
                        onToggleSelect={toggleSelect}
                        pipelineId={activePipelineId}
                        jobs={jobs}
                        onRefresh={refreshSilent}
                    />
                )}
            </div>
            <BulkActionBar
                selectedIds={selectedIds}
                candidates={filtered}
                onClearSelection={clearSelection}
                onAfterAction={refreshSilent}
                showArchived={showArchived}
            />

            <FiltersModal
                open={showFilters}
                onClose={() => setShowFilters(false)}
                filters={filters}
                jobs={jobs}
                referrers={[...new Set(candidates.map((c) => c.referred_by).filter(Boolean))].sort()}
                onApply={(f) => { applyFilters({ ...f, search: filters.search }); setShowFilters(false); }}
            />
            <CandidateDrawer
                candidateId={selectedId}
                onClose={closeDrawer}
                onUpdated={refreshSilent}
                jobs={jobs}
            />
        </div>
    );
}

function KPI({ icon, label, value }) {
    return (
        <div className="flex items-center gap-2 px-3 py-1.5 surface flex-shrink-0" data-testid={`kpi-${label.toLowerCase()}`}>
            <div className="text-brand-primary">{icon}</div>
            <div className="text-xs text-ink-muted">{label}</div>
            <div className="font-heading text-sm font-semibold tabular-nums">{value}</div>
        </div>
    );
}
