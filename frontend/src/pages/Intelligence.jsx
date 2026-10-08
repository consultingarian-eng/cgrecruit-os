import { useEffect, useState, useCallback } from "react";
import { usePipeline } from "@/lib/pipeline";
import { useAuth } from "@/lib/auth";
import { ArrowLeft, ArrowRight, UserPlus, MicrophoneStage, CalendarBlank, CheckCircle, ClipboardText, Checks, GraduationCap, Archive, Export, ArrowFatLineRight, X, Timer, Door } from "@phosphor-icons/react";
import api from "@/lib/api";

// ── Week helpers (Mon–Sun) ────────────────────────────────────────────────────
function toISO(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
}

function getThisWeekMonday() {
    const today = new Date();
    const day = today.getDay(); // 0=Sun
    const diff = day === 0 ? -6 : 1 - day; // shift to Monday
    const mon = new Date(today);
    mon.setDate(today.getDate() + diff);
    return toISO(mon);
}

function addWeeks(mondayISO, n) {
    const d = new Date(mondayISO + "T00:00:00");
    d.setDate(d.getDate() + n * 7);
    return toISO(d);
}

function weekLabel(mondayISO) {
    const mon = new Date(mondayISO + "T00:00:00");
    const sun = new Date(mon);
    sun.setDate(mon.getDate() + 6);
    const fmt = (d) => d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
    return `${fmt(mon)} – ${fmt(sun)}`;
}

function WeekPicker({ value, onChange }) {
    const thisWeek = getThisWeekMonday();
    return (
        <div className="flex items-center gap-1">
            <button
                onClick={() => onChange(addWeeks(value, -1))}
                className="p-1.5 rounded text-ink-muted hover:text-ink hover:bg-surface-active transition-colors"
            >
                <ArrowLeft size={14} weight="bold" />
            </button>
            <div className="px-3 py-1.5 rounded-md bg-surface-active text-xs font-medium text-ink min-w-[150px] text-center">
                {weekLabel(value)}
            </div>
            <button
                onClick={() => onChange(addWeeks(value, 1))}
                disabled={value >= thisWeek}
                className="p-1.5 rounded text-ink-muted hover:text-ink hover:bg-surface-active transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
            >
                <ArrowRight size={14} weight="bold" />
            </button>
            {value !== thisWeek && (
                <button
                    onClick={() => onChange(thisWeek)}
                    className="ml-1 px-2 py-1 rounded text-[11px] text-brand-primary hover:bg-surface-active transition-colors"
                >
                    This week
                </button>
            )}
        </div>
    );
}

// ── Stat card ─────────────────────────────────────────────────────────────────
function Stat({ icon, label, value, sub, color = "#6366F1", faded }) {
    return (
        <div className={`surface p-4 flex flex-col gap-1 ${faded ? "opacity-60" : ""}`}>
            <div className="flex items-center gap-2 mb-1">
                <div className="p-1.5 rounded" style={{ background: `${color}1A`, color }}>{icon}</div>
                <div className="label-overline">{label}</div>
            </div>
            <div className="font-heading text-3xl font-bold tabular-nums">{value ?? "—"}</div>
            {sub != null && (
                <div className="text-[11px] text-ink-muted">{sub}</div>
            )}
        </div>
    );
}

// ── Funnel bar ────────────────────────────────────────────────────────────────
function FunnelBar({ steps }) {
    const max = steps[0]?.value || 1;
    return (
        <div className="surface p-4 md:p-5">
            <div className="label-overline mb-4">Cohort Funnel</div>
            <div className="space-y-2.5">
                {steps.map((step, i) => {
                    const pct = max > 0 ? Math.round((step.value / max) * 100) : 0;
                    const conv = i > 0 && steps[i - 1].value > 0
                        ? Math.round((step.value / steps[i - 1].value) * 100)
                        : null;
                    return (
                        <div key={step.label}>
                            <div className="flex items-center justify-between text-xs mb-1">
                                <span className="text-ink-muted">{step.label}</span>
                                <div className="flex items-center gap-2">
                                    {conv !== null && (
                                        <span className={`font-semibold tabular-nums ${conv >= 60 ? "text-emerald-400" : conv >= 30 ? "text-amber-400" : "text-red-400"}`}>
                                            {conv}% step
                                        </span>
                                    )}
                                    <span className="font-mono font-semibold text-ink tabular-nums">{step.value}</span>
                                </div>
                            </div>
                            <div className="h-2 bg-surface-active rounded-full overflow-hidden">
                                <div
                                    className="h-full rounded-full transition-all duration-500"
                                    style={{ width: `${pct}%`, background: step.color || "#6366F1" }}
                                />
                            </div>
                        </div>
                    );
                })}
            </div>
        </div>
    );
}

