import { useState, useEffect } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Funnel, X } from "@phosphor-icons/react";

// No Applicant option — the board renders no Applicant column, so filtering to
// it can only ever return an empty board.
const STAGE_OPTIONS = [
    { value: "SCREENING", label: "Screening" },
    { value: "APPOINTMENT", label: "Appointment" },
    { value: "FORM", label: "Form" },
    { value: "CLOSE", label: "Close" },
    { value: "TRAINING", label: "Training" },
];

const VERDICT_OPTIONS = [
    { value: "strong", label: "Strong" },
    { value: "good", label: "Good" },
    { value: "borderline", label: "Borderline" },
    { value: "weak", label: "Weak" },
    { value: "incomplete", label: "Incomplete" },
];

const STATUS_OPTIONS = [
    { value: "pending", label: "Pending" },
    { value: "queued", label: "Queued" },
    { value: "in_progress", label: "Calling…" },
    { value: "approved", label: "Approved" },
    { value: "incomplete_info", label: "Incomplete" },
    { value: "no_answer", label: "No answer" },
    { value: "didnt_connect", label: "Didn't connect" },
    { value: "rejected", label: "Auto-rejected" },
    { value: "review", label: "Review" },
];

const APPOINTMENT_PRESETS = [
    { value: "any", label: "Any time" },
    { value: "today", label: "Today" },
    { value: "tomorrow", label: "Tomorrow" },
    { value: "this_week", label: "This week" },
    { value: "next_week", label: "Next week" },
    { value: "past", label: "Past" },
    { value: "custom", label: "Custom range" },
    { value: "none", label: "No appointment" },
];

// minRating is stripped on the way in as well as out: no card renders a rating,
// so a value saved before the filter was dropped would keep hiding candidates
// with nothing on screen to explain why.
const stripDropped = (f) => { const { minRating, ...rest } = f || {}; return rest; };

const togglePill = (set, value) => {
    const arr = Array.isArray(set) ? [...set] : [];
    const i = arr.indexOf(value);
    if (i >= 0) arr.splice(i, 1);
    else arr.push(value);
    return arr;
};

