import { useEffect, useState, useRef, useCallback } from "react";
import { usePipeline } from "@/lib/pipeline";
import { ArrowLeft, ArrowRight, ArrowsClockwise, DownloadSimple, CheckCircle, Warning } from "@phosphor-icons/react";
import api from "@/lib/api";

// ── Date helpers ──────────────────────────────────────────────────────────────
function toISO(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
}

// Week
function getThisWeekMonday() {
    const today = new Date();
    const diff = today.getDay() === 0 ? -6 : 1 - today.getDay();
    const mon = new Date(today);
    mon.setDate(today.getDate() + diff);
    return toISO(mon);
}
function addWeeks(iso, n) {
    const d = new Date(iso + "T00:00:00");
    d.setDate(d.getDate() + n * 7);
    return toISO(d);
}
function weekPeriodEnd(iso) {
    const d = new Date(iso + "T00:00:00");
    d.setDate(d.getDate() + 7);
    return toISO(d);
}
function weekLabel(iso) {
    const mon = new Date(iso + "T00:00:00");
    const sun = new Date(mon);
    sun.setDate(mon.getDate() + 6);
    const fmt = (d) => d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
    return `${fmt(mon)} – ${fmt(sun)}`;
}

// Month
function getThisMonthStart() {
    const d = new Date();
    return toISO(new Date(d.getFullYear(), d.getMonth(), 1));
}
function addMonths(iso, n) {
    const d = new Date(iso + "T00:00:00");
    d.setMonth(d.getMonth() + n);
    d.setDate(1);
    return toISO(d);
}
function monthPeriodEnd(iso) {
    const d = new Date(iso + "T00:00:00");
    return toISO(new Date(d.getFullYear(), d.getMonth() + 1, 1));
}
function monthLabel(iso) {
    const d = new Date(iso + "T00:00:00");
    return d.toLocaleDateString("en-US", { month: "long", year: "numeric" });
}

// Quarter
function getThisQuarterStart() {
    const d = new Date();
    const q = Math.floor(d.getMonth() / 3);
    return toISO(new Date(d.getFullYear(), q * 3, 1));
}
function addQuarters(iso, n) {
    const d = new Date(iso + "T00:00:00");
    d.setMonth(d.getMonth() + n * 3);
    d.setDate(1);
    return toISO(d);
}
function quarterPeriodEnd(iso) {
    const d = new Date(iso + "T00:00:00");
    const q = Math.floor(d.getMonth() / 3);
    return toISO(new Date(d.getFullYear(), (q + 1) * 3, 1));
}
function quarterLabel(iso) {
    const d = new Date(iso + "T00:00:00");
    const q = Math.floor(d.getMonth() / 3) + 1;
    return `Q${q} ${d.getFullYear()}`;
}

function getCurrentPeriodStart(mode) {
    if (mode === "month")   return getThisMonthStart();
    if (mode === "quarter") return getThisQuarterStart();
    return getThisWeekMonday();
}
function getPeriodEnd(start, mode) {
    if (mode === "month")   return monthPeriodEnd(start);
    if (mode === "quarter") return quarterPeriodEnd(start);
    return weekPeriodEnd(start);
}
function getPeriodLabel(start, mode) {
    if (mode === "month")   return monthLabel(start);
    if (mode === "quarter") return quarterLabel(start);
    return weekLabel(start);
}
function advancePeriod(start, mode, n) {
    if (mode === "month")   return addMonths(start, n);
    if (mode === "quarter") return addQuarters(start, n);
    return addWeeks(start, n);
}