// ── Stage distribution pills ───────────────────────────────────────────────────
const STAGE_COLORS = {
    SCREENING: "#3B82F6",
    APPOINTMENT: "#8B5CF6",
    FORM: "#F59E0B",
    CLOSE: "#10B981",
    TRAINING: "#14B8A6",
};

function StageDist({ stages, archived, total }) {
    if (!stages) return null;
    const rows = Object.entries(stages).concat([["Archived", archived]]).filter(([, v]) => v > 0);
    return (
        <div className="surface p-4 md:p-5">
            <div className="label-overline mb-3">Cohort — Current Stage</div>
            <div className="grid grid-cols-3 md:grid-cols-6 gap-2">
                {rows.map(([stage, count]) => (
                    <div key={stage} className="rounded-md border border-strokes p-2.5 text-center">
                        <div className="text-[10px] uppercase tracking-widest text-ink-muted mb-1">{stage}</div>
                        <div className="font-heading text-xl font-bold tabular-nums" style={{ color: STAGE_COLORS[stage] || "#8888a8" }}>
                            {count}
                        </div>
                        <div className="text-[10px] text-ink-muted mt-0.5">
                            {total > 0 ? Math.round((count / total) * 100) : 0}%
                        </div>
                    </div>
                ))}
            </div>
        </div>
    );
}

// ── Activity rate bar ─────────────────────────────────────────────────────────
function RateBar({ label, value, of: ofVal, color }) {
    const pct = ofVal > 0 ? Math.round((value / ofVal) * 100) : 0;
    return (
        <div>
            <div className="flex items-center justify-between text-xs mb-1">
                <span className="text-ink-muted">{label}</span>
                <span className="font-mono font-semibold text-ink">{value} / {ofVal} <span className={`ml-1 font-semibold ${pct >= 60 ? "text-emerald-400" : pct >= 30 ? "text-amber-400" : "text-red-400"}`}>({pct}%)</span></span>
            </div>
            <div className="h-2 bg-surface-active rounded-full overflow-hidden">
                <div className="h-full rounded-full transition-all duration-500" style={{ width: `${pct}%`, background: color }} />
            </div>
        </div>
    );
}

// ── Exit pipeline ─────────────────────────────────────────────────────────────
const EXIT_COLORS = {
    no_show:          "#EF4444",
    uncontactable:    "#F97316",
    withdrawn:        "#EAB308",
    attended_no_form: "#10B981",
    rejected:         "#8B5CF6",
    duplicate:        "#6366F1",
    manual:           "#6B7280",
    other:            "#94A3B8",
};

function Sparkline({ values, color }) {
    const max = Math.max(...values, 1);
    const w = 56, h = 24, pad = 2;
    const step = (w - pad * 2) / (values.length - 1);
    const pts = values.map((v, i) => {
        const x = pad + i * step;
        const y = h - pad - ((v / max) * (h - pad * 2));
        return `${x},${y}`;
    }).join(" ");
    return (
        <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} className="flex-shrink-0">
            <polyline
                points={pts}
                fill="none"
                stroke={color}
                strokeWidth="1.5"
                strokeLinejoin="round"
                strokeLinecap="round"
            />
            {values.map((v, i) => {
                const x = pad + i * step;
                const y = h - pad - ((v / max) * (h - pad * 2));
                return <circle key={i} cx={x} cy={y} r="2" fill={color} />;
            })}
        </svg>
    );
}