export default function FiltersModal({ open, onClose, filters, onApply, jobs = [], referrers = [] }) {
    const [local, setLocal] = useState(() => stripDropped(filters));
    useEffect(() => setLocal(stripDropped(filters)), [filters, open]);

    const Pills = ({ options, value = [], onChange, testidPrefix }) => (
        <div className="flex flex-wrap gap-1.5">
            {options.map((o) => {
                const on = value.includes(o.value);
                return (
                    <button
                        key={o.value}
                        type="button"
                        onClick={() => onChange(togglePill(value, o.value))}
                        data-testid={`${testidPrefix}-${o.value}`}
                        className={`text-[11px] px-2.5 py-1 rounded-full border transition-colors ${
                            on
                                ? "bg-brand-primary/20 border-brand-primary text-brand-primary"
                                : "bg-transparent border-strokes text-ink-muted hover:border-strokes-focus hover:text-ink"
                        }`}
                    >
                        {o.label}
                    </button>
                );
            })}
        </div>
    );

    return (
        <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
            <DialogContent className="bg-[#0E0E11] border-strokes max-w-lg max-h-[88vh] overflow-y-auto" data-testid="filters-modal">
                <DialogHeader>
                    <DialogTitle className="font-heading text-xl tracking-tight flex items-center gap-2">
                        <Funnel size={16} weight="duotone" className="text-brand-primary" />
                        Filter candidates
                    </DialogTitle>
                </DialogHeader>

                <div className="space-y-5 mt-1">
                    {/* Stage */}
                    <div>
                        <label className="label-overline block mb-2">Stage</label>
                        <Pills
                            options={STAGE_OPTIONS}
                            value={local.stages || []}
                            onChange={(v) => setLocal({ ...local, stages: v })}
                            testidPrefix="filter-stage"
                        />
                    </div>

                    {/* Verdict */}
                    <div>
                        <label className="label-overline block mb-2">AI Verdict</label>
                        <Pills
                            options={VERDICT_OPTIONS}
                            value={local.verdicts || []}
                            onChange={(v) => setLocal({ ...local, verdicts: v })}
                            testidPrefix="filter-verdict"
                        />
                    </div>

                    {/* Screening status */}
                    <div>
                        <label className="label-overline block mb-2">Screening status</label>
                        <Pills
                            options={STATUS_OPTIONS}
                            value={local.statuses || []}
                            onChange={(v) => setLocal({ ...local, statuses: v })}
                            testidPrefix="filter-status"
                        />
                    </div>

                    {/* Appointment date */}
                    <div>
                        <label className="label-overline block mb-2">Appointment date</label>
                        <Pills
                            options={APPOINTMENT_PRESETS}
                            value={local.apptPreset ? [local.apptPreset] : []}
                            onChange={(v) => {
                                const preset = v[v.length - 1] || null;
                                setLocal({ ...local, apptPreset: preset });
                            }}
                            testidPrefix="filter-appt"
                        />
                        {local.apptPreset === "custom" && (
                            <div className="grid grid-cols-2 gap-2 mt-3">
                                <div>
                                    <span className="text-[10px] text-ink-muted uppercase tracking-wider mb-1 block">From</span>
                                    <input
                                        type="date"
                                        className="input-dark text-xs !py-1.5"
                                        data-testid="filter-appt-from"
                                        value={local.apptFrom || ""}
                                        onChange={(e) => setLocal({ ...local, apptFrom: e.target.value })}
                                    />
                                </div>
                                <div>
                                    <span className="text-[10px] text-ink-muted uppercase tracking-wider mb-1 block">To</span>
                                    <input
                                        type="date"
                                        className="input-dark text-xs !py-1.5"
                                        data-testid="filter-appt-to"
                                        value={local.apptTo || ""}
                                        onChange={(e) => setLocal({ ...local, apptTo: e.target.value })}
                                    />
                                </div>
                            </div>
                        )}
                    </div>

                    {/* Job */}
                    {jobs.length > 0 && (
                        <div>
                            <label className="label-overline block mb-2">Job</label>
                            <select
                                className="input-dark text-xs !py-1.5"
                                data-testid="filter-job-select"
                                value={local.jobId || ""}
                                onChange={(e) => setLocal({ ...local, jobId: e.target.value || null })}
                            >
                                <option value="">Any job</option>
                                {jobs.map((j) => (
                                    <option key={j.id} value={j.id}>{j.title}</option>
                                ))}
                            </select>
                        </div>
                    )}

                    <div className="grid grid-cols-2 gap-3">
                        <div>
                            <label className="label-overline block mb-1.5">Min Smart Score</label>
                            <input
                                data-testid="filter-score-input"
                                type="number" min={0} max={100}
                                className="input-dark text-xs !py-1.5"
                                value={local.minScore ?? ""}
                                onChange={(e) => setLocal({ ...local, minScore: e.target.value === "" ? null : Number(e.target.value) })}
                            />
                        </div>
                    </div>

                    {/* Referred by */}
                    {referrers.length > 0 && (
                        <div>
                            <label className="label-overline block mb-2">Referred by</label>
                            <div className="flex flex-wrap gap-1.5">
                                {referrers.map((name) => {
                                    const on = local.referredBy === name;
                                    return (
                                        <button
                                            key={name}
                                            type="button"
                                            onClick={() => setLocal({ ...local, referredBy: on ? null : name })}
                                            data-testid={`filter-referred-${name}`}
                                            className={`text-[11px] px-2.5 py-1 rounded-full border transition-colors ${
                                                on
                                                    ? "bg-brand-primary/20 border-brand-primary text-brand-primary"
                                                    : "bg-transparent border-strokes text-ink-muted hover:border-strokes-focus hover:text-ink"
                                            }`}
                                        >
                                            {name}
                                        </button>
                                    );
                                })}
                            </div>
                        </div>
                    )}

                    {/* Contact toggles */}
                    <div className="flex flex-wrap gap-3 text-xs">
                        <label className="flex items-center gap-1.5 cursor-pointer select-none">
                            <input
                                type="checkbox"
                                className="accent-brand-primary"
                                data-testid="filter-has-phone"
                                checked={!!local.hasPhone}
                                onChange={(e) => setLocal({ ...local, hasPhone: e.target.checked })}
                            />
                            Has phone
                        </label>
                        <label className="flex items-center gap-1.5 cursor-pointer select-none">
                            <input
                                type="checkbox"
                                className="accent-brand-primary"
                                data-testid="filter-has-email"
                                checked={!!local.hasEmail}
                                onChange={(e) => setLocal({ ...local, hasEmail: e.target.checked })}
                            />
                            Has email
                        </label>
                        <label className="flex items-center gap-1.5 cursor-pointer select-none">
                            <input
                                type="checkbox"
                                className="accent-brand-primary"
                                data-testid="filter-has-resume"
                                checked={!!local.hasResume}
                                onChange={(e) => setLocal({ ...local, hasResume: e.target.checked })}
                            />
                            Has resume
                        </label>
                    </div>

                    <div className="flex gap-2 pt-3 border-t border-strokes">
                        <button
                            onClick={() => { onApply({}); }}
                            data-testid="filters-reset"
                            className="btn-secondary flex-1 flex items-center justify-center gap-1.5"
                        >
                            <X size={11} weight="bold" /> Reset
                        </button>
                        <button
                            onClick={() => onApply(local)}
                            data-testid="filters-apply"
                            className="btn-primary flex-1"
                        >
                            Apply filters
                        </button>
                    </div>
                </div>
            </DialogContent>
        </Dialog>
    );
}

/** Count active filters for the header badge. */
export function countActiveFilters(f) {
    if (!f) return 0;
    let n = 0;
    if (f.search) n += 1;
    if (Array.isArray(f.stages) && f.stages.length) n += 1;
    if (Array.isArray(f.verdicts) && f.verdicts.length) n += 1;
    if (Array.isArray(f.statuses) && f.statuses.length) n += 1;
    if (f.apptPreset && f.apptPreset !== "any") n += 1;
    if (f.jobId) n += 1;
    if (f.minScore != null) n += 1;
    if (f.hasPhone) n += 1;
    if (f.hasEmail) n += 1;
    if (f.hasResume) n += 1;
    if (f.referredBy) n += 1;
    return n;
}