// ── Percentage bubble with hover tooltip ─────────────────────────────────────
function PctBubble({ actual, benchmark, loading, fromCount, toCount }) {
    const [hovered, setHovered] = useState(false);

    if (loading) {
        return (
            <div className="w-16 h-9 rounded-full bg-surface-active animate-pulse flex-shrink-0" />
        );
    }
    if (actual == null) {
        return (
            <div className="w-16 h-9 rounded-full bg-surface-active flex items-center justify-center flex-shrink-0">
                <span className="text-xs text-ink-muted font-mono">%</span>
            </div>
        );
    }
    const diff = actual - benchmark;
    let bg;
    if (diff >= -5)       bg = "linear-gradient(135deg, #10B981, #059669)";
    else if (diff >= -10) bg = "linear-gradient(135deg, #F59E0B, #D97706)";
    else                  bg = "linear-gradient(135deg, #EF4444, #DC2626)";

    const showTooltip = hovered && fromCount != null && toCount != null;

    return (
        <div
            className="relative flex-shrink-0 flex items-center justify-center"
            style={{ width: "4rem", height: "2.25rem" }}
            onMouseEnter={() => setHovered(true)}
            onMouseLeave={() => setHovered(false)}
        >
            <div
                className="w-16 h-9 rounded-full flex items-center justify-center font-bold text-sm tabular-nums cursor-default select-none"
                style={{ background: bg, color: "#fff" }}
            >
                {actual}%
            </div>

            {showTooltip && (
                <div
                    className="absolute bottom-full left-1/2 mb-2.5 z-50 pointer-events-none"
                    style={{ transform: "translateX(-50%)" }}
                >
                    <div
                        className="flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-semibold whitespace-nowrap shadow-xl"
                        style={{
                            background: "linear-gradient(135deg, #1e2130, #252a3d)",
                            border: "1px solid rgba(255,255,255,0.12)",
                            color: "#fff",
                            boxShadow: "0 8px 24px rgba(0,0,0,0.5)",
                        }}
                    >
                        <span className="tabular-nums text-sm font-bold" style={{ color: "#94a3b8" }}>
                            {fromCount}
                        </span>
                        <svg width="14" height="10" viewBox="0 0 14 10" fill="none">
                            <path d="M1 5h11M8 1l4 4-4 4" stroke="#6366f1" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"/>
                        </svg>
                        <span className="tabular-nums text-sm font-bold" style={{ color: "#fff" }}>
                            {toCount}
                        </span>
                    </div>
                    {/* Caret */}
                    <div className="flex justify-center">
                        <div style={{
                            width: 0, height: 0,
                            borderLeft: "5px solid transparent",
                            borderRight: "5px solid transparent",
                            borderTop: "5px solid rgba(255,255,255,0.12)",
                            marginTop: "-1px",
                        }} />
                    </div>
                </div>
            )}
        </div>
    );
}

// Benchmark bubble is always green
function BenchmarkBubble({ value }) {
    return (
        <div
            className="w-16 h-9 rounded-full flex items-center justify-center flex-shrink-0 font-bold text-sm tabular-nums"
            style={{ background: "linear-gradient(135deg, #10B981, #059669)", color: "#fff" }}
        >
            {value}%
        </div>
    );
}

// ── Single funnel step row ────────────────────────────────────────────────────
function StepRow({ leftLabel, rightLabel, benchmarkPct, actualPct, loading, fromCg1, fromCount, toCount }) {
    return (
        <div className="flex items-center gap-3 py-2.5 border-b border-strokes last:border-0">
            {/* Benchmark side */}
            <div className="flex items-center gap-2 flex-1 min-w-0">
                <span className="text-xs font-semibold text-ink uppercase tracking-wide truncate text-right flex-1">
                    {leftLabel}
                </span>
                <BenchmarkBubble value={benchmarkPct} />
                <span className="text-xs font-semibold text-ink uppercase tracking-wide truncate flex-1">
                    {rightLabel}
                </span>
            </div>
            {/* Divider */}
            <div className="w-px h-8 bg-strokes flex-shrink-0" />
            {/* Actual side */}
            <div className="flex items-center gap-2 flex-1 min-w-0">
                <span className="text-xs font-semibold text-ink uppercase tracking-wide truncate text-right flex-1">
                    {leftLabel}
                </span>
                <PctBubble
                    actual={actualPct}
                    benchmark={benchmarkPct}
                    loading={loading}
                    fromCount={fromCount}
                    toCount={toCount}
                />
                <span className="text-xs font-semibold text-ink uppercase tracking-wide truncate flex-1">
                    {rightLabel}
                    {fromCg1 && <span className="ml-1 text-[9px] text-ink-muted align-middle">(CG1)</span>}
                </span>
            </div>
        </div>
    );
}

// ── Print/export styles ───────────────────────────────────────────────────────
const PRINT_STYLE = `
@media print {
  body * { visibility: hidden; }
  #funnel-report-print, #funnel-report-print * { visibility: visible; }
  #funnel-report-print { position: absolute; left: 0; top: 0; width: 100%; }
  .no-print { display: none !important; }
}
`;