function ExitPipeline({ exits, exitPeriod, onPeriodChange }) {
    if (!exits) return null;
    const { total, by_reason, trend_weeks } = exits;
    const active = by_reason.filter((r) => r.count > 0);

    const periodOptions = [
        { label: "Last 30 days", value: "30d" },
        { label: "Last 90 days", value: "90d" },
        { label: "All time",     value: "all" },
    ];

    return (
        <div className="space-y-3">
            {/* Header row with period selector */}
            <div className="flex items-center justify-between flex-wrap gap-2">
                <div>
                    <div className="font-heading font-semibold text-base flex items-center gap-2">
                        <Door size={16} className="text-brand-primary" weight="bold" />
                        Exit Pipeline
                    </div>
                    <div className="text-xs text-ink-muted mt-0.5">
                        Why candidates left the funnel · {total} total exit{total !== 1 ? "s" : ""}
                    </div>
                </div>
                <div className="flex items-center gap-1 bg-surface-active rounded-lg p-1">
                    {periodOptions.map((opt) => (
                        <button
                            key={opt.value}
                            onClick={() => onPeriodChange(opt.value)}
                            className={`px-3 py-1 rounded-md text-xs font-medium transition-colors ${
                                exitPeriod === opt.value
                                    ? "bg-surface text-ink shadow-sm"
                                    : "text-ink-muted hover:text-ink"
                            }`}
                        >
                            {opt.label}
                        </button>
                    ))}
                </div>
            </div>

            {total === 0 ? (
                <div className="surface p-6 text-center text-sm text-ink-muted">
                    No exits in this period — all candidates are still active.
                </div>
            ) : (
                <div className="surface p-4 md:p-5 space-y-1">
                    {/* Sparkline week labels */}
                    <div className="flex items-center justify-end gap-1 mb-2 pr-1">
                        <div className="text-[9px] text-ink-muted mr-1 italic">4-week trend →</div>
                        {trend_weeks?.map((ws) => {
                            const d = new Date(ws + "T00:00:00");
                            return (
                                <div key={ws} className="w-14 text-center text-[9px] text-ink-muted">
                                    {d.toLocaleDateString("en-US", { month: "short", day: "numeric" })}
                                </div>
                            );
                        })}
                    </div>

                    {/* Per-reason rows */}
                    {(active.length ? active : by_reason).map((row) => {
                        const color = EXIT_COLORS[row.key] || "#94A3B8";
                        return (
                            <div key={row.key} className="group">
                                <div className="flex items-center gap-3 py-2">
                                    {/* Label + bar */}
                                    <div className="flex-1 min-w-0">
                                        <div className="flex items-center justify-between mb-1">
                                            <span className="text-xs font-medium text-ink">{row.label}</span>
                                            <div className="flex items-center gap-2 flex-shrink-0 ml-2">
                                                <span className="font-mono text-xs font-semibold tabular-nums text-ink">{row.count}</span>
                                                <span className="text-[10px] font-semibold tabular-nums" style={{ color }}>
                                                    {row.pct}%
                                                </span>
                                            </div>
                                        </div>
                                        <div className="h-1.5 bg-surface-active rounded-full overflow-hidden">
                                            <div
                                                className="h-full rounded-full transition-all duration-500"
                                                style={{ width: `${row.pct}%`, background: color }}
                                            />
                                        </div>
                                    </div>

                                    {/* Sparkline */}
                                    <Sparkline values={row.trend} color={color} />
                                </div>
                                <div className="border-b border-strokes last:border-0" />
                            </div>
                        );
                    })}

                    {/* Summary footer */}
                    <div className="pt-2 flex items-center justify-between text-[11px] text-ink-muted">
                        <span>Sparkline shows Mon–Sun weekly exits (oldest → newest)</span>
                        <span className="font-semibold text-ink tabular-nums">{total} total</span>
                    </div>
                </div>
            )}
        </div>
    );
}

// ── Velocity funnel ───────────────────────────────────────────────────────────
const VELOCITY_COLORS = ["#3B82F6", "#6366F1", "#8B5CF6", "#F59E0B", "#10B981", "#14B8A6"];

function fmtDuration(avgDays) {
    if (avgDays == null) return "—";
    const minutes = avgDays * 24 * 60;
    if (minutes < 60) return `${Math.round(minutes)}m`;
    const hours = avgDays * 24;
    if (hours < 24) return `${hours.toFixed(1)}h`;
    return `${avgDays.toFixed(1)}d`;
}

