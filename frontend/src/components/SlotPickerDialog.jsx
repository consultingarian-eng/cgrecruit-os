import { useState, useEffect, useMemo } from "react";
import {
    Dialog, DialogContent, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Calendar, ArrowRight } from "@phosphor-icons/react";
import api from "@/lib/api";
import { fmtET, DEFAULT_TZ } from "@/lib/formatET";
import { usePipeline } from "@/lib/pipeline";

// Recruiter-facing: always the account's timezone, never the browser's. A slot
// list that silently reads 6:15 AM because someone opened the app from the west
// coast is worse than useless when the office runs on ET.
const fmtDate = (iso, tz) => fmtET(iso, { timeZone: tz, weekday: "short", month: "short", day: "numeric" });
const fmtTime = (iso, tz) => fmtET(iso, { timeZone: tz, hour: "numeric", minute: "2-digit" });
const dayKey = (iso, tz) => fmtET(iso, { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" });

/**
 * Slot picker that pops when a recruiter drops a candidate into the APPOINTMENT
 * column without an `appointment_at`. They pick a slot from available pipeline
 * times, then confirm — at confirm time we delegate to the parent so the
 * standard StageMoveConfirmDialog (with email-send checkbox) can fire.
 */
export default function SlotPickerDialog({ open, onOpenChange, candidate, pipelineId, onPick, mode = "book" }) {
    const { timezone } = usePipeline() || {};
    const tz = timezone || DEFAULT_TZ;
    const [slots, setSlots] = useState([]);
    const [selected, setSelected] = useState("");
    const [loading, setLoading] = useState(false);

    useEffect(() => {
        if (!open || !pipelineId) return;
        setSelected("");
        setLoading(true);
        api.get(`/pipelines/${pipelineId}/slots?days=21`)
            .then((r) => setSlots(r.data?.slots || []))
            .catch(() => setSlots([]))
            .finally(() => setLoading(false));
    }, [open, pipelineId]);

    // Group slots by day for cleaner select rendering.
    const grouped = useMemo(() => {
        const byDay = new Map();
        for (const s of slots) {
            const d = new Date(s.datetime);
            if (Number.isNaN(d.getTime())) continue;
            // Group on the account's calendar day, not the browser's — an
            // evening ET slot is tomorrow in UTC and would split off on its own.
            const key = dayKey(s.datetime, tz);
            if (!byDay.has(key)) byDay.set(key, { date: d, iso: s.datetime, slots: [] });
            byDay.get(key).slots.push(s);
        }
        return [...byDay.values()];
    }, [slots, tz]);

    const handleConfirm = () => {
        if (!selected) return;
        onPick?.(selected);
    };

    const isReschedule = mode === "reschedule";
    const currentAt = candidate?.appointment_at;

    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="bg-[#0E0E11] border-strokes max-w-md" data-testid="slot-picker-dialog">
                <DialogHeader>
                    <DialogTitle className="font-heading text-xl tracking-tight flex items-center gap-2">
                        <Calendar size={16} weight="duotone" className="text-brand-primary" />
                        {isReschedule ? "Reschedule interview" : "Pick an interview time"}
                    </DialogTitle>
                </DialogHeader>
                <div className="space-y-3">
                    <div className="text-xs text-ink-muted leading-relaxed">
                        {isReschedule ? (
                            <>
                                Pick a new time for{" "}
                                <strong className="text-ink">{candidate?.first_name} {candidate?.last_name}</strong>
                                {currentAt && (
                                    <>
                                        {" "}— currently booked for{" "}
                                        <strong className="text-ink">{fmtDate(currentAt, tz)} {fmtTime(currentAt, tz)}</strong>
                                    </>
                                )}
                                . They'll get an updated email + SMS with the new time and reminders will move automatically.
                            </>
                        ) : (
                            <>
                                <strong className="text-ink">{candidate?.first_name} {candidate?.last_name}</strong> has no appointment booked yet. Pick a time from your available pipeline slots — they'll get the booking email + reminders automatically.
                            </>
                        )}
                    </div>

                    {loading && <div className="text-sm text-ink-muted py-6 text-center">Loading slots…</div>}

                    {!loading && grouped.length === 0 && (
                        <div className="surface p-4 text-center text-sm" data-testid="slot-picker-empty">
                            <Calendar size={20} weight="duotone" className="text-ink-muted mx-auto mb-2" />
                            <div className="text-ink-muted">No open slots in the next 3 weeks.</div>
                            <div className="text-[11px] text-ink-dim mt-1">Add availability rules in the Calendar tab first.</div>
                        </div>
                    )}

                    {!loading && grouped.length > 0 && (
                        <div className="max-h-[55vh] overflow-y-auto pr-1 space-y-3" data-testid="slot-picker-grid">
                            {grouped.map(({ date, iso, slots: daySlots }) => (
                                <div key={date.toISOString()}>
                                    <div className="text-[10px] uppercase tracking-widest text-ink-muted font-semibold mb-1.5 sticky top-0 bg-[#0E0E11] py-0.5">
                                        {fmtET(iso, { timeZone: tz, weekday: "long", month: "long", day: "numeric" })}
                                    </div>
                                    <div className="grid grid-cols-3 gap-1.5">
                                        {daySlots.map((s) => {
                                            const isSelected = selected === s.datetime;
                                            const remaining = s.remaining_capacity;
                                            return (
                                                <button
                                                    key={s.datetime}
                                                    type="button"
                                                    onClick={() => setSelected(s.datetime)}
                                                    data-testid={`slot-option-${s.datetime}`}
                                                    className={`text-xs px-2 py-1.5 rounded border transition-colors ${
                                                        isSelected
                                                            ? "bg-brand-primary text-white border-brand-primary"
                                                            : "bg-transparent border-strokes text-ink-muted hover:border-brand-primary hover:text-ink"
                                                    }`}
                                                >
                                                    {fmtTime(s.datetime, tz)}
                                                    {remaining != null && remaining <= 5 && (
                                                        <span className="block text-[9px] opacity-70 mt-0.5">{remaining} left</span>
                                                    )}
                                                </button>
                                            );
                                        })}
                                    </div>
                                </div>
                            ))}
                        </div>
                    )}

                    <div className="flex gap-2 pt-2 border-t border-strokes">
                        <button onClick={() => onOpenChange(false)} className="btn-secondary flex-1" data-testid="slot-picker-cancel">
                            Cancel
                        </button>
                        <button
                            onClick={handleConfirm}
                            disabled={!selected}
                            data-testid="slot-picker-confirm"
                            className="btn-primary flex-1 flex items-center justify-center gap-1.5 disabled:opacity-40"
                        >
                            {selected ? `${fmtDate(selected, tz)} ${fmtTime(selected, tz)}` : "Pick a slot"}
                            {selected && <ArrowRight size={11} weight="bold" />}
                        </button>
                    </div>
                </div>
            </DialogContent>
        </Dialog>
    );
}
