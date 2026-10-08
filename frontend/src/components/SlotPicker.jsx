import { useEffect, useMemo, useState } from "react";
import api from "@/lib/api";
import { Calendar, CaretLeft, CaretRight } from "@phosphor-icons/react";
import { fmtET, DEFAULT_TZ } from "@/lib/formatET";
import { usePipeline } from "@/lib/pipeline";

/**
 * Lists upcoming available appointment slots for a pipeline grouped by day,
 * lets the user click one. Used in the Approve / Move-to-Appointment confirm
 * dialog so the recruiter never sends a "blank slot" approval email.
 *
 * Props:
 *   pipelineId  required — drives which schedule we read
 *   value       optional — pre-selected ISO datetime
 *   onChange    (iso, slotMeta) => void
 */
export default function SlotPicker({ pipelineId, value, onChange }) {
    // Recruiter-facing: pin every time to the account timezone, never the browser's.
    const { timezone } = usePipeline() || {};
    const tz = timezone || DEFAULT_TZ;
    const [slots, setSlots] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);
    // `dayIndex` is the index inside the unique-days array we render with arrows.
    const [dayIndex, setDayIndex] = useState(0);

    useEffect(() => {
        if (!pipelineId) return;
        let cancelled = false;
        setLoading(true);
        setError(null);
        api.get(`/pipelines/${pipelineId}/slots`, { params: { days: 14 } })
            .then((r) => {
                if (cancelled) return;
                setSlots(r.data?.slots || []);
            })
            .catch(() => { if (!cancelled) setError("Couldn't load available slots."); })
            .finally(() => { if (!cancelled) setLoading(false); });
        return () => { cancelled = true; };
    }, [pipelineId]);

    // Group slots by date string (in slot's local label) so we get day-cards.
    const groupedDays = useMemo(() => {
        const buckets = new Map();
        for (const s of slots) {
            const d = new Date(s.datetime);
            const key = fmtET(s.datetime, { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" });
            if (!buckets.has(key)) buckets.set(key, { key, dateObj: d, iso: s.datetime, slots: [] });
            buckets.get(key).slots.push(s);
        }
        return Array.from(buckets.values());
    }, [slots, tz]);

    // When `value` changes (e.g. AI-captured slot is pre-selected), jump the
    // visible day to the one containing it.
    useEffect(() => {
        if (!value || groupedDays.length === 0) return;
        const target = fmtET(value, { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" });
        const idx = groupedDays.findIndex((d) => d.key === target);
        if (idx >= 0) setDayIndex(idx);
    }, [value, groupedDays, tz]);

    if (!pipelineId) return null;

    const visibleDay = groupedDays[dayIndex];

    return (
        <div className="space-y-2 rounded-md border border-strokes bg-[#0B0B0F] p-3" data-testid="slot-picker">
            <div className="flex items-center justify-between">
                <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-widest text-ink-muted font-semibold">
                    <Calendar size={12} weight="bold" /> Pick a slot
                </div>
                {groupedDays.length > 1 && (
                    <div className="flex items-center gap-1">
                        <button
                            type="button"
                            onClick={() => setDayIndex((i) => Math.max(0, i - 1))}
                            disabled={dayIndex === 0}
                            data-testid="slot-picker-prev-day"
                            className="p-1 text-ink-muted hover:text-ink disabled:opacity-30"
                            aria-label="Previous day"
                        >
                            <CaretLeft size={14} weight="bold" />
                        </button>
                        <span className="text-[11px] text-ink-muted tabular-nums">
                            {dayIndex + 1} / {groupedDays.length}
                        </span>
                        <button
                            type="button"
                            onClick={() => setDayIndex((i) => Math.min(groupedDays.length - 1, i + 1))}
                            disabled={dayIndex >= groupedDays.length - 1}
                            data-testid="slot-picker-next-day"
                            className="p-1 text-ink-muted hover:text-ink disabled:opacity-30"
                            aria-label="Next day"
                        >
                            <CaretRight size={14} weight="bold" />
                        </button>
                    </div>
                )}
            </div>

            {loading && <div className="text-xs text-ink-muted py-3 text-center">Loading slots…</div>}
            {error && <div className="text-xs text-[#F87171] py-2">{error}</div>}
            {!loading && !error && groupedDays.length === 0 && (
                <div className="text-xs text-ink-muted py-3 text-center">
                    No upcoming slots available. Check Calendar → Manage availability.
                </div>
            )}
            {visibleDay && (
                <>
                    <div className="text-sm font-semibold text-ink">
                        {fmtET(visibleDay.iso, { timeZone: tz, weekday: "long", month: "short", day: "numeric" })}
                    </div>
                    <div className="grid grid-cols-3 gap-1.5">
                        {visibleDay.slots.map((s) => {
                            const selected = value === s.datetime;
                            return (
                                <button
                                    key={s.datetime}
                                    type="button"
                                    onClick={() => onChange?.(s.datetime, s)}
                                    data-testid={`slot-pick-${s.datetime}`}
                                    data-selected={selected ? "true" : "false"}
                                    className={`text-xs px-2 py-1.5 rounded border transition-colors ${
                                        selected
                                            ? "bg-brand-primary border-brand-primary text-white font-semibold"
                                            : "bg-[#0E0E13] border-strokes text-ink hover:border-brand-primary/60 hover:text-brand-primary"
                                    }`}
                                >
                                    {fmtET(s.datetime, { timeZone: tz, hour: "numeric", minute: "2-digit" })}
                                    {s.capacity_remaining < 5 && (
                                        <div className="text-[9px] opacity-70 mt-0.5">
                                            {s.capacity_remaining} left
                                        </div>
                                    )}
                                </button>
                            );
                        })}
                    </div>
                </>
            )}
        </div>
    );
}