function VelocityFunnel({ velocity }) {
    if (!velocity?.length) return null;

    // Build node list: from_stage of first step + all to_stage values
    const nodes = [velocity[0].from_stage, ...velocity.map((v) => v.to_stage)];

    return (
        <div className="surface p-4 md:p-5">
            <div className="label-overline mb-4">Average Time Per Stage</div>

            {/* Desktop: horizontal chain */}
            <div className="hidden md:flex items-center gap-0 overflow-x-auto pb-1">
                {nodes.map((node, i) => {
                    const step = velocity[i]; // arrow AFTER this node (undefined for last)
                    const color = VELOCITY_COLORS[i % VELOCITY_COLORS.length];
                    return (
                        <div key={node} className="flex items-center gap-0 flex-shrink-0">
                            {/* Stage box */}
                            <div className="flex flex-col items-center">
                                <div
                                    className="rounded-lg px-3 py-2 text-center min-w-[90px]"
                                    style={{ background: `${color}1A`, border: `1px solid ${color}40` }}
                                >
                                    <div className="text-[10px] uppercase tracking-widest font-semibold" style={{ color }}>
                                        {node}
                                    </div>
                                </div>
                            </div>

                            {/* Arrow + days label */}
                            {step && (
                                <div className="flex flex-col items-center mx-1 flex-shrink-0" style={{ minWidth: 64 }}>
                                    <div className="text-[11px] font-semibold tabular-nums text-ink mb-0.5">
                                        {fmtDuration(step.avg_days)}
                                    </div>
                                    <div className="flex items-center gap-0.5 w-full">
                                        <div className="h-px flex-1 bg-strokes" />
                                        <ArrowRight size={10} className="text-ink-muted flex-shrink-0" weight="bold" />
                                    </div>
                                    <div className="text-[9px] text-ink-muted mt-0.5 tabular-nums">
                                        {step.sample_size} cvs
                                    </div>
                                </div>
                            )}
                        </div>
                    );
                })}
            </div>

            {/* Mobile: vertical stacked list */}
            <div className="md:hidden space-y-2">
                {velocity.map((step, i) => {
                    const color = VELOCITY_COLORS[i % VELOCITY_COLORS.length];
                    return (
                        <div key={i} className="flex items-center gap-3 rounded-lg p-3" style={{ background: `${color}0D` }}>
                            <div
                                className="w-8 h-8 rounded-full flex items-center justify-center flex-shrink-0 text-xs font-bold"
                                style={{ background: `${color}22`, color }}
                            >
                                {i + 1}
                            </div>
                            <div className="flex-1 min-w-0">
                                <div className="text-xs font-medium text-ink">
                                    {step.from_stage} → {step.to_stage}
                                </div>
                                <div className="text-[10px] text-ink-muted">
                                    {step.sample_size} candidate{step.sample_size !== 1 ? "s" : ""} measured
                                </div>
                            </div>
                            <div className="text-right flex-shrink-0">
                                <div className="font-heading text-lg font-bold tabular-nums" style={{ color }}>
                                    {fmtDuration(step.avg_days)}
                                </div>
                                <div className="text-[9px] text-ink-muted">avg</div>
                            </div>
                        </div>
                    );
                })}
            </div>

            {/* Summary row */}
            {velocity.some((v) => v.avg_days != null) && (
                <div className="mt-4 pt-3 border-t border-strokes flex items-center justify-between text-xs text-ink-muted">
                    <span>End-to-end (Added → Training)</span>
                    <span className="font-semibold text-ink tabular-nums">
                        ~{fmtDuration(velocity.reduce((sum, v) => sum + (v.avg_days ?? 0), 0))} total
                    </span>
                </div>
            )}
        </div>
    );
}

// ── Main page ─────────────────────────────────────────────────────────────────
// ── Attendance patterns heatmap (day-of-week × hour show-rate) ───────────────
const DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function heatColor(rate, booked) {
    if (booked === 0) return "transparent";
    if (rate >= 70) return "rgba(16,185,129,0.35)";
    if (rate >= 50) return "rgba(16,185,129,0.18)";
    if (rate >= 35) return "rgba(245,158,11,0.22)";
    return "rgba(239,68,68,0.25)";
}

function fmtHour(h) {
    if (h === 0) return "12a";
    if (h < 12) return `${h}a`;
    if (h === 12) return "12p";
    return `${h - 12}p`;
}