// ── Main page ─────────────────────────────────────────────────────────────────
export default function FunnelReport() {
    const { activePipelineId } = usePipeline();
    const [viewMode, setViewMode] = useState("week");
    const [periodStart, setPeriodStart] = useState(getThisWeekMonday);
    const [report, setReport] = useState(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);
    const styleRef = useRef(null);

    const currentPeriodStart = getCurrentPeriodStart(viewMode);
    const isCurrentPeriod = periodStart === currentPeriodStart;

    // Inject print styles once
    useEffect(() => {
        const el = document.createElement("style");
        el.textContent = PRINT_STYLE;
        document.head.appendChild(el);
        styleRef.current = el;
        return () => el.remove();
    }, []);

    const load = useCallback(async (pipeline, start, mode) => {
        setLoading(true);
        setError(null);
        try {
            const params = { week_start: start };
            if (mode !== "week") params.period_end = getPeriodEnd(start, mode);
            if (pipeline) params.pipeline_id = pipeline;
            const { data } = await api.get("/intelligence/funnel-report", { params });
            setReport(data);
        } catch (e) {
            const detail = e?.response?.data?.detail;
            const status = e?.response?.status;
            const msg = detail || (status ? `Server error ${status}` : e?.message) || "Failed to load report";
            console.error("funnel-report error:", e?.response?.data || e);
            setError(msg);
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        load(activePipelineId, periodStart, viewMode);
    }, [activePipelineId, periodStart, viewMode, load]);

    const switchMode = (mode) => {
        setViewMode(mode);
        setPeriodStart(getCurrentPeriodStart(mode));
    };

    const b = report?.benchmarks || {};
    const r = report?.rates || {};
    const a = report?.actuals || {};

    // Steps 1–4: recruitment cohort — candidates ADDED this week
    // Steps 5–8: new-starters cohort — candidates whose training_start_at falls this week
    // (the same person counts in whichever week they were recruited AND in the WE they started)
    const recruitSteps = [
        { left: "CV Upload",       right: "AI Screened",     bKey: "cv_to_screened",     rKey: "cv_to_screened",     leftCount: a.added,          rightCount: a.screened },
        { left: "Booked",          right: "Attended Pres",   bKey: "booked_to_attended", rKey: "booked_to_attended", leftCount: a.booked,          rightCount: a.attended },
        { left: "Attended Pres",   right: "Filled Form",     bKey: "attended_to_form",   rKey: "attended_to_form",   leftCount: a.attended,        rightCount: a.form_submitted },
        { left: "Filled Form",     right: "Booked to Start", bKey: "form_to_training",   rKey: "form_to_training",   leftCount: a.form_submitted,  rightCount: a.recruited_to_training },
    ];
    const starterSteps = [
        { left: "BA's Booked for Training", right: "BA's Attending Training", bKey: "training_to_attended", rKey: "training_to_attended", leftCount: a.new_starters,       rightCount: a.training_attended },
        { left: "BA's Attending Training",  right: "BA's Badged",             bKey: "attended_to_badge",    rKey: "attended_to_badge",    leftCount: a.training_attended,  rightCount: a.badged,        fromCg1: true },
        { left: "BA's Badged",              right: "BA's doing First Sale",   bKey: "badge_to_sale",        rKey: "badge_to_sale",        leftCount: a.badged,             rightCount: a.made_sale,     fromCg1: true },
        { left: "BA's Badged",              right: "BA Completing Week 1",    bKey: "badge_to_week1",       rKey: "badge_to_week1",       leftCount: a.badged,             rightCount: a.week1_complete, fromCg1: true },
    ];

    return (
        <div className="flex flex-col gap-5 p-4 md:p-6 max-w-4xl mx-auto">
            {/* Header */}
            <div className="flex flex-col sm:flex-row sm:items-center gap-3 no-print">
                <div className="flex-1">
                    <h1 className="font-heading text-xl font-bold text-ink">Funnel Report</h1>
                    <p className="text-xs text-ink-muted mt-0.5">Recruitment funnel vs targets — incl. CG1 training outcomes</p>
                </div>
                <div className="flex items-center gap-2 flex-wrap justify-end">
                    {/* View mode toggle */}
                    <div className="flex rounded-md overflow-hidden border border-strokes text-[11px] font-medium">
                        {["week", "month", "quarter"].map((m) => (
                            <button
                                key={m}
                                onClick={() => switchMode(m)}
                                className={`px-2.5 py-1 capitalize transition-colors ${viewMode === m ? "bg-brand-primary text-white" : "text-ink-muted hover:text-ink hover:bg-surface-active"}`}
                            >
                                {m.charAt(0).toUpperCase() + m.slice(1)}
                            </button>
                        ))}
                    </div>
                    {/* Period navigator */}
                    <div className="flex items-center gap-1">
                        <button
                            onClick={() => setPeriodStart(s => advancePeriod(s, viewMode, -1))}
                            className="p-1.5 rounded text-ink-muted hover:text-ink hover:bg-surface-active transition-colors"
                        >
                            <ArrowLeft size={14} weight="bold" />
                        </button>
                        <div className="px-3 py-1.5 rounded-md bg-surface-active text-xs font-medium text-ink min-w-[150px] text-center">
                            {getPeriodLabel(periodStart, viewMode)}
                        </div>
                        <button
                            onClick={() => setPeriodStart(s => advancePeriod(s, viewMode, 1))}
                            disabled={isCurrentPeriod}
                            className="p-1.5 rounded text-ink-muted hover:text-ink hover:bg-surface-active transition-colors disabled:opacity-30"
                        >
                            <ArrowRight size={14} weight="bold" />
                        </button>
                    </div>
                    {!isCurrentPeriod && (
                        <button
                            onClick={() => setPeriodStart(currentPeriodStart)}
                            className="px-2 py-1 rounded text-[11px] text-brand-primary hover:bg-surface-active transition-colors"
                        >
                            {viewMode === "week" ? "This week" : viewMode === "month" ? "This month" : "This quarter"}
                        </button>
                    )}
                    <button
                        onClick={() => load(activePipelineId, periodStart, viewMode)}
                        className="p-1.5 rounded text-ink-muted hover:text-ink hover:bg-surface-active transition-colors"
                        title="Refresh"
                    >
                        <ArrowsClockwise size={14} weight="bold" className={loading ? "animate-spin" : ""} />
                    </button>
                    <button
                        onClick={() => window.print()}
                        className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-surface-active text-xs font-medium text-ink hover:bg-surface-hover transition-colors"
                        title="Print / Save as PDF"
                    >
                        <DownloadSimple size={13} weight="bold" />
                        Export
                    </button>
                </div>
            </div>

            {error && (
                <div className="surface border border-red-500/30 p-3 text-sm text-red-400 flex items-center gap-2">
                    <Warning size={14} /> {error}
                </div>
            )}

            {/* Report card */}
            <div id="funnel-report-print" className="surface p-5">
                {/* Column headers */}
                <div className="flex items-center gap-3 mb-4 pb-3 border-b border-strokes">
                    <div className="flex-1 text-center">
                        <div className="text-[11px] font-bold text-emerald-400 uppercase tracking-widest">What Good</div>
                        <div className="text-[11px] font-bold text-emerald-400 uppercase tracking-widest">Looks Like</div>
                    </div>
                    <div className="w-px h-8 bg-strokes flex-shrink-0" />
                    <div className="flex-1 text-center">
                        <div className="text-[11px] font-bold text-brand-primary uppercase tracking-widest">
                            What We Look Like
                        </div>
                        {report?.week_label && (
                            <div className="text-[10px] text-ink-muted">{report.week_label}</div>
                        )}
                    </div>
                </div>

                {/* Recruitment funnel — candidates added this period */}
                <div className="mb-1">
                    <div className="flex items-center gap-2 mb-2">
                        <span className="text-[10px] font-semibold uppercase tracking-widest text-ink-muted">
                            Recruitment — candidates added {report?.week_label || getPeriodLabel(periodStart, viewMode)}
                        </span>
                        <div className="flex-1 h-px bg-strokes" />
                    </div>
                    {recruitSteps.map((step) => (
                        <StepRow
                            key={step.rKey}
                            leftLabel={step.left}
                            rightLabel={step.right}
                            benchmarkPct={b[step.bKey]}
                            actualPct={r[step.rKey] ?? null}
                            loading={loading}
                            fromCount={step.leftCount}
                            toCount={step.rightCount}
                        />
                    ))}
                </div>

                {/* New starters — candidates whose training started this period */}
                <div className="mt-4">
                    <div className="flex items-center gap-2 mb-2">
                        <span className="text-[10px] font-semibold uppercase tracking-widest text-brand-primary">
                            New starters — {report?.week_label || getPeriodLabel(periodStart, viewMode)}
                            {a.new_starters != null ? ` (${a.new_starters} starters)` : ""}
                        </span>
                        <div className="flex-1 h-px bg-brand-primary/30" />
                    </div>
                    {starterSteps.map((step) => (
                        <StepRow
                            key={step.rKey}
                            leftLabel={step.left}
                            rightLabel={step.right}
                            benchmarkPct={b[step.bKey]}
                            actualPct={r[step.rKey] ?? null}
                            loading={loading}
                            fromCg1={step.fromCg1}
                            fromCount={step.leftCount}
                            toCount={step.rightCount}
                        />
                    ))}
                </div>

                {/* Legend */}
                <div className="flex items-center gap-4 mt-4 pt-3 border-t border-strokes justify-center">
                    {[
                        { color: "linear-gradient(135deg,#10B981,#059669)", label: "On target (≤5% below)" },
                        { color: "linear-gradient(135deg,#F59E0B,#D97706)", label: "<10% below" },
                        { color: "linear-gradient(135deg,#EF4444,#DC2626)", label: ">10% below" },
                    ].map(({ color, label }) => (
                        <div key={label} className="flex items-center gap-1.5">
                            <div className="w-3 h-3 rounded-full flex-shrink-0" style={{ background: color }} />
                            <span className="text-[10px] text-ink-muted">{label}</span>
                        </div>
                    ))}
                </div>
            </div>

            {/* Raw counts reference */}
            {report && !loading && (
                <div className="surface p-4 space-y-4">
                    {/* Recruitment cohort counts */}
                    <div>
                        <div className="label-overline mb-2">
                            Recruitment — {report.week_label}
                        </div>
                        <div className="grid grid-cols-3 sm:grid-cols-6 gap-2">
                            {[
                                { label: "Added",          val: a.added },
                                { label: "AI Screened",    val: a.screened },
                                { label: "Booked",         val: a.booked },
                                { label: "Attended Pres",  val: a.attended },
                                { label: "Form Submitted", val: a.form_submitted },
                                { label: "Booked to Start",val: a.recruited_to_training },
                            ].map(({ label, val }) => (
                                <div key={label} className="text-center p-2 rounded-md bg-surface-active">
                                    <div className="font-heading text-lg font-bold tabular-nums">
                                        {val ?? <span className="text-ink-muted text-sm">—</span>}
                                    </div>
                                    <div className="text-[10px] text-ink-muted mt-0.5">{label}</div>
                                </div>
                            ))}
                        </div>
                    </div>

                    <div className="h-px bg-strokes" />

                    {/* New starters cohort counts */}
                    <div>
                        <div className="label-overline mb-2 text-brand-primary">
                            New starters — {report.week_label} ({a.new_starters ?? "—"} people)
                        </div>
                        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                            {[
                                { label: "BA's Booked for Training",  val: a.new_starters },
                                { label: "BA's Attending Training",   val: a.training_attended },
                                { label: "BA's Badged",               val: a.badged },
                                { label: "BA's doing First Sale",     val: a.made_sale },
                                { label: "BA Completing Week 1",      val: a.week1_complete },
                            ].map(({ label, val }) => (
                                <div key={label} className="text-center p-2 rounded-md bg-surface-active">
                                    <div className="font-heading text-lg font-bold tabular-nums">
                                        {val ?? <span className="text-ink-muted text-sm">—</span>}
                                    </div>
                                    <div className="text-[10px] text-ink-muted mt-0.5">{label}</div>
                                </div>
                            ))}
                        </div>
                    </div>

                    {!report.cg1_available && (
                        <p className="text-[11px] text-ink-muted flex items-center gap-1">
                            <Warning size={11} /> CG1 not configured for this pipeline — badge and sales data unavailable.
                        </p>
                    )}
                    {report.cg1_available && report.starters_sent_to_cg1 > 0 && (
                        <p className="text-[11px] text-ink-muted flex items-center gap-1">
                            <CheckCircle size={11} className="text-emerald-400" />
                            {report.cg1_names_resolved} of {report.starters_sent_to_cg1} new starters matched in CG1.
                        </p>
                    )}
                </div>
            )}
        </div>
    );
}