function AttendanceHeatmap({ patterns }) {
    if (!patterns?.cells?.length) return null;
    const { cells, total_marked } = patterns;

    // Only render the hour range that actually has data.
    const hours = [...new Set(cells.map((c) => c.hour))].sort((a, b) => a - b);
    const days = [...new Set(cells.map((c) => c.day))].sort((a, b) => a - b);
    const byKey = Object.fromEntries(cells.map((c) => [`${c.day}-${c.hour}`, c]));

    // Best + worst slots (min 3 booked so tiny samples don't dominate)
    const qualified = cells.filter((c) => c.booked >= 3);
    const best = qualified.length ? qualified.reduce((a, b) => (b.show_rate > a.show_rate ? b : a)) : null;
    const worst = qualified.length ? qualified.reduce((a, b) => (b.show_rate < a.show_rate ? b : a)) : null;

    return (
        <div className="space-y-3">
            <div className="surface p-4 md:p-5 overflow-x-auto">
                <table className="border-separate" style={{ borderSpacing: "3px" }}>
                    <thead>
                        <tr>
                            <th className="text-[10px] text-ink-muted font-normal text-left pr-2" />
                            {hours.map((h) => (
                                <th key={h} className="text-[10px] text-ink-muted font-normal px-1 pb-1 text-center min-w-[52px]">
                                    {fmtHour(h)}
                                </th>
                            ))}
                        </tr>
                    </thead>
                    <tbody>
                        {days.map((d) => (
                            <tr key={d}>
                                <td className="text-[11px] text-ink-muted pr-2 font-medium">{DAY_LABELS[d]}</td>
                                {hours.map((h) => {
                                    const c = byKey[`${d}-${h}`];
                                    if (!c) return <td key={h} className="rounded" style={{ background: "rgba(255,255,255,0.02)", height: 44 }} />;
                                    const lowSample = c.booked < 3;
                                    return (
                                        <td
                                            key={h}
                                            className="rounded text-center align-middle"
                                            style={{ background: heatColor(c.show_rate, c.booked), height: 44, opacity: lowSample ? 0.55 : 1 }}
                                            title={`${DAY_LABELS[d]} ${fmtHour(h)} — ${c.attended}/${c.booked} attended (${c.show_rate}%)${lowSample ? " · small sample" : ""}`}
                                        >
                                            <div className="text-xs font-semibold tabular-nums leading-tight">{Math.round(c.show_rate)}%</div>
                                            <div className="text-[9px] text-ink-muted tabular-nums leading-tight">{c.attended}/{c.booked}</div>
                                        </td>
                                    );
                                })}
                            </tr>
                        ))}
                    </tbody>
                </table>
                <div className="flex items-center gap-4 mt-3 text-[10px] text-ink-muted flex-wrap">
                    <span>{total_marked} marked appointments · all time</span>
                    <span className="flex items-center gap-1"><span className="inline-block w-2.5 h-2.5 rounded-sm" style={{ background: "rgba(16,185,129,0.35)" }} /> ≥70%</span>
                    <span className="flex items-center gap-1"><span className="inline-block w-2.5 h-2.5 rounded-sm" style={{ background: "rgba(245,158,11,0.22)" }} /> 35–49%</span>
                    <span className="flex items-center gap-1"><span className="inline-block w-2.5 h-2.5 rounded-sm" style={{ background: "rgba(239,68,68,0.25)" }} /> &lt;35%</span>
                    <span>Faded = under 3 bookings</span>
                </div>
            </div>
            {best && worst && best !== worst && (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                    <div className="surface p-3.5 border-l-2" style={{ borderLeftColor: "#10B981" }}>
                        <div className="text-[10px] uppercase tracking-widest text-ink-muted mb-0.5">Best slot</div>
                        <div className="text-sm font-medium">
                            {DAY_LABELS[best.day]} {fmtHour(best.hour)} — <span className="text-emerald-400 font-semibold">{best.show_rate}%</span> show ({best.attended}/{best.booked})
                        </div>
                    </div>
                    <div className="surface p-3.5 border-l-2" style={{ borderLeftColor: "#EF4444" }}>
                        <div className="text-[10px] uppercase tracking-widest text-ink-muted mb-0.5">Worst slot</div>
                        <div className="text-sm font-medium">
                            {DAY_LABELS[worst.day]} {fmtHour(worst.hour)} — <span className="text-red-400 font-semibold">{worst.show_rate}%</span> show ({worst.attended}/{worst.booked})
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}


// Pulsing placeholder shown while a section's data is in flight.
function SectionSkeleton({ rows = 3 }) {
    return (
        <div className="surface p-4 md:p-5 space-y-2.5" data-testid="section-skeleton">
            {Array.from({ length: rows }).map((_, i) => (
                <div key={i} className="h-6 rounded bg-surface-active animate-pulse" style={{ animationDelay: `${i * 80}ms`, width: `${90 - i * 12}%` }} />
            ))}
        </div>
    );
}

export default function IntelligencePage() {
    const { activePipelineId, pipelines } = usePipeline();
    const { isSuperAdmin, isAnalyst } = useAuth();

    // Default to the active pipeline; super-admins can switch to null (all pipelines)
    const [selectedPipelineId, setSelectedPipelineId] = useState(() => activePipelineId || null);

    // If the global active pipeline changes (e.g. user switches in the sidebar),
    // follow it — but only if the user hasn't explicitly chosen a different view.
    useEffect(() => {
        if (activePipelineId) setSelectedPipelineId((prev) => prev ?? activePipelineId);
    }, [activePipelineId]);

    const [cohortWeek, setCohortWeek] = useState(getThisWeekMonday);
    const [activityWeek, setActivityWeek] = useState(getThisWeekMonday);
    const [cohortData, setCohortData] = useState(null);
    const [activityData, setActivityData] = useState(null);
    const [velocityData, setVelocityData] = useState(null);
    const [exitsData, setExitsData] = useState(null);
    const [attendancePatterns, setAttendancePatterns] = useState(null);
    const [exitPeriod, setExitPeriod] = useState("30d");
    const [exporting, setExporting] = useState(false);

    const fetchCohort = useCallback(() => {
        const params = { week_start: cohortWeek };
        if (selectedPipelineId) params.pipeline_id = selectedPipelineId;
        api.get("/intelligence/cohort-week", { params })
            .then((r) => setCohortData(r.data)).catch(() => {});
    }, [selectedPipelineId, cohortWeek]);

    const fetchActivity = useCallback(() => {
        const params = { week_start: activityWeek };
        if (selectedPipelineId) params.pipeline_id = selectedPipelineId;
        api.get("/intelligence/activity-week", { params })
            .then((r) => setActivityData(r.data)).catch(() => {});
    }, [selectedPipelineId, activityWeek]);

    const fetchDashboard = useCallback(() => {
        const params = {};
        if (selectedPipelineId) params.pipeline_id = selectedPipelineId;
        if (exitPeriod !== "all") {
            const today = new Date();
            const days = exitPeriod === "30d" ? 30 : 90;
            const from = new Date(today);
            from.setDate(today.getDate() - days);
            params.exit_date_from = from.toISOString().slice(0, 10);
            params.exit_date_to = today.toISOString().slice(0, 10);
        }
        api.get("/intelligence/dashboard", { params })
            .then((r) => {
                setVelocityData(r.data?.velocity ?? null);
                setExitsData(r.data?.exits ?? null);
                setAttendancePatterns(r.data?.attendance_patterns ?? null);
            }).catch(() => {});
    }, [selectedPipelineId, exitPeriod]);

    useEffect(() => { fetchCohort(); }, [fetchCohort]);
    useEffect(() => { fetchActivity(); }, [fetchActivity]);
    useEffect(() => { fetchDashboard(); }, [fetchDashboard]);

    const exportCSV = async () => {
        setExporting(true);
        try {
            const params = new URLSearchParams({
                date_field: "created_at",
                date_from: cohortWeek,
                date_to: addWeeks(cohortWeek, 1),
            });
            if (selectedPipelineId) params.set("pipeline_id", selectedPipelineId);
            const res = await api.get(`/intelligence/export-csv?${params}`, { responseType: "blob" });
            const url = URL.createObjectURL(new Blob([res.data], { type: "text/csv" }));
            const a = document.createElement("a");
            a.href = url;
            const pipeSlug = selectedPipelineId
                ? (pipelines.find((p) => p.id === selectedPipelineId)?.name || selectedPipelineId).toLowerCase().replace(/\s+/g, "-")
                : "all-pipelines";
            a.download = `cohort-${cohortWeek}-${pipeSlug}.csv`;
            a.click();
            URL.revokeObjectURL(url);
        } catch { /* silently fail */ }
        finally { setExporting(false); }
    };

    const selectedPipelineName = selectedPipelineId
        ? (pipelines.find((p) => p.id === selectedPipelineId)?.name || "Pipeline")
        : "All Pipelines";

    const f = cohortData?.funnel;
    const act = activityData;

    const cohortSteps = f ? [
        { label: "Resumes Added", value: f.added, color: "#3B82F6" },
        { label: "Attempted (dialled)", value: f.screened_attempted, color: "#6366F1" },
        { label: "Screened (spoke to)", value: f.screened_successful, color: "#8B5CF6" },
        { label: "Booked (any date)", value: f.booked, color: "#7C3AED" },
        { label: "Attended", value: f.attended, color: "#10B981" },
        { label: "Form Sent", value: f.invited_to_form, color: "#84CC16" },
        { label: "Form Submitted", value: f.forms_submitted, color: "#F59E0B" },
        { label: "Booked to Start", value: f.training, color: "#14B8A6" },
    ] : [];

    return (
        <div className="p-4 md:p-6 space-y-6 overflow-auto h-[calc(100vh-3.5rem)]">
            {/* Header */}
            <div className="flex items-start justify-between gap-3 flex-wrap">
                <div>
                    <h1 className="font-heading text-2xl md:text-3xl font-bold tracking-tight">Pipeline Intelligence</h1>
                    <p className="text-sm text-ink-muted mt-0.5">Tracking every candidate's journey through the funnel</p>
                </div>
                <div className="flex items-center gap-2 flex-wrap">
                    {/* Pipeline selector — "All" only available to super-admins */}
                    <select
                        value={selectedPipelineId || ""}
                        onChange={(e) => setSelectedPipelineId(e.target.value || null)}
                        className="px-3 py-1.5 rounded-md text-xs font-medium bg-surface border border-strokes text-ink focus:outline-none focus:border-brand-primary"
                    >
                        {isSuperAdmin && <option value="">All Pipelines</option>}
                        {pipelines.map((p) => (
                            <option key={p.id} value={p.id}>{p.name}</option>
                        ))}
                    </select>
                    {/* The CSV lists candidates by name; analyst accounts get figures only. */}
                    {!isAnalyst && (
                        <button
                            onClick={exportCSV}
                            disabled={exporting}
                            className="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium bg-surface border border-strokes text-ink-muted hover:text-ink hover:border-brand-primary transition-colors disabled:opacity-50"
                        >
                            <Export size={13} />
                            <span className="hidden sm:inline">{exporting ? "Exporting…" : "Export CSV"}</span>
                        </button>
                    )}
                </div>
            </div>
            {/* Selected pipeline label */}
            <div className="text-xs font-semibold text-brand-primary uppercase tracking-widest -mt-4">
                {selectedPipelineName}
            </div>

            {/* ─── Section 1: Cohort View ───────────────────────────────── */}
            <section className="space-y-3">
                <div className="flex items-center justify-between flex-wrap gap-2">
                    <div>
                        <div className="font-heading font-semibold text-base">Cohort: Added This Week</div>
                        <div className="text-xs text-ink-muted mt-0.5">Resumes added Mon–Sun · tracks what stage they reached (includes archived)</div>
                    </div>
                    <WeekPicker value={cohortWeek} onChange={setCohortWeek} />
                </div>

                {!cohortData ? (
                    <SectionSkeleton />
                ) : (
                    <>
                        {/* Top KPI strip */}
                        <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
                            <Stat icon={<UserPlus size={14} weight="bold" />} label="Resumes Added" value={f.added} color="#3B82F6"
                                sub={cohortData.archived > 0 ? `${cohortData.active} active · ${cohortData.archived} archived` : `${f.added} active`} />
                            <Stat icon={<CheckCircle size={14} weight="bold" />} label="Attended" value={f.attended} color="#10B981"
                                sub={f.booked > 0 ? `${Math.round((f.attended / f.booked) * 100)}% of booked` : null} />
                            <Stat icon={<ClipboardText size={14} weight="bold" />} label="Form Sent" value={f.invited_to_form} color="#84CC16"
                                sub={f.attended > 0 ? `${Math.round(((f.invited_to_form ?? 0) / f.attended) * 100)}% of attended` : null} />
                            <Stat icon={<Checks size={14} weight="bold" />} label="Form Submitted" value={f.forms_submitted} color="#F59E0B"
                                sub={f.invited_to_form > 0 ? `${Math.round((f.forms_submitted / f.invited_to_form) * 100)}% of form sent` : null} />
                            <Stat icon={<GraduationCap size={14} weight="bold" />} label="Booked to Start" value={f.training} color="#14B8A6"
                                sub={f.added > 0 ? `${Math.round((f.training / f.added) * 100)}% of added` : null} />
                        </div>

                        {/* Funnel + stage distribution side by side on desktop */}
                        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
                            <FunnelBar steps={cohortSteps} />
                            {/* Pass active total so stage-pill percentages only count active candidates */}
                            <StageDist stages={cohortData.stages} archived={cohortData.archived} total={cohortData.active ?? f.added} />
                        </div>
                    </>
                )}
            </section>

            {/* Divider */}
            <div className="border-t border-strokes" />

            {/* ─── Section 2: Activity View ─────────────────────────────── */}
            <section className="space-y-3">
                <div className="flex items-center justify-between flex-wrap gap-2">
                    <div>
                        <div className="font-heading font-semibold text-base">Activity: This Week's Movements</div>
                        <div className="text-xs text-ink-muted mt-0.5">Appointment dates + stage moves that happened Mon–Sun · any application date</div>
                    </div>
                    <WeekPicker value={activityWeek} onChange={setActivityWeek} />
                </div>

                {!activityData ? (
                    <SectionSkeleton />
                ) : (
                    <>
                        {/* 5 KPI cards */}
                        <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
                            <Stat icon={<CalendarBlank size={14} weight="bold" />} label="Appts Due" value={act.appointments_due} color="#8B5CF6"
                                sub="scheduled this week" />
                            <Stat icon={<CheckCircle size={14} weight="bold" />} label="Attended" value={act.attended} color="#10B981"
                                sub={act.appointments_due > 0 ? `${Math.round((act.attended / act.appointments_due) * 100)}% of due` : null} />
                            <Stat icon={<ClipboardText size={14} weight="bold" />} label="Forms Sent" value={act.forms_sent} color="#84CC16"
                                sub={act.attended > 0 ? `${Math.round(((act.forms_sent ?? 0) / act.attended) * 100)}% of attended` : "sent this week"} />
                            <Stat icon={<Checks size={14} weight="bold" />} label="Forms Submitted" value={act.moved_to_close} color="#F59E0B"
                                sub="submitted this week" />
                            <Stat icon={<GraduationCap size={14} weight="bold" />} label="Booked to Start" value={act.training_booked} color="#14B8A6"
                                sub="booked this week" />
                        </div>

                        {/* Rate bars for attended / close / training */}
                        {act.appointments_due > 0 && (
                            <div className="surface p-4 md:p-5 space-y-3">
                                <div className="label-overline mb-1">This Week's Appointment Outcomes</div>
                                <RateBar label="Attended" value={act.attended} of={act.appointments_due} color="#10B981" />
                                <RateBar label="No-Show" value={act.no_show ?? 0} of={act.appointments_due} color="#EF4444" />
                                <RateBar label="Pending / Upcoming" value={act.appointments_due - act.attended - (act.no_show ?? 0)} of={act.appointments_due} color="#6366F1" />
                            </div>
                        )}
                    </>
                )}
            </section>

            {/* Divider */}
            <div className="border-t border-strokes" />

            {/* ─── Section 3: Recruitment Velocity ─────────────────────── */}
            <section className="space-y-3">
                <div className="flex items-center justify-between flex-wrap gap-2">
                    <div>
                        <div className="font-heading font-semibold text-base flex items-center gap-2">
                            <Timer size={16} className="text-brand-primary" weight="bold" />
                            Recruitment Velocity
                        </div>
                        <div className="text-xs text-ink-muted mt-0.5">
                            Average days between each stage milestone · all time · {selectedPipelineName}
                        </div>
                    </div>
                </div>

                {!velocityData ? (
                    <SectionSkeleton />
                ) : velocityData.every((v) => v.avg_days == null) ? (
                    <div className="surface p-6 text-center text-sm text-ink-muted">
                        Not enough data yet — velocity calculates once candidates have progressed through stages.
                    </div>
                ) : (
                    <VelocityFunnel velocity={velocityData} />
                )}
            </section>

            {/* Divider */}
            <div className="border-t border-strokes" />

            {/* ─── Section 4: Exit Pipeline ────────────────────────────── */}
            <section>
                {!exitsData ? (
                    <SectionSkeleton />
                ) : (
                    <ExitPipeline
                        exits={exitsData}
                        exitPeriod={exitPeriod}
                        onPeriodChange={setExitPeriod}
                    />
                )}
            </section>

            {/* Divider */}
            <div className="border-t border-strokes" />

            {/* ─── Section 5: Attendance Patterns ──────────────────────── */}
            <section className="space-y-3">
                <div>
                    <div className="font-heading font-semibold text-base flex items-center gap-2">
                        <CalendarBlank size={16} className="text-brand-primary" weight="bold" />
                        Attendance by Slot
                    </div>
                    <div className="text-xs text-ink-muted mt-0.5">
                        Show-rate by day &amp; time · use this to weight availability toward slots that actually attend · {selectedPipelineName}
                    </div>
                </div>
                {!attendancePatterns ? (
                    <SectionSkeleton />
                ) : !attendancePatterns.cells?.length ? (
                    <div className="surface p-6 text-center text-sm text-ink-muted">
                        No marked attendance yet — the heatmap builds as you mark candidates attended or no-show.
                    </div>
                ) : (
                    <AttendanceHeatmap patterns={attendancePatterns} />
                )}
            </section>
        </div>
    );
}
